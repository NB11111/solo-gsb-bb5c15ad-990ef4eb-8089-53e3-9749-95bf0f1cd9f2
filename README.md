# 实验室样本混样追踪 API

基于 **Python + FastAPI + SQLite** 的实验室样本混样（pooling）追踪服务。
客户端提交初始样本（体积 + 标记物浓度）与按时间排序的转移操作，服务在
**系统总量守恒（所有管内 + 累计移液损耗 waste）** 的前提下，逐步计算每次转移后：

* 每支管的**当前体积**与各标记物的**混合浓度**；
* 管内液体的**来源构成与占比**（来自哪些初始样本、各占多少体积 / 百分比）；
* 可选 `delivered_amount` 时的**移液损耗追踪**：吸出量 `amount` 与实际
  入管量 `delivered_amount` 的差额按源管当时浓度、来源比例计入该步 `lost`，
  并累计为结果 `waste`；
* 两种执行模式：默认 `sequential`（逐条顺序执行）；`time_batch` 把同一时刻
  的连续转移视为**一批原子操作**——吸取全部基于批次开始快照、先按源管汇总
  校验可用量、再统一写入，同批结果与数组顺序无关；
* 两个口径的守恒量：含 waste 的**系统总量差**（应恒为 0）与仅看管内的
  **管内体积 / 质量变化**（恰好等于累计损耗）。

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

更新代码后重新构建并启动（镜像名 `sample-pooling-api:latest`，无需外部
镜像仓库）：

```bash
docker compose up --build
```

如需确认容器内运行的新版本，可查看自动生成的 OpenAPI 文档：
`http://localhost:8000/openapi.json`（其中 `info.version` 随服务版本更新，
`SimulationRequest.execution_mode` 与 `StepOut.batch_index` 均已包含在
schema 中），交互式文档在 `/docs`、`/redoc`。

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

### 启动后的示例（time_batch 原子批次）

`execution_mode` 不传时默认 `sequential`，行为与旧版完全一致。设为
`time_batch` 后，解析后 `time` 相等的连续转移合成一批：所有吸取都基于批次
开始时的源管快照计算，因此同一时刻的双向转移与数组顺序无关；只有先按源管
汇总 `amount` 校验全部可用后，才统一写入目标管与 waste。

```bash
curl -s -X POST http://localhost:8000/api/simulations \
  -H "Content-Type: application/json" \
  -d '{
    "name": "同时刻双向转移-原子批次",
    "execution_mode": "time_batch",
    "samples": [
      {"id": "S1", "initial_volume": 10, "concentration": 10},
      {"id": "S2", "initial_volume": 10, "concentration": 0}
    ],
    "transfers": [
      {"time": 1, "source": "S1", "target": "S2", "amount": 6},
      {"time": 1, "source": "S2", "target": "S1", "amount": 6}
    ]
  }' | python3 -m json.tool
```

预期要点：两个步骤的 `batch_index` 都为 `0`；每步的 `source_after` /
`target_after` / `conservation` 均为整批执行后的视图。批次内一吸一收后
两管体积都仍是 10，S1 浓度变为 4.0（剩余质量 40 / 体积 10），S2 变为 6.0；
交换两条 transfers 的顺序结果完全相同。若把两条 `S1 -> S2` 的吸取量写成
各 6（合计 12 > 10），返回 400，`error.transfer_index` 定位到
`transfers[1]`，整批不落地、`steps` 为空；之前批次（更小 time）的结果保留。

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
9. **执行模式 `execution_mode`**：
   * `sequential`（默认）：逐条顺序执行，每条转移自成一批
     （`batch_index == index`），与旧版行为完全一致；
   * `time_batch`：解析后 `time` 相等的连续转移合成**一批原子操作**。
     * 批次内所有吸取都基于**批次开始快照**计算浓度与来源构成，同批
       计算结果与数组顺序无关（同一支管同时收、发也互不影响）；
     * 先按源管汇总本批 `amount` 与批次开始可用量比较，任一源管累计
       超量则**整批不落地**，错误定位到原 `transfers` 下标（首个导致
       累计超量的操作，`location` 形如 `transfers[i].amount`）；
     * 校验通过后再把全部源管减量、目标管增量与 waste 增量**统一写入**；
     * 同批各步的 `source_after` / `target_after` / `conservation`
       都是整批执行后的视图，`batch_index` 相同；守恒以批次为单位，
       覆盖多源、多目标、同管收发及 `delivered_amount` 损耗；
     * 某批失败时仅该批不落地，之前批次已提交的步骤与状态保留
       （`TIME_NOT_ORDERED` / `UNKNOWN_SAMPLE` / `SAME_SOURCE_TARGET`
       同理，下标均指向原 transfers 序列）。

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
    "execution_mode": "sequential",
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

