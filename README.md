# 实验室样本混样追踪 API

基于 **Python + FastAPI + SQLite** 的实验室样本混样（pooling）追踪服务。
客户端提交初始样本（体积 + 标记物浓度）与按时间排序的转移操作，服务在
**系统总量守恒（所有管内 + 累计移液损耗 waste）** 的前提下，逐步计算每次转移后：

* 每支管的**当前体积**与各标记物的**混合浓度**；
* 管内液体的**来源构成与占比**（来自哪些初始样本、各占多少体积 / 百分比）；
* 可选 `delivered_amount` 时的**移液损耗追踪**：吸出量 `amount` 与实际
  入管量 `delivered_amount` 的差额按源管当时浓度、来源比例计入该步 `lost`，
  并累计为结果 `waste`；
* 两个口径的守恒量：含 waste 的**系统总量差**（应恒为 0）与仅看管内的
  **管内体积 / 质量变化**（恰好等于累计损耗）。

执行模式（`execution_mode`）：

* `sequential`（默认）：转移按数组顺序逐条即时落地，完全兼容旧版行为；
* `time_batch`：解析后 `time` 相等的**连续**转移合并为一个**原子批次**——
  所有吸取都基于**批次开始快照**计算（浓度、来源构成一致，不受数组顺序影响），
  先按源管汇总 `amount` 校验可用量，再统一写入目标管与 waste；任一操作无效时
  **整批不落地**（错误仍定位到原 `transfers` 下标，之前批次结果保留）。每步
  响应带 `batch_index`，同批共享。

转移量超出当前可用体积、时间逆序、引用未声明的样本时，返回 **HTTP 400** 和
定位明确的结构化错误（如 `transfers[2].source`），失败记录同样持久化。

---

## 一条命令启动

需要本机已安装 Docker 与 Docker Compose（无需任何第三方账号、无需外部数据库）：

```bash
docker compose up --build
```

启动后访问：

| 入口 | 地址 |
| --- | --- |
| API 服务 | http://localhost:8000 |
| Swagger 交互文档 | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| 健康检查 | http://localhost:8000/health |

SQLite 数据文件保存在 Docker 命名卷 `pooling-data` 中（容器内 `/data/pooling.db`），
容器重建后记录不丢失。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时清空数据库卷
```

### 启动后的示例（含移液损耗追踪）

服务起来后，另开一个终端执行（第一步吸 50 只注入 48，损耗 2）：

```bash
# 1) 提交模拟
curl -s -X POST http://localhost:8000/api/simulations \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Docker 启动自检-含移液损耗",
    "samples": [
      {"id": "S1", "initial_volume": 100, "concentration": 12.5},
      {"id": "S2", "initial_volume": 50, "markers": {"RFP": 8}}
    ],
    "transfers": [
      {"time": 1, "source": "S1", "target": "S2", "amount": 50, "delivered_amount": 48},
      {"time": 2, "source": "S2", "target": "S1", "amount": 25}
    ]
  }' | python3 -m json.tool

# 2) 用上一步返回的 id 复查（含 waste / lost 的完整结果已落库）
curl -s http://localhost:8000/api/simulations/<id> | python3 -m json.tool

# 3) 查看历史列表
curl -s "http://localhost:8000/api/simulations?limit=10" | python3 -m json.tool
```

预期要点：第一步 `steps[0].lost.volume == 2.0`、
`waste.marker_amount.default == 25.0`、管内合计 148 但
`conservation.final_total_volume == 150.0`（含 waste）、
`volume_difference == 0` 且 `tube_volume_change == -2.0`；
第二步未传 `delivered_amount`，`lost.volume == 0`，行为与旧版一致。

想验证按时间批次的原子模式，只需在请求体顶层加
`"execution_mode": "time_batch"`（默认 `sequential`，旧请求无需改动）：

```bash
curl -s -X POST http://localhost:8000/api/simulations \
  -H "Content-Type: application/json" \
  -d '{
    "execution_mode": "time_batch",
    "samples": [
      {"id": "S1", "initial_volume": 100, "concentration": 12.5},
      {"id": "S2", "initial_volume": 50, "markers": {"RFP": 8}}
    ],
    "transfers": [
      {"time": 1, "source": "S1", "target": "S2", "amount": 50},
      {"time": 1, "source": "S2", "target": "S1", "amount": 50},
      {"time": 2, "source": "S1", "target": "S2", "amount": 10}
    ]
  }' | python3 -m json.tool
```

前两条同刻转移组成批次 0（每步 `batch_index == 0`），均按批次开始快照计算，
互换两条顺序结果不变；若同批吸取量按源管汇总后超量，整批不落地并返回
`400`，错误下标仍是原 `transfers` 下标。

---

## 本地开发（不使用 Docker）

要求 Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000
```

