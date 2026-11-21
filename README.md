# 单站点道路噪声分档 API

一个 FastAPI + SQLite 的单站点服务：按日录入逐分钟整数分贝值，或直接录入带原始边界的
分档分钟数；按日期范围和候选边界查询，得到每日各档分钟数，可分页续取。

- 某候选边界若会切开范围内任一天非空的历史分档，则**丢弃该边界**，并回报该日与它的原档。
- 用**余下的边界**统计：档位是 `[lower, upper)`，等于边界的读数归**较高档**。
- 查询一旦建立就**冻结**：边界、各日计数、每页日期全部落库。之后补报、更正、重启都不改变旧查询；
  凭 `query_id` 逐页续取，跨重启一致。
- 新查询每次读取当时的最新数据。
- 日档必须**从负无穷到正无穷连续且互不重叠**：首档下端、末档上端开放，中间档两端都必须是
  显式边沿，且后一档的 `lower` 必须恰好等于前一档的 `upper`。空档（0 分钟）也不能例外。
- 存量异常日档不会被拿去生成快照：新查询返回 `409 anomalous_day_bins` 与异常日期/修订号；
  旧查询按原号翻页，详情里标出该日当时的判定，或标出“因原档已被覆盖而无法核验”。

## 启动

```bash
docker compose up --build -d
# Swagger: http://127.0.0.1:8000/docs      ReDoc: /redoc
curl -s http://127.0.0.1:8000/health
```

数据保存在命名卷 `noise-data` 的 `/data/noise.db`（WAL 模式）。`docker compose down` 保留数据，
`docker compose down -v` 才会清空。

端口和站点名可通过环境变量覆盖：

```bash
API_PORT=9000 NOISE_STATION=ring-road-north docker compose up --build -d
```

## 数据模型

| 表 | 作用 |
| --- | --- |
| `day` | 每天一行：日期、修订号、来源（`minutes`/`bins`） |
| `reading` | 逐分钟读数日的 1440 行整数 |
| `bin_interval` | 分档日的原档（半开区间，`NULL` 表示无界）与分钟数 |
| `query_log` | 查询快照：请求/生效边界、被拒边界及证据、缺失日期、分页大小 |
| `query_day` | 快照中每日各档计数与当时的修订号 |
| `query_page` | 冻结的每页日期列表 |
| `query_day_audit` | 每个快照日当时采用的修订号与档位判定（`valid`/`anomalous`/`unverifiable`/`not_applicable`），写一次后永不重算 |

所有写入都在一个 `BEGIN IMMEDIATE` 事务内完成，整天替换要么全成、要么全不成。
`user_version` 为 2；新表用 `CREATE TABLE IF NOT EXISTS` 建立，旧库升级时无需手工迁移。

## 接口

### 录入/查询日数据

```bash
# 逐分钟：必须恰好 1440 个整数，dB ∈ [-100, 300]
curl -X PUT http://127.0.0.1:8000/days/2024-06-01 \
  -H 'Content-Type: application/json' \
  -d '{"mode":"minutes","values":[...1440 ints...]}'

# 分档（显式原档；首档 lower=null，末档 upper=null，必须连续且合计 1440）
curl -X PUT http://127.0.0.1:8000/days/2024-06-02 \
  -H 'Content-Type: application/json' \
  -d '{"mode":"bins","intervals":[
        {"lower":null,"upper":55,"minutes":210},
        {"lower":55,"upper":65,"minutes":690},
        {"lower":65,"upper":null,"minutes":540}]}'

# 分档（紧凑写法：n 个边界 + n+1 个计数，等价于上面的原档）
curl -X PUT http://127.0.0.1:8000/days/2024-06-02 \
  -H 'Content-Type: application/json' \
  -d '{"mode":"bins","boundaries":[55,65],"counts":[210,690,540]}'
```

同一日期再次 PUT 即**整天更正**，`revision` 加一（来源也可以换，例如先报分钟再更正为分档）。
更正被 4xx 拒绝时**不会**改动已存日档，`revision` 也不变——校验在写事务之前完成。

