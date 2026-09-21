# 交叉实验顺序平衡 API

基于 **Python + FastAPI + SQLite** 的交叉（crossover）实验顺序分配、**按周期受控揭盲**
与**周期完成 / 洗脱期门禁**服务。
创建实验时提交互异条件、受试者 ID、随机种子与可选的洗脱秒数，服务生成
**Williams 平衡拉丁方**序列并映射给受试者，保证：

- 每个条件在每个位置上出现次数相等；
- 每个**有向相邻条件对**出现次数相等（平衡一阶残留效应）；
- 完整序列组：偶数条件含 `n` 条序列，奇数条件含 `2n` 条序列；
- 受试者数量必须是完整组大小的整数倍，否则返回 `422` 及最近可接受数量；
- 相同输入与相同种子产生完全相同的分配结果（确定性分配）。

## 受控揭盲（period-controlled unblinding）

完整条件序列**只保存在服务端**，避免受试者提前获知后续安排：

- 创建接口 `POST /experiments` 与详情接口 `GET /experiments/{id}` 只返回受试者名册
  （`assignments[].subject_id`）与条件清单，不包含任何人的条件序列或序列下标；
- 通过推进接口逐周期揭盲：提交 `subject_id`、`expected_period` 与 `request_id`，
  **仅当 `expected_period` 正好等于该受试者的下一周期**时返回当前周期条件并原子推进；
- 相同 `request_id` 重试（接口超时、网络重试等）返回**首次结果**且不重复推进；
  同一 `request_id` 绑定不同受试者或周期返回 `409`；
- 并发请求或过期周期（落后 / 超前 / 实验已结束）返回 `409`，冲突路径不写入、不多揭露任何条件；
- 进度查询只返回**已揭盲记录**、**已完成周期**与 `next_period` / `next_available_at`，
  不暴露后续周期安排。

## 周期完成与洗脱期门禁（period completion & washout gate）

创建实验时可通过非负的 `washout_seconds`（缺省 `0`）配置相邻周期之间的最小洗脱时间：

- **完成登记**：`POST /experiments/{id}/complete` 登记“当前已揭盲且尚未完成”周期的
  完成时间，`completed_at` 由**服务端时钟**生成并持久化；同一 `request_id` 重放首次结果，
  键冲突（绑定不同受试者 / 周期）或越序（周期未揭盲、已完成、尚无揭盲周期）返回 `409`；
- **推进门禁**：配置了洗脱期的实验，推进到第 2 个及以后的周期前，上一周期必须
  **已登记完成**，且服务端当前时间已达到 `completed_at + washout_seconds`；
  未完成返回 `409`（`previous_period_not_completed`，不给等待时间），
  未到期返回 `409`（`washout_not_elapsed`，回传 `retry_after_seconds` 与
  `next_available_at`），响应体**不含下一周期条件**；
- **服务端时间裁决**：完成时间与是否届满都以服务端时钟为准，完成登记、推进门禁、
  揭盲写入在单个 `BEGIN IMMEDIATE` 事务内完成，并发完成 / 推进串行化，状态原子一致；
- **零值向后兼容**：`washout_seconds = 0` 时不强制完成登记，推进行为与旧版本完全一致；
  旧版本数据库启动时自动迁移（补齐列与新表），未配置洗脱期的旧实验一律按 `0` 秒处理。

## 快速启动（Docker Compose，一条命令）

```bash
docker compose up --build
```

该命令会构建镜像并启动容器：构建时安装依赖，容器启动命令中再次校验依赖安装，
应用启动钩子（lifespan）自动完成 SQLite 建表与迁移。**旧版本数据库无需手工迁移**：
启动时新增的进度、揭盲、幂等、完成登记表通过 `CREATE TABLE IF NOT EXISTS` 自动补齐，
`experiments.washout_seconds` 列通过 `ALTER TABLE ... ADD COLUMN` 补齐（缺省 `0`），
旧实验数据保留、可继续揭盲，并按 `0` 秒洗脱处理（完成登记不强制）。

- **访问地址**：http://localhost:8000
- **交互式 API 文档（Swagger UI）**：http://localhost:8000/docs
- **端口**：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 的 `ports` 中修改）

## 必需配置

无强制配置，开箱即用。可选环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/app/data/experiments.db`（compose 已设置）；本地运行默认 `./experiments.db` | SQLite 数据库文件路径，目录不存在时自动创建 |

compose 中通过命名卷 `crossover-data` 持久化数据，删除容器数据不丢失。

## API 说明

### 创建实验 `POST /experiments`

```bash
curl -X POST http://localhost:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{
        "conditions": ["A", "B", "C"],
        "subject_ids": ["s1", "s2", "s3", "s4", "s5", "s6"],
        "seed": 42,
        "washout_seconds": 86400
      }'
