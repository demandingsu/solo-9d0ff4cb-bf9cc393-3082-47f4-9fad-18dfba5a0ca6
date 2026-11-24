# 炉温冷档服务（Furnace Cold Archive）

把炉次逐分钟炉温压缩成受“整炉留点上限”约束的冷档，写入 SQLite，并按炉次与分钟区间查询复原炉温和连续处于工艺温区内的时段。

温度、误差、温区上下限全部用**十分之一摄氏度**的整数表示（例：`7800` = 780.0 °C）。
温区上下限**计入**温区（闭区间）。

## 快速启动

```bash
docker compose up --build
```

- 接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 数据卷：`furnace-data`（SQLite 文件在容器内 `/data/furnace_archive.sqlite3`）

本地直接运行（需要 Python 3.11+）：

```bash
pip install -r requirements.txt
FURNACE_DB_PATH=./data/furnace_archive.sqlite3 \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/archives` | 压缩一个炉次并整炉入档；无解或写入失败返回原因 |
| `GET` | `/api/v1/archives` | 列出已归档炉次（最新在前）；坏档自动跳过并在响应中列出其炉次号 |
| `GET` | `/api/v1/archives/{run_id}` | 归档元数据与留点数 |
| `GET` | `/api/v1/archives/{run_id}/minutes?start_minute_utc=&end_minute_utc=` | 区间内的复原炉温、缺测分钟、温区内连续时段 |
| `GET` | `/health` | 数据库可达性 |

### 入档示例

```bash
curl -s -X POST http://localhost:8000/api/v1/archives \
  -H 'Content-Type: application/json' -d '{
    "run_id": "HT-2026-0042",
    "start_minute_utc": 29800000,
    "readings": [250, 4800, 7000, 7800, 7990, 8000, 8000, 8010, 8000, 8000,
                 7990, 8000, 8000, 8000, 8000, 7990, 7800, 6000, 4000],
    "band_low_tenths_c": 7800,
    "band_high_tenths_c": 8200,
    "tolerance_tenths_c": 5,
    "max_retained_points": 13
  }'
```

`readings` 是最多 360 个逐分钟值，`null` 表示缺测。成功返回 `201` 和归档摘要（含留点数、缺测分钟数、分段数、内容校验和）。

### 查询示例

```bash
curl -s 'http://localhost:8000/api/v1/archives/HT-2026-0042/minutes?start_minute_utc=29800000&end_minute_utc=29800018'
```

返回：

- `minutes[]`：每个分钟的 `value_tenths_c`（复原值）、`missing`、`band_state`（`below` / `in_band` / `above`，缺测为 `unknown`）；
- `missing_minutes[]`：区间内缺测分钟号；
- `in_band_intervals[]`：连续处于温区内的时段（含 `duration_minutes`）。

区间内的 `in_band_intervals` 返回**所有与查询区间相交**的完整时段，不会被查询边界截断，因此可以直接读出一次保温持续了多久；缺测分钟会打断时段。
查询区间必须落在该炉次范围内，否则返回 `416`。

### 清单与坏档

```bash
curl -s 'http://localhost:8000/api/v1/archives?limit=50'
```

返回：

- `archives[]`：按入档时间从新到旧、同时间按炉次号升序排列的可用档案摘要，最多 `limit` 条；
- `skipped_corrupt_run_ids[]`：为凑齐结果而跳过的坏档炉次号，顺序与扫描顺序一致；
- `available_exhausted`：`true` 表示全库已扫描完（可用档案不足 `limit` 条，已全部返回），`false` 表示收满 `limit` 条后提前停止；
- `limit`：本次请求的页大小。

坏档不会让清单整体失败；但单炉详情（`GET /api/v1/archives/{run_id}`）与分钟查询遇到坏档仍返回 `500 archive_corrupt`。

## 压缩规则与保证