日档规则（`bins` 来源）：首档 `lower` 必须为 `null`、末档 `upper` 必须为 `null`；所有中间档
两端都必须有界；后一档 `lower` 必须等于前一档 `upper`。违反时返回 `422`，`code` 为
`bins_not_contiguous` / `bins_do_not_cover_day` / `invalid_bin_range` 等，并在 `problems`
里逐条给出原因（`gap`、`overlap`、`unbounded_inner_bin`）。因此日档总是从 `-inf` 到 `+inf`
连续且互不重叠。

`GET /days/{date}` 取回最新版本；`GET /days?start_date=&end_date=` 列出区间内已存日期。

### 建查询（边界审查 + 冻结）

```bash
curl -X PUT ...  # 录入若干天
curl -X POST http://127.0.0.1:8000/queries \
  -H 'Content-Type: application/json' \
  -d '{"start_date":"2023-01-01","end_date":"2025-12-31",
       "boundaries":[55,60,65,70],"page_size":31}'
```

返回（节选）：

```json
{
  "query_id": "q-…",
  "requested_boundaries": [55, 60, 65, 70],
  "effective_boundaries": [55, 65],
  "rejected": [
    {"boundary": 60, "reason": "would_split_original_bin", "severed_total": 1,
     "severed_truncated": false,
     "severed": [{"date": "2024-06-02",
                  "original_bin": {"lower": 55, "upper": 65, "minutes": 690}}]}
  ],
  "bins": [{"index":0,"lower":null,"upper":55,"label":"<55"}, …],
  "missing_dates": ["2023-01-01", …],
  "pages_total": 36, "days_total": 1095, "days_available": 2
}
```

审查规则：只有 `bins` 来源的日子会约束边界（分钟来源本身有全分辨率，任何边界都能精确计算）。
边界落在某个**非空**原档内部（`lower < b < upper`）即被拒；边界恰好等于原档边沿不算切开。
被拒明细最多列 50 条，`severed_total` 给出真实总数，`severed_truncated` 标记是否截断。

计数规则：档位是 `[lower, upper)`，因此读数 `v` 落入第 `bisect_right(boundaries, v)` 档——等于
边界归较高档。分档日因为不存在切割，每个非空原档完整落入唯一目标档。

### 存量异常日档

旧版本能存下“中间档带无界端点”的日子（例如 `[null,55)` 后面直接跟一个无界档，或两档边界
重叠）。这类日子无法用一套连续档位正确统计，所以建查询时先做判定：

- 范围内任一天的分档不满足“连续且互不重叠”（或分钟数合计不是 1440），整个查询**不生成快照**，
  返回 `409 anomalous_day_bins`，并列出每个异常日期及其**当时的修订号**：

```json
{"error": {
  "code": "anomalous_day_bins",
  "message": "1 stored day(s) in range have bins that do not continuously tile (-inf, +inf); no query was created",
  "anomalous_dates": ["2024-03-01"],
  "anomalies": [{"date": "2024-03-01", "revision": 7, "source": "bins",
                 "problems": [{"code": "bins_not_contiguous", "bin": 1, "reason": "gap", …}],
                 "original_bins": [{"lower": null, "upper": 55, "minutes": 600}, …]}]
}}
```

`PUT /days/{date}` 用合法分档（或分钟数据）更正该日之后，新查询即可正常建立。

修复前冻结的旧查询**照常可用**：`query_id` 不变，按原页码取回当时的档位与计数。判定写在
`query_day_audit`，每个日期记录“当时采用的修订号 + 结论”，只写一次，后续更正或服务重启都不会重算：

| `status` | 含义 |
| --- | --- |
| `valid` | 当时采用的修订已核验，档位连续且互不重叠 |
| `anomalous` | 当时采用的修订已核验，档位异常（只可能来自旧版本冻结的查询） |
| `unverifiable` | 当时采用的原始档位已被更正覆盖，无法再核验；仍按冻结计数返回 |
| `not_applicable` | 分钟来源的日，没有原始档位可核验 |

升级后首次启动会对所有缺判定的旧查询补判：若当天仍停在被冻结的修订号上，就能给出
`valid`/`anomalous`；若已被覆盖，则永久记为 `unverifiable`。