数据库默认写入 `./data/pooling.db`，可用环境变量覆盖：

```bash
export POOLING_DB_PATH=/自定义/路径/pooling.db
```

运行测试：

```bash
pytest -q
```

---

## 物理模型与计算规则

1. **充分混匀假设**：每次转移前，源管内液体视为完全混匀，吸出液体携带源管
   当前的混合浓度与来源构成。
2. **吸 / 注分离与移液损耗**：每次转移的 `amount` 是从源管**吸出**（移出
   源管）的体积；可选 `delivered_amount` 是实际**进入目标管**的体积，要求
   `0 < delivered_amount ≤ amount`。差额 `amount - delivered_amount` 记为
   该步移液损耗 `lost`，损耗液的浓度、标记物质量与来源比例与吸出液一致，
   并累计到全程 `waste`。不传 `delivered_amount` 时按无损处理
   （`delivered_amount == amount`，旧请求完全兼容）。
3. **系统总量守恒（含 waste）**：
   `Σ 各管体积 + waste 体积`、`Σ 管内标记物质量 + waste 标记物质量`
   在每次转移前后都不变，对应结果中的
   `conservation.volume_difference` / `marker_amount_difference`（恒为 0）。
4. **管内口径单独给出**：仅看管内，总体积 / 总质量随损耗下降，下降量恰好
   等于累计 waste，对应 `tube_volume_change` / `tube_marker_amount_change`
   （每步）与全程同名字段。混合仍在“质量域”完成：目标管新浓度 =
   入管质量 / 入管后总体积。
5. **超量校验口径**：是否超量始终按吸出量 `amount` 与源管当前体积判断，
   与 `delivered_amount` 无关。
6. **来源追踪**：每支管记录“来自各初始样本的净体积”。吸出量按源管当前
   来源占比在“入管 / 损耗”两部分间分摊；目标管累加入管部分，waste 累加
   损耗部分。管内占比以管内体积为分母、waste 占比以累计损耗体积为分母，
   各自之和恒为 1（空集合除外）。
7. **浮点处理**：小于 `1e-12` 的残余量按 0 清理，结果保留 12 位小数。
8. **时间规则**：转移必须按时间**非递减**（允许同一时刻多次操作）。
9. **执行模式（原子批次）**：
   * `sequential`（默认）：按 `transfers` 数组顺序逐条执行，每步看到的是
     前一步落地后的管状态，与旧版完全一致；
   * `time_batch`：解析后 `time` 相等的**连续**转移合并为一批（时间为数值或
     ISO 8601 均可，按解析后的时间值判等）。批次执行遵循：
     - **先校验、后落地**：自转移、未知管、以及「按源管汇总的吸取总量
       `Σ amount`」超过该管**批次开始时**体积等任一错误，都会让整批作废，
       管状态与 waste 停留在批次之前；错误信息中的下标仍是原 `transfers`
       下标，已成功的更早批次照常保留；
     - **批次开始快照**：批内每条转移吸出液的浓度与来源构成都取自批次开始
       瞬间的源管状态，因此**同批结果与数组顺序无关**；
     - **统一写入**：校验通过后，源管按汇总吸取量扣减、目标管按 delivered
       汇总额增加、差额计入累计 waste。同一支管在批内既收又发时，发出按
       快照计算、收入正常累加，两个方向互不干扰；
     - 响应仍保留原 `transfers` 的步骤顺序，并为每步给出 `batch_index`
       （同批相同）；同批各步的 `source_after` / `target_after` 都是批次
       落地后的状态，`conservation` 给出批次级（before→after）守恒边界。

标记物支持两种写法，可混用：

* `concentration`: 单一标记物，内部命名为 `default`；
* `markers`: `{标记物名: 浓度}`，一次追踪多种标记物（如 GFP / RFP）。

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/simulations` | 提交一次模拟（成功 201，业务失败 400，格式错误 422） |
| `GET` | `/api/simulations/{id}` | 查询某次模拟的完整逐步结果（失败记录返回 400） |
| `GET` | `/api/simulations` | 历史记录列表，支持 `limit` / `offset` / `status` |
| `GET` | `/health` | 健康检查 |

### 请求示例

```bash
curl -s -X POST http://localhost:8000/api/simulations \
  -H "Content-Type: application/json" \
  -d '{
    "name": "含移液损耗的两管互混",
    "samples": [
      {"id": "S1", "initial_volume": 100, "concentration": 12.5},
      {"id": "S2", "initial_volume": 50,  "markers": {"RFP": 8}}
    ],
    "transfers": [
      {"time": "2026-09-18T09:00:00Z", "source": "S1", "target": "S2", "amount": 50, "delivered_amount": 48},
      {"time": "2026-09-18T10:30:00Z", "source": "S2", "target": "S1", "amount": 25}
    ]
  }'