顶层可选字段 `execution_mode`：

| 取值 | 语义 |
| --- | --- |
| `sequential`（默认，缺省即此值） | 按数组顺序逐条执行，每条转移一个批次（`batch_index == index`） |
| `time_batch` | 解析后 `time` 相等的连续转移合为一批原子执行：吸取基于批次开始快照、按源管汇总 `amount` 校验后统一写入；同批每步都带相同 `batch_index` |

time_batch 下若同批对同一源管的合计吸取量超过其批次开始体积（即使每条
单独看不超量），整批返回 400 且不落地：

```json
{
  "status": "failed",
  "error": {
    "code": "INSUFFICIENT_VOLUME",
    "message": "transfers[1] 使批次（batch 起始于 transfers[0]，共 2 个同时刻操作）从 'A' 的累计吸取量达到 12.0，但该管批次开始时仅有 10.0",
    "transfer_index": 1,
    "location": "transfers[1].amount"
  }
}
```

第一步从 S1 吸出 `50` 但实际注入 S2 `48`，`2` 的差额按 S1 当时浓度
（`default=12.5`）计入该步 `lost` 并累计到 `waste`；第二步未传
`delivered_amount`，视为无损（`delivered_amount == 25`）。

`time` 既可以是 ISO 8601 字符串（`...Z` / 带时区偏移；无时区按 UTC），
也可以是数值序号（`1`、`2.5`），但同一请求中需保持可比较的递增顺序。

### 响应结构（节选）

```jsonc
{
  "id": "551a00db...",
  "status": "success",            // 或 "failed"
  "created_at": "2026-09-18T15:24:11Z",
  "initial_states": [ /* 每支管的初始体积/浓度/来源 */ ],
  "steps": [
    {
      "index": 0,
      "batch_index": 0,            // 所属批次；time_batch 下同批多步共享
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
        // before/after 为“所有管内 + 截至本步累计 waste”的系统口径；
        // time_batch 下以整批为单位，同批各步的该字段相同
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

不传 `execution_mode`（或显式传 `sequential`）时，每个 `index` 自成一批、
`batch_index == index`，逐步的 `source_after` / `target_after` /
`conservation` 仍为该步执行后的视图，与旧版逐条顺序执行的结果逐字段一致。
`time_batch` 下，同批每步的这三项均为**整批执行后**的统一视图，而
`transferred` / `lost` / `amount` / `delivered_amount` 仍只描述该操作本身。

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
| `INSUFFICIENT_VOLUME` | 转移量超过源管可用体积（time_batch 下为批次内按源管汇总的累计吸取量超过批次开始体积，定位到首个超量操作） | `transfers[i].amount` |
| `TIME_NOT_ORDERED` | 转移时间早于上一批转移 | `transfers[i].time` |
| `UNKNOWN_SAMPLE` | 源 / 目标管未在 `samples` 中声明 | `transfers[i].source` / `.target` |
| `SAME_SOURCE_TARGET` | 源管与目标管相同 | `transfers[i]` |
| `SIMULATION_NOT_FOUND` | 查询不存在的记录 id | `path.sim_id` |

请求体本身的字段问题（负数体积、缺少浓度、未知字段、样本 id 重复、
`delivered_amount` 非正数或大于 `amount`、`execution_mode` 取值不在
`sequential` / `time_batch` 中等）返回 FastAPI 标准 `422` 结构，
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
│   ├── models.py      # Pydantic 请求与响应模型、时间解析
│   ├── engine.py      # 混样模拟核心（浓度 / 来源 / 移液损耗 / 守恒）
│   └── database.py    # SQLite 连接与记录持久化
├── tests/
│   ├── conftest.py
│   ├── test_engine.py # 引擎计算、移液损耗 / 来源 / 守恒、time_batch 原子批次与错误定位
│   └── test_api.py    # API 端到端（含损耗追踪、time_batch / OpenAPI 与持久化）
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