```bash
curl "http://127.0.0.1:8000/queries/q-…/audit"          # 全部快照日的判定
curl "http://127.0.0.1:8000/queries/q-…"                # audit 汇总 + 异常/无法核验清单
curl "http://127.0.0.1:8000/queries/q-…/days/2024-03-01" # 单日：audit 含当时日期与修订号
```

### 逐页取数

```bash
curl "http://127.0.0.1:8000/queries/q-…/pages/1"
curl "http://127.0.0.1:8000/queries/q-…/pages/2"
curl "http://127.0.0.1:8000/queries/q-…/days/2024-06-01"   # 单日
curl "http://127.0.0.1:8000/queries/q-…"                    # 元数据 + 页索引
```

页码从 1 开始，超过 `pages_total` 返回 404 `page_not_found`。范围内缺失的日期仍占页内位置，
返回 `available: false`、`counts: null`——这样页边界与日期集合在任何时候都可复现。

### 冻结语义

- 旧 `query_id` 的响应只会返回建查询时的边界与计数；`revision` 字段标明当时钉住的是哪一版。
- 分页结果里的 `bin_status` / `anomalous` / `original_bins` 来自冻结判定，同样不随数据更正变化。
- 出错处理不会写快照（整天替换也是单事务）；建查询因存量异常日档被拒时同样什么都不写。
- 所有查询状态都在 SQLite 中，服务重启后凭 `query_id` 继续翻页，结果逐字节一致。

### 错误格式

所有错误统一为：

```json
{"error": {"code": "bin_total_mismatch", "message": "…", "expected": 1440, "got": 1200}}
```

常见 `code`：`wrong_minute_count`、`invalid_level`、`bin_total_mismatch`、`bins_not_contiguous`、
`bins_do_not_cover_day`、`invalid_bin_range`、`boundaries_not_ascending`、`invalid_range`、
`range_too_long`、`anomalous_day_bins`、`day_not_found`、`query_not_found`、`page_not_found`、
`date_outside_query`、`day_missing_in_query`、`invalid_request`（请求体/路径校验失败，附 `problems`）。

## 本地开发与自测

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --port 8000          # NOISE_DB 默认 ./data/noise.db，可覆盖
python scripts/demo.py http://127.0.0.1:8000
```

`scripts/demo.py` 只用标准库，分两段演示：

1. 常规流程：分钟日 + 分档日 → 边界审查 → 两页取数 → 更正一天 → 旧查询保持不变、新查询看到更正。
2. 异常日档：先由 `scripts/legacy_seed.py` 造出“旧版本遗留库”，再展示新查询被 `409` 拒绝并
   给出日期/修订号、旧查询照常翻页并标出 `anomalous` / `unverifiable`、更正后新查询恢复正常。

Docker Compose 下的完整演示（Swagger：<http://127.0.0.1:8000/docs>）：

```bash
docker compose up --build -d
docker compose exec api python scripts/legacy_seed.py   # 造旧库：异常日档 + 两条旧查询
python3 scripts/demo.py http://127.0.0.1:8000           # 终端里跑完全部场景
# 或直接在 Swagger UI 里按上面 seed 输出的 1–7 步点：
#   GET  /queries/q-legacy-anomaly        → audit.anomalous_days（2022-01-15 rev 2）
#   POST /queries                         → 409 anomalous_day_bins（含日期与修订号）
#   GET  /queries/q-legacy-overwritten    → audit.unverifiable_days（2022-01-16 rev 1）
#   PUT  /days/2022-01-15                 → 用合法分档或分钟数据更正
#   POST /queries                         → 201；旧 query_id 仍返回原计数
```

## 边界与限制

- 单站点：站点名只来自 `NOISE_STATION` 环境变量，库内不存多站。
- 日期范围上限 3660 天；候选边界上限 100 个；`page_size` 1–366。
- 分贝读数限制在 [-100, 300]，边界限制在 [-1000, 1000]（整数）。
- 查询快照会物化每日计数，长期大量历史查询会使 `query_day` 增长；没有做快照回收接口。
- 只支持“以天为单位”的一次性提交，不支持单分钟增量补丁；更正即整天替换。