```

第一步从 S1 吸出 `50` 但实际注入 S2 `48`，`2` 的差额按 S1 当时浓度
（`default=12.5`）计入该步 `lost` 并累计到 `waste`；第二步未传
`delivered_amount`，视为无损（`delivered_amount == 25`）。

`time` 既可以是 ISO 8601 字符串（`...Z` / 带时区偏移；无时区按 UTC），
也可以是数值序号（`1`、`2.5`），但同一请求中需保持可比较的递增顺序。

### 按时间批次原子执行（`execution_mode: "time_batch"`）

默认 `sequential` 模式下，同一时刻的多条转移仍按数组顺序逐条即时落地，因此
先写哪一条会影响中间结果（例如 A→B 排在 B→A 前面时，B 先收到液体再被吸出）。
当一组操作在实验上是**同一时刻并行发生**时，可将 `execution_mode` 设为
`time_batch`：解析后 `time` 相等的连续转移会被当作一个原子批次，所有吸取都
基于批次开始快照计算并先按源管汇总校验，再统一写入：

```bash
curl -s -X POST http://localhost:8000/api/simulations \
  -H "Content-Type: application/json" \
  -d '{
    "execution_mode": "time_batch",
    "samples": [
      {"id": "A", "initial_volume": 100, "concentration": 10},
      {"id": "B", "initial_volume": 50,  "markers": {"RFP": 8}}
    ],
    "transfers": [
      {"time": 1, "source": "A", "target": "B", "amount": 60, "delivered_amount": 55},
      {"time": 1, "source": "B", "target": "A", "amount": 50, "delivered_amount": 45},
      {"time": 2, "source": "A", "target": "B", "amount": 10}
    ]
  }'