```

- `conditions`：互异条件，至少 2 个；
- `subject_ids`：互异受试者 ID；数量必须是完整组大小（3 个条件时为 6）的整数倍；
- `seed`：随机种子（整数）。相同输入与种子 → 相同分配；
- `washout_seconds`（可选，非负整数，缺省 `0`）：相邻周期之间的最小洗脱秒数。
  非 0 时，推进下一周期前必须先调用完成接口登记上一周期完成，并等待洗脱期届满。

成功返回 `201`。响应中 `assignments` **仅为受试者名册**，完整条件序列由服务端持有：

```json
{
  "experiment_id": "…",
  "conditions": ["A", "B", "C"],
  "seed": 42,
  "group_size": 6,
  "num_subjects": 6,
  "num_periods": 3,
  "washout_seconds": 86400,
  "created_at": "…",
  "assignments": [
    {"subject_id": "s1"},
    {"subject_id": "s2"}
  ]
}
```

受试者数量不成整组时返回 `422`，并给出最近可接受数量：

```json
{
  "detail": {
    "message": "受试者数量 7 无法整分为完整的序列组（每组 6 条序列）",
    "subject_count": 7,
    "group_size": 6,
    "nearest_acceptable": [6, 12]
  }
}
```

### 推进揭盲 `POST /experiments/{experiment_id}/advance`

进入下一周期前调用，请求体：

| 字段 | 说明 |
| --- | --- |
| `subject_id` | 受试者 ID |
| `expected_period` | 调用方认为该受试者当前所处的下一周期（**从 1 开始**） |
| `request_id` | 调用方生成的幂等键（如 UUID），同一请求重试必须复用 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/advance \
  -H 'Content-Type: application/json' \
  -d '{"subject_id": "s1", "expected_period": 1, "request_id": "9f1c…"}'
```

仅当 `expected_period` 等于该受试者下一周期、且（配置了洗脱期时）通过
完成 / 洗脱门禁时返回 `200`，并把进度原子推进到下一周期：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "period": 1,
  "condition": "A",
  "request_id": "9f1c…",
  "revealed_at": "2026-09-21T08:00:00.000000+00:00"
}
```

冲突一律返回 `409`，响应体 `detail.reason` 区分原因，且**不揭露任何条件**：

- `period_mismatch`：`expected_period` 落后或超前（并发抢先、重复提交、跳周期），
  同时回传服务端当前的 `next_period`；
- `experiment_complete`：该受试者已完成全部周期；
- `request_id_conflict`：相同 `request_id` 曾用于不同的 `subject_id` / `expected_period`；
- `previous_period_not_completed`：上一周期尚未调用完成接口登记，门禁不放行
  （不回传等待时间，调用方应先完成上一周期）；
- `washout_not_elapsed`：上一周期已完成但洗脱期未届满，回传
  `retry_after_seconds`（剩余整秒，向上取整）与 `next_available_at`
  （`completed_at + washout_seconds`），调用方可据此延迟重试。

未到期示例：

```json
{
  "detail": {
    "message": "洗脱期尚未届满，下一周期暂不可揭盲",
    "reason": "washout_not_elapsed",
    "next_period": 2,
    "retry_after_seconds": 86342,
    "next_available_at": "2026-09-22T08:00:00+00:00"
  }
}
```

幂等语义：相同 `(实验, request_id)` 且相同请求体重试，返回**首次的条件与 `revealed_at`**，
即使此时受试者已经推进到更后面的周期，也不会重复推进或改写历史。
门禁拦截属于失败路径（事务回滚），不会占用 `request_id`，届满后用同一键可首次成功。
洗脱期为 `0`（含未配置的旧实验）时门禁等价于放行，无需登记完成即可连续推进。

### 登记周期完成 `POST /experiments/{experiment_id}/complete`

完成当前周期的干预 / 观察后调用，登记服务端完成时间，作为下一周期洗脱门禁的起点。
请求体：

| 字段 | 说明 |
| --- | --- |
| `subject_id` | 受试者 ID |
| `expected_period` | 要登记完成的周期，必须是该受试者**已揭盲且尚未完成**的当前周期（从 1 开始） |
| `request_id` | 调用方生成的幂等键（如 UUID），同一请求重试必须复用 |

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/complete \
  -H 'Content-Type: application/json' \
  -d '{"subject_id": "s1", "expected_period": 1, "request_id": "7d20…"}'
```

