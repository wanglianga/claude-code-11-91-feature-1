# 充电预约演示系统（Vue 3 + FastAPI + PostgreSQL）

司机按「站点 + 时间窗 + 停留时长 + 最低功率」预约充电，运营方维护枪口并评估对既有预约的影响。
系统同时支持两种预约粒度，并以同一套容量数据给出可核验结论：

- **固定枪口（CONNECTOR）**：确认时锁定一支兼容枪口，位置承诺直观；枪口故障时需迁移订单。
- **资源池（POOL）**：只承诺一个满足插头/功率/无障碍条件的站点名额，到站再分配枪口；
  以「池内同时可履约的枪口数」限制订单，绝不会因为两支枪各自「空闲」就把同一份容量卖两次。

在此之上支持**车队批次预约**：车队经理用带车队编号的一次批次请求登记多辆车的
「接口 / 最低目标电量 / 到站范围 / 出发时刻」，站点确认后生成可追溯的子预约，
车队视图按**最低就绪数**显示就绪进度、未满足车辆及原因；子预约取消、枪口失能或
临时换车时批次立即重算，已宣布的车队就绪可以被撤回。批次提供两条确认路线：

- **逐车确认（GRADUAL）**：逐车确认可用时段并累计批次进度，准备较早的车辆先收到
  导航与进场码；但任何一笔单车确认都不等于车队就绪，一笔取消可能让已宣布的就绪重新不足。
- **原子组合（ATOMIC）**：先在保存点内求出达到最低就绪数的一组兼容预约，再一次提交
  确认；达不到最低数量时整组回滚、零容量占用，杜绝把部分成功误作批次承诺，代价是
  所有司机要等完整组合算出后才收到结果。

## 一、目录结构

```
.
├── docker-compose.yml          # 一键启动 db + backend + frontend
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── e2e_check.sh            # 题面场景手工 E2E（36 项检查，含车队 F-8）
│   ├── app/
│   │   ├── main.py             # FastAPI 入口（含 /docs Swagger UI）
│   │   ├── models.py           # 车辆/站点/枪口/维护窗口/预约/事件/客服动作/幂等记录/车队批次
│   │   ├── enums.py
│   │   ├── schemas.py
│   │   ├── deps.py             # 幂等端点（事务咨询锁 + 首次响应留存与回放）
│   │   ├── seed.py             # 东站四枪 + 北站 F-8 车队场景等样例数据
│   │   ├── bootstrap.py        # 容器启动：建表，空库才灌种子（可重复执行）
│   │   ├── services/
│   │   │   ├── capacity.py     # 时间切片容量引擎（两种粒度共用）
│   │   │   ├── reservation.py  # 确认/取消/迟到/到站/开始充电 状态机
│   │   │   ├── maintenance.py  # 降额/停用/恢复 + 影响判定 + 客服动作
│   │   │   └── fleet.py        # 车队批次：建档/逐车确认/原子组合/重算/换车/就绪通知
│   │   └── routers/            # resources / search / reservations / operations / fleet
│   └── tests/
│       ├── test_scenarios.py   # 20 个基础 pytest 场景用例
│       └── test_fleet.py       # 13 个车队批次 pytest 场景用例
└── frontend/
    ├── Dockerfile              # 多阶段构建：vite build → nginx 静态托管 + API 代理
    ├── nginx.conf
    └── src/
        ├── views/DriverView.vue       # 司机端
        ├── views/FleetView.vue        # 车队批次视图（建档/逐车与原子确认/重算/事件流）
        └── views/OperationsView.vue   # 运营端
```

## 二、一键启动（Docker，推荐）

前置：本机已安装 Docker 与 Docker Compose。

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| 前端（司机端 / 车队批次 / 运营端三个页签） | http://localhost:8080 |
| 后端 API | http://localhost:8000 |
| Swagger UI（接口文档） | http://localhost:8000/docs |
| PostgreSQL | localhost:5432，库/用户/密码均为 `ccdev` |

后端容器启动时会自动建表，并**仅在空库时**灌入样例数据；重复重启不会清空已有数据。
需要恢复初始演示数据时：

```bash
docker compose exec backend python -m app.seed
```

停止并清理数据卷：

```bash
docker compose down -v
```

## 三、本地开发启动（不使用 Docker）

容器内已预装 Python 3.12 / Node 22 / PostgreSQL 18，下面的命令也适用于普通开发机
（自行准备一个可连接的 PostgreSQL 即可）。