```

要点：

* 前两条 `time` 相同，组成批次 0（`batch_index` 均为 `0`）；第三条是批次 1
  （`batch_index` 为 `1`）。把前两条在数组里互换顺序，终态与守恒结果**完全
  不变**；
* 批次 0 中 A 按开始时的浓度 `10` 被吸出 60（不关心它同时从 B 收入 45），
  B 按开始时的 RFP 浓度被吸出 50；落地后 A 为 `100-60+45=85`、
  B 为 `50-50+55=55`，损耗合计 `(60-55)+(50-45)=10` 计入 waste；
* 若同批两条都从 A 吸取且**合计**超过批次开始时 A 的体积（如 70+50>100），
  返回 400，`error.location` 指向原下标（如 `transfers[0].amount`），整批
  回滚（无步骤、waste 不增加）；更早的批次结果保留；
* `sequential` 模式（默认或显式指定）下每步自成一批，
  `batch_index == index`，旧请求与旧响应完全兼容。

### 响应结构（节选）

```jsonc
{
  "id": "551a00db...",
  "status": "success",            // 或 "failed"
  "execution_mode": "sequential", // sequential（默认）或 time_batch
  "created_at": "2026-09-18T15:24:11Z",
  "initial_states": [ /* 每支管的初始体积/浓度/来源 */ ],
  "steps": [
    {
      "index": 0,
      "batch_index": 0,           // 所属批次：sequential 下等于 index；同批共享
      "time": "2026-09-18T09:00:00Z",
      "source": "S1", "target": "S2",
      "amount": 50.0,              // 从源管吸出的体积
      "delivered_amount": 48.0,    // 实际进入目标管的体积（缺省 = amount）
      "transferred": {             // 入管液体（48）的浓度与来源构成
        "volume": 48.0,
        "concentrations": {"RFP": 0.0, "default": 12.5},
        "origins": [{"source_id": "S1", "volume": 48.0, "volume_fraction": 1.0}]
      },
      "lost": {                    // 本步移液损耗（50 - 48 = 2）
        "volume": 2.0,
        "concentrations": {"RFP": 0.0, "default": 12.5},
        "marker_amount": {"RFP": 0.0, "default": 25.0},
        "origins": [{"source_id": "S1", "volume": 2.0, "volume_fraction": 1.0}]
      },
      "source_after": { /* 按吸出量 50 扣减：S1 剩 50 */ },
      "target_after": { /* 按入管量 48 增加：S2 变为 98 */ },
      "conservation": {
        // before/after 为“所有管内 + 截至本步累计 waste”的系统口径
        "volume_before": 150.0, "volume_after": 150.0,
        "volume_difference": 0.0,            // 系统总量差：恒为 0
        "tube_volume_change": -2.0,          // 仅看管内的体积变化
        "marker_amount_before": {"RFP": 400.0, "default": 1250.0},
        "marker_amount_after":  {"RFP": 400.0, "default": 1250.0},
        "marker_amount_difference": {"RFP": 0.0, "default": 0.0},
        "tube_marker_amount_change": {"RFP": 0.0, "default": -25.0}
      }
    },
    { /* index 1：无损步，delivered_amount == amount == 25，lost.volume == 0 */ }
  ],
  "final_states": [ /* 终态：S1=75、S2=73（管内合计 148） */ ],
  "waste": {                         // 全程累计移液损耗（各步 lost 之和）
    "volume": 2.0,
    "marker_amount": {"RFP": 0.0, "default": 25.0},
    "origins": [{"source_id": "S1", "volume": 2.0, "volume_fraction": 1.0}]
  },
  "conservation": {
    "initial_total_volume": 150.0,
    "final_total_volume": 150.0,     // 系统口径：管内 148 + waste 2
    "volume_difference": 0.0,        // 系统总量差
    "tube_volume_change": -2.0,      // 管内口径变化 = -waste 体积
    "initial_marker_amount": {"RFP": 400.0, "default": 1250.0},
    "final_marker_amount":  {"RFP": 400.0, "default": 1250.0},
    "marker_amount_difference": {"RFP": 0.0, "default": 0.0},
    "tube_marker_amount_change": {"RFP": 0.0, "default": -25.0}
  },
  "error": null
}
```

旧请求不传 `delivered_amount` 时，每步 `delivered_amount` 回填为 `amount`、
`lost.volume` 为 `0`、`origins` 为空，顶层 `waste` 同样为零，
`tube_*_change` 与系统差值均为 `0`，原有字段语义不变。

### 错误响应（定位明确）

业务错误返回 `400`，响应体其余字段（已完成步骤、当时各管状态）仍可用于排查；
记录同样保存，可用返回的 `id` 通过 `GET` 复查。

```json
{
  "status": "failed",
  "error": {
    "code": "INSUFFICIENT_VOLUME",
    "message": "transfers[0] 试图从 'A' 吸取 99.0，但该管当前仅有 5.0",
    "transfer_index": 0,
    "location": "transfers[0].amount"
  },
  "...": "initial_states / steps / final_states / conservation 照常返回"
}
```

错误码：

| code | 触发条件 | location 示例 |
| --- | --- | --- |
| `INSUFFICIENT_VOLUME` | 转移量超过源管当前体积（time_batch 下为同批按源管汇总的吸取总量超过批次开始体积） | `transfers[i].amount` |
| `TIME_NOT_ORDERED` | 转移时间早于上一次转移 | `transfers[i].time` |
| `UNKNOWN_SAMPLE` | 源 / 目标管未在 `samples` 中声明 | `transfers[i].source` / `.target` |
| `SAME_SOURCE_TARGET` | 源管与目标管相同 | `transfers[i]` |
| `SIMULATION_NOT_FOUND` | 查询不存在的记录 id | `path.sim_id` |

请求体本身的字段问题（负数体积、缺少浓度、未知字段、样本 id 重复、
`delivered_amount` 非正数或大于 `amount`、`execution_mode` 不是
`sequential` / `time_batch` 等）返回 FastAPI 标准 `422` 结构，
`detail[].loc` 指明出错字段路径。

### 查询与列表

```bash
# 完整结果
curl http://localhost:8000/api/simulations/<id>

# 列表（按创建时间倒序）
curl "http://localhost:8000/api/simulations?limit=20&offset=0"
curl "http://localhost:8000/api/simulations?status=failed"
```

---

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由（创建 / 查询 / 列表 / 健康检查）
│   ├── models.py      # Pydantic 请求与响应模型、时间解析、ExecutionMode
│   ├── engine.py      # 混样模拟核心（浓度 / 来源 / 移液损耗 / 守恒 / 原子批次）
│   └── database.py    # SQLite 连接与记录持久化
├── tests/
│   ├── conftest.py
│   ├── test_engine.py # 引擎计算、移液损耗 / 来源 / 守恒、time_batch 原子批次与错误定位
│   └── test_api.py    # API 端到端（21 个用例，含损耗追踪、执行模式与持久化）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

## 设计说明

* **无外部依赖**：仅用 Python 标准库自带的 SQLite，开箱即用；启用 WAL 与
  busy timeout 以兼容多 worker 部署。
* **失败也持久化**：成功和失败的模拟都落库，失败记录保留已执行步骤与
  现场状态，方便审计与复盘。
* **浓度统一在质量域计算**：避免反复稀释 / 混合引入的累积误差；
  `conservation.*_difference`（含 waste 的系统口径）恒为 0，可直接用于
  自动校验，`tube_*_change` 则给出仅管内口径的下降量，便于核对损耗账。
* **安全默认值**：容器以非 root 用户运行，附带 healthcheck；请求模型
  禁止额外字段（`extra=forbid`），防止拼写错误被静默忽略。