成功返回 `200`，`completed_at` 为服务端生成的时间戳：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "period": 1,
  "request_id": "7d20…",
  "completed_at": "2026-09-21T08:00:00.000000+00:00"
}
```

- 相同 `(实验, request_id)` 且相同请求体重试，返回**首次结果**（含首次 `completed_at`），
  不重复登记；即使服务端时间已推进、后续周期已完成也一样重放；
- 同一 `request_id` 绑定不同受试者或周期 → `409 request_id_conflict`；
- 越序一律 `409` 且不写入：`no_revealed_period`（尚无揭盲周期）、
  `period_not_revealed`（目标周期还未揭盲，超前）、
  `period_already_completed`（目标周期已登记，重复 / 落后）、
  `period_mismatch`（不是当前待完成周期），并回传 `current_period`；
- 最后一个周期揭盲后同样可以（也应当）登记完成；洗脱期为 `0` 的实验中完成登记可选。
  实验或受试者不存在返回 `404`。

### 查询受试者进度 `GET /experiments/{experiment_id}/subjects/{subject_id}/progress`

只返回已揭盲记录、已完成周期与下一周期可揭盲时间，不含完整序列或任何后续安排：

```json
{
  "experiment_id": "…",
  "subject_id": "s1",
  "next_period": 2,
  "num_periods": 3,
  "washout_seconds": 86400,
  "completed": false,
  "reveals": [
    {"period": 1, "condition": "A", "revealed_at": "…"}
  ],
  "completions": [
    {"period": 1, "completed_at": "2026-09-21T08:00:00+00:00"}
  ],
  "next_available_at": "2026-09-22T08:00:00+00:00"
}
```

`next_available_at` 为 `null` 的情形：洗脱期为 0、已完成全部周期、下一周期门禁已届满，
或上一周期尚未登记完成（此时应先调用完成接口）。实验或受试者不存在返回 `404`。

### 典型时序（配置洗脱期的实验）

```text
advance period 1            → 200，揭盲条件
complete period 1           → 200，记录 completed_at
advance period 2（过早）     → 409 washout_not_elapsed + retry_after_seconds
… 等待至 next_available_at …
advance period 2            → 200，揭盲下一条件（响应此刻才首次出现）
complete period 2           → 200
… 依此类推
```

### 其他接口

- `GET /experiments/{experiment_id}`：查询实验（受试者名册 + 条件清单，**不含条件序列**；不存在返回 `404`）；
- `GET /experiments`：列出全部实验摘要；
- `GET /health`：健康检查。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                         # 运行测试（含序列平衡、幂等、409 并发、旧库迁移校验）
uvicorn app.main:app --reload  # 本地启动，默认 http://127.0.0.1:8000
```

## 实现要点

- **Williams 序列**（`app/williams.py`）：第一行按 `0, n-1, 1, n-2, …` 从两端交替取值，
  其余行循环移位（模 n）；奇数阶再拼接镜像方阵，使每个有向相邻对出现次数相等。
- **确定性分配**（`app/main.py`）：将完整序列组复制 `受试者数 / 组大小` 份构成序列池，
  用 `random.Random(seed)` 打乱后依次映射给受试者，因此相同输入与种子结果一致。
  序列只写入服务端存储，不进入创建 / 详情响应。
- **存储**（`app/db.py`，SQLite）共七张表，启动时 `CREATE TABLE IF NOT EXISTS` 幂等建表、
  `ALTER TABLE ... ADD COLUMN` 幂等补列（旧库自动迁移，未配置的旧实验按
  `washout_seconds = 0` 处理）：
  - `experiments`（含 `washout_seconds` 列）/ `assignments`：实验配置与受试者完整序列
    （序列服务端持有）；
  - `subject_progress`：每受试者的 `next_period`；
  - `unblindings`：已揭盲记录（周期 → 条件，含幂等键与时间戳）；
  - `advance_requests`：按 `(实验, request_id)` 唯一约束保存首次推进结果，支持重放；
  - `period_completions`：周期完成登记（周期 → 服务端 `completed_at`，主键约束防重复）；
  - `complete_requests`：完成接口按 `(实验, request_id)` 保存的首次结果，支持重放。
- **原子推进与门禁**：推进接口在单个 `BEGIN IMMEDIATE` 事务内完成“幂等检查 → 周期校验 →
  上一周期完成记录与 `completed_at + washout_seconds` 校验（服务端时钟）→
  写揭盲记录 / 推进进度 / 落盘幂等结果”；完成接口同样在写事务内完成“幂等检查 →
  越序校验 → 写完成记录 / 落盘幂等结果”。配合 `busy_timeout` 让并发请求串行化：
  每个周期只有一个推进 / 完成请求成功，其余得到 `409`；任何失败路径整体回滚，
  不多揭露条件、不留幂等垃圾。