1. **每段连续实测数据保留首尾**：缺测会把炉次切成若干连续实测段，每段首、尾采样点必须留点。
2. **只用原采样点连成折线**：留点全部来自原采样点，保留值即原读数；不存任何合成点。相邻留点之间按直线插值（四舍五入到整数十分之一度），缺测分钟不插值、不跨段连接。
3. **误差约束**：每个实测分钟的复原值与原读数之差不超过 `tolerance_tenths_c`。
4. **温区判定一致**：每个实测分钟复原值的 `below / in_band / above` 判定必须与原读数一致，上下限计入温区（`low ≤ v ≤ high` 为 `in_band`）。
5. **留点上限**：整炉留点数不得超过 `max_retained_points`。规划器用精确动态规划求出**最小留点数**：
   `cost[0]=1`，`cost[b]=1+min{cost[a] : a<b 且 a、b 之间直线可行}`；
   各连续段相互独立，最小留点数相加即为整炉最小值。若最小值超过上限则返回 `422`，并给出所需最小留点数与上限。
6. **整炉入档**：任意一段无解即整炉不入档；成功时整炉一次性写入。
7. **只存压缩档**：SQLite 中只保存留点（分钟偏移 + 原值）、缺测区间、参数与 SHA-256 校验和，不保存原始逐分钟序列。
8. **写入原子性**：使用 `BEGIN IMMEDIATE` 事务，失败完整回滚；已归档数据不受影响，照常可查。
9. **读档完整性**：每次读取都会先做严格的格式与结构校验，再校验 SHA-256。标量列必须是真正的整数（拒绝 `780.5` 这类小数、`true` 这类布尔值，绝不做静默取整）与非空字符串；留点和缺测区间必须是“两个整数组成的数组”，留点偏移严格递增、不重复且落在炉次范围内，缺测区间按偏移有序、互不重叠且不越界。随后还会校验留点数、段首尾留点与可重建性。被篡改的归档在详情和分钟查询上返回 `500 archive_corrupt`，不会影响其他归档的查询。
10. **清单容错与一致性**：`GET /api/v1/archives` 不再因单个坏档整张报错。它在**一个只读事务（同一份已提交快照）**内按 `created_at_utc` 从新到旧、同时间按炉次号升序**流式**扫描，逐行做第 9 条的完整校验：通过校验的记录收满 `limit` 条即停止（不会一次性装入全库压缩内容），坏档则跳过并把炉次号记入 `skipped_corrupt_run_ids`；若通过校验的记录不足 `limit`，则返回全部可用档案，`available_exhausted=true`。入档与查清单交错时，清单内容与跳过项始终来自同一份已提交数据，扫描期间新提交的炉次只会出现在下一次查询里。数据库本身不可读时仍返回 `503 archive_unavailable`。

## 错误与状态码

| 状态码 | `reason_code` | 含义 |
| --- | --- | --- |
| `201` | — | 整炉已入档 |
| `409` | `run_already_archived` | 炉次已归档，原归档保持不变 |
| `422` | `no_feasible_plan` | 无可行方案（附 `minimum_retained_points`、`max_retained_points`） |
| `422` | `invalid_request` / `invalid_range` | 请求校验失败 / 分钟区间颠倒 |
| `404` | `archive_not_found` | 该炉次无归档 |
| `416` | `range_outside_run` | 查询区间超出该炉次范围 |
| `503` | `archive_write_failed` | 写入失败（`retryable` 指示可否重试） |
| `503` | `archive_unavailable` | 数据库不可达/不可打开 |
| `500` | `archive_corrupt` | 归档校验失败（详情/分钟查询）；清单接口会跳过坏档并在 `skipped_corrupt_run_ids` 中列出 |

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖：规划器与穷举最优解对比、误差/温区/首尾留点约束、缺测不桥接、上限剔除原因、入库只存压缩档、重复炉次、写入失败后旧归档仍可查、篡改检测（小数/布尔/坏 JSON/坏结构/重叠缺测区间/坏摘要）、清单跳过坏档并报告炉次号、清单同秒按炉次号排序、凑满 limit 提前停止、入档交错下的快照一致性、区间与缺测标记。

## 结构

```
app/planner.py   压缩规划（精确最小留点）、复原、温区时段
app/db.py        SQLite 冷档存储与完整性校验
app/models.py    请求/响应模型与字段校验
app/main.py      FastAPI 路由与统一错误响应
tests/           规划器与 API 测试
Dockerfile, docker-compose.yml
```