### 1. 后端

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 指向你的 PostgreSQL，默认 postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev
export DATABASE_URL="postgresql+psycopg://用户:密码@主机:5432/库名"
.venv/bin/python -m app.seed           # 重置并灌入样例数据
.venv/bin/uvicorn app.main:app --reload --port 8000
```

### 2. 前端

```bash
cd frontend
npm install
npm run dev      # http://localhost:8080，/api 已代理到 127.0.0.1:8000
```

## 四、测试数据与「账号」说明

本演示系统不设登录认证（无账号体系），打开页面即可操作，预置数据如下，
相当于固定的测试入口：

- **站点**：东站充电站（id=1），四支枪口
  - **A**：CCS / 120kW / 无障碍 —— 已被有效预约 `R-SEEDA01` 在周五 18:00–18:30 占用
  - **B**：CCS / 120kW / 无障碍 —— 18:00–18:10 唯一剩余的 90kW 合格容量
  - **C**：CCS / **60kW** / 普通车位 —— 功率不足
  - **D**：**CHAdeMO** / 120kW —— 与 CCS 车辆接口不兼容
- **车辆**：
  - V1：CCS / 120kW（持有 A 枪预约的车主）
  - V2：CCS / **最大受电仅 60kW**
  - V3：CCS / 100kW / **有无障碍接入要求**
  - V4：CCS / 150kW（发起 90kW 请求的第二位车主）
  - V5：CCS / 60kW（个人用户，持有北站 N3 枪预约）
  - V6/V7：CCS / 120kW（F-8 的 A、B 车）；V8：CCS / 60kW（F-8 的备用 C 车）
- **北站（id=2）与预置车队批次 F-8**（凌晨出车场景）：
  - **N1**：CCS / 120kW，**05:30 起停用**；**N2**：CCS / 120kW，**05:30 前停用**
    → 05:00–05:30 全程只有 **N1 一支 120kW 枪**
  - **N3**：CCS / **60kW**，06:00 起停用；个人预约 `R-SEEDN01` 已占用 N3 的 05:00–06:00
  - 批次 **F-8**：逐车确认路线、**最低就绪数 2**；A/B 需 120kW（到站 05:00–05:30），
    C 可接受 60kW（到站 05:00–06:00），出发时刻均为 06:00
- 所有演示时间固定在 **2026-11-27（周五）**，前端表单已预填 18:00 / 05:00。

### 车队接口一览（均在 `/docs` Swagger UI 可见）

| 接口 | 说明 |
| --- | --- |
| `POST /api/fleet/batches` | 带车队编号建档（fleet_no 唯一、min_ready ≤ 车辆数，整体幂等） |
| `GET /api/fleet/batches` / `/{id}` | 车队视图：就绪计数、deficit、未满足车辆及原因、事件流 |
| `POST /api/fleet/batches/{id}/items/{item_id}/confirm` | 逐车确认（GRADUAL），成功即发导航/进场码 |
| `POST /api/fleet/batches/{id}/atomic-confirm` | 原子组合求解（ATOMIC），达标整体落库、不足整组回滚零占用 |
| `POST /api/fleet/batches/{id}/items/{item_id}/replan` | 临时换车：作废旧子预约、改写需求并立即重算 |

## 五、本题验证方式（均已实际执行）

### 1. 后端单元/场景测试：33 项全部通过

```bash
cd backend
.venv/bin/python -m pytest tests/ -q
# 结果：33 passed（test_scenarios.py 20 项 + test_fleet.py 13 项）
```

`test_fleet.py` 覆盖：F-8 初始未就绪与未满足清单、逐车路线“单车确认不伪装车队就绪”、
N1/N3 争抢下 B/C 被拒且界面保持未达 2 辆、释放 N3 后达标并发就绪通知、取消后就绪撤回、
逐车确认成功/失败两类幂等且不多占容量、原子组合失败整组回滚零占用与重放、
原子成功后通知统一签发、维护停用触发即时重算、客服改派救回就绪、临时换车作废旧预约、
重复车队编号/路线守卫、无 vehicle_id 自动建档、子预约与批次行的可追溯关联。

### 2. 题面场景 E2E：36 项检查全部通过

先重置数据，再启动后端，然后：

```bash
cd backend
.venv/bin/python -m app.seed
.venv/bin/uvicorn app.main:app --port 8000 &
bash e2e_check.sh
# 结果：通过 36 项，失败 0 项
```

第 9–13 段针对车队 F-8 用真实 HTTP 请求验证了题面要求的关键结论：

1. A 车逐车确认成功只计 1 辆，批次保持 `OPEN`、**没有车队就绪事件**，但 A 已拿到进场码；
   同键重放返回同一子预约，不产生新订单；
2. N1 是 05:00–05:30 唯一 120kW 枪且已被 A 锁定、N3 被个人用户占用：B、C 确认均 `REJECTED`，
   `ready_declared=false`，未满足清单含 B/C 两车——**界面必须保持“未达 2 辆”，不发车队就绪通知**；
3. 个人用户取消预约释放 N3 后，C 再确认成功，就绪数达到 2 才产生 `FLEET_READY` 通知；
4. 取消 A 的子预约后就绪数跌回 1，产生 `FLEET_READY_WITHDRAWN`——**一笔取消使已宣布的就绪重新不足**；
5. 原子组合路线在容量不足时返回 `REJECTED`，预约总数前后不变（零容量占用），重放仍是同一结果。

### 3. 前端手工验证

`npm run dev` 后访问 http://localhost:8080，切到「车队批次」页签：

- 用预置的 F-8 批次逐一点确认：观察 B/C 被拒原因、就绪计数始终 `1/2`、未满足车辆表；
- 在「运营端」取消个人预约 `R-SEEDN01`（或直接在车队页取消子预约）后回到车队页，
  观察 C 可确认、状态变「车队已就绪」、进场码与事件流；再取消 A 的子预约观察就绪撤回；
- 新建 `ATOMIC` 批次点「原子求解」：容量不足时整组被拒且无新预约，释放容量后再次求解，
  两笔子预约一次性出现、司机通知统一签发。

## 六、关键设计与约束的落实

- **候选搜索不构成订单**：`POST /api/search` 为纯只读；真正占用容量只发生在确认事务里。
- **事务内重算 + 并发安全**：确认先取站点级 `pg_advisory_xact_lock`，
  在同一事务内重新计算所有重叠切片后落单；并发请求被串行化，同一份能力不可能卖两次。
- **池容量不重复售卖**：每个时间切片统计「合格且未锁定的枪数 − 在途池预约数」，
  全程取最小值作为可确认名额。
- **幂等**：每个写操作携带 `idempotency_key`；首次请求（含 409 业务失败）的响应码与
  响应体被持久化，重复提交原样返回，同键不同参数返回 409。
- **状态机**：`CONFIRMED → ARRIVED → CHARGING → COMPLETED`，
  以及 `CANCELLED / NO_SHOW`（释放容量）、`NEEDS_RESELECTION / MANUAL_CONTACT`
  （维护影响）；非法转换返回 409。池模式在「到站」时才分配具体枪口。
- **维护按真实交集判定**：维护窗口与预约按半开区间求交集，交集为空则完全不影响；
  整晚订单遇到中途降额时，已履约区间与待履约区间在原因中分别说明。
  仍满足车辆需求的订单继续履约，不满足的进入改选/人工联系。
- **充电会话保护**：`CHARGING` 订单不能被取消、不能被客服替代枪口流程迁移，
  维护只会给出 `PROTECTED_CHARGING` 判定。
- **历史不可改写**：确认时写入承诺快照（枪号/功率/池名额），之后降额、恢复、
  迁移都不会修改它；客服动作固化当时的订单状态与原枪/目标枪设备快照；
  设备恢复只结束维护窗口并追加 `DEVICE_RECOVERED` 事件，不自动回滚订单状态。

### 车队批次专项约束的落实

- **单车确认不伪装车队就绪**：批次就绪数只统计持有有效容量（HOLDING 状态）的子预约，
  达到 `min_ready` 才写 `FLEET_READY`；逐车确认成功时只发该车的 `DRIVER_NOTIFY`。
- **就绪可撤回**：`ready_declared/ready_at` 随重算变化，任一子预约取消、迟到、
  维护改选导致就绪数跌破最低线时，立即产生 `FLEET_READY_WITHDRAWN` 并清空宣布时刻。
- **原子路线无部分成功**：整组试算在独立保存点内进行，数量不足即回滚到试算前，
  失败原因与 `ATOMIC_SOLVE_FAILED` 事件落外层事务，预约总数零增长；成功才统一签发通知。
- **重复提交不多占容量**：建档、逐车确认、原子求解、换车全部携带幂等键并复用统一
  幂等框架；同键重放（含失败结果）原样返回，同车队编号重复建档返回 409。
- **三类变更即时重算**：取消/迟到（预约状态机钩子）、枪口失能（维护影响钩子）、
  客服救回与临时换车（客服/换车钩子）都在同一请求事务内调用 `reconcile_batch` 对账。
- **全程可追溯**：子预约带 `fleet_batch_item_id`，批次行保留需求快照、当前子预约指针、
  进场码与完整事件流；换车/取消后历史预约仍可经事件流与外键回溯。
