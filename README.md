# 设备诊断双代理熔断器（FastAPI + Redis）

机房场景演练：设备诊断后端连续超时，两台代理**一起停止转发**；后端恢复后，
经半开探测确认才重新放行。所有熔断状态放在 Redis 中由两个代理共享，
状态判断与切换全部通过 Lua 脚本原子完成。

本版本在基线之上修复了**探测归属**缺陷：

- 两台代理共享的每份有效探测权（带 TTL 的租约）**只放行一条真实请求**；
  同一代理在探测在途时的其它并发请求也会被拒绝（原缺陷：同代理重入被当成
  第二个探测直接放行）。
- 租约到期后可由任意代理接手；接手时代数 +1。**只有当前探测的结果**能决定
  恢复或重新冷却，旧探测迟到的成功/失败一律忽略，也**不会删除接手者的探测权**
  （原缺陷：旧探测成功会提前恢复转发并清掉接手者的租约）。

## 目录结构

```
.
├── docker-compose.yml      # 5 个服务：redis / backend / proxy-a / proxy-b / web
├── Dockerfile              # 通用 FastAPI 镜像（构建参数 APP_DIR 区分服务）
├── redis/redis.conf        # AOF 持久化（appendfsync always），重启不丢熔断状态
├── proxy/                  # 诊断代理（同一份代码，两个实例 PROXY_ID 不同）
│   └── app/
│       ├── breaker.py            # Redis 熔断客户端
│       ├── preflight.lua         # 预检状态机（放行 / 冷却拒绝 / 唯一探测租约发放）
│       ├── report_result.lua     # 结果上报状态机（代数 + 探测归属校验）
│       └── main.py               # /diagnose/{设备号}、/cb/status、/cb/reset、/health
├── backend/app/main.py     # 模拟诊断后端：normal 立即返回 / timeout 挂起并返回 504
├── web/app/main.py         # 操作网页 + 聚合 API（/api/overview、/api/diagnose ...）
└── tests/                  # Lua 单测 + 端到端 HTTP 测试 + 一键脚本
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开操作网页
open http://localhost:8080        # macOS；Linux 用浏览器访问 http://localhost:8080
```

端口映射：

| 服务      | 地址                  | 说明                     |
|-----------|-----------------------|--------------------------|
| web       | http://localhost:8080 | 运维操作网页             |
| proxy-a   | http://localhost:8081 | 代理 A（可直接 curl）    |
| proxy-b   | http://localhost:8082 | 代理 B                   |
| backend   | 仅 compose 内网       | 模拟诊断后端             |
| redis     | 仅 compose 内网       | 共享熔断状态，AOF 持久化 |

网页功能：

- 输入设备号、选择任一代理发起**真实诊断**，页面直接展示后端返回的真实
  诊断数据（设备号、诊断时间、CPU 温度 / 内存 / 磁盘占用、自检项）或后端
  错误原因；被熔断拒绝时展示 `reason`（`COOLDOWN` / `PROBE_IN_PROGRESS` /
  `REDIS_DOWN`）、熔断状态与代数。
- **同代理并发 3 次**按钮：让探测在途 1 秒，可直观看到只有 1 条真实请求
  被放行、其余 2 条以 `PROBE_IN_PROGRESS` 拒绝。
- 一键切换后端 `正常 / 超时`；实时（1s 刷新）查看全局熔断状态、连续超时
  计数、冷却剩余、探测归属与租约剩余、两代理各自的 Redis 连通性；两台代理
  视图不一致或全部不可用时页面显式告警。提供熔断复位按钮（管理操作）。

## 熔断语义（与需求逐条对应）

1. **合计连续 3 次超时即熔断**：失败计数在 Redis 中共享，两个代理的超时
   合并计数（`failures` 达到 `FAILURE_THRESHOLD`，默认 3 → `OPEN`）；任一成功
   会清零计数。
2. **熔断后快速拒绝**：`OPEN` 冷却期内请求直接返回 `503 reason=COOLDOWN`，
   不与后端建立连接（实测毫秒级返回）。
3. **冷却 5 秒后半开只放一个探测**：冷却到期后第一个请求成为唯一探测
   （`HALF_OPEN`），探测权写入带 10s TTL 的租约 key。**每份有效租约只放行
   一条真实请求**：其它代理、以及探测持有者自己的其它并发请求，一律返回
   `503 reason=PROBE_IN_PROGRESS`。
4. **探测成功才恢复**：当前探测成功 → `CLOSED`、计数清零；失败 → 重新
   `OPEN` 并重新计时 5 秒冷却。
5. **探测超时 / 代理停顿或退出后仍能恢复**：探测权是带 TTL 的租约；探测
   在途停顿超过租约后，key 自动过期，下一个请求（任意代理）可接手并重新
   发放租约。接手时 `generation + 1`，失效的旧探测不会卡死恢复流程。
6. **只有当前探测能决定状态**：请求预检时记录 `generation`，上报时 Lua 校验
   代数一致，且半开期还校验 `probe_owner` 与当前租约持有者一致；旧探测迟到
   的成功/失败都 `applied=false`，既不能提前恢复转发，也**不会清除接手者的
   探测权**。
7. **代理 / Redis 重启不提前放行**：代理无本地状态，重启后从 Redis 读取
   同一状态继续冷却；Redis 开启 AOF（`appendfsync always`），重启后
   `OPEN/opened_at/generation` 原样恢复。
8. **Redis 不可用明确拒绝**：代理对 Redis 设置 1s 连接/读写超时，任何
   Redis 异常都返回 `503 reason=REDIS_DOWN`，绝不绕过熔断直接转发；
   Redis 恢复后代理自动自愈。

### HTTP 接口速览

```bash
# 代理
GET  /diagnose/{device_id}   # 诊断（先过熔断再转发）；?delay=N 仅供自测制造慢响应
GET  /cb/status              # 熔断状态视图（含探测租约剩余毫秒）
POST /cb/reset               # 管理复位
GET  /health                 # 含 redis_ok 连通性

# 模拟后端
GET  /admin/mode             # 当前 normal / timeout
POST /admin/mode             # 切换模式 {"mode":"timeout"}
GET  /diagnose/{device_id}?delay=3   # delay 仅供自测制造慢响应

# 网页后端
GET  /api/overview                # 双代理 + 后端模式 + 共享熔断状态聚合
POST /api/diagnose                # {"device_id":"DEV-1","proxy":"a"|"b"}
POST /api/diagnose/concurrent     # {"device_id":"DEV-1","proxy":"a","count":3,"delay":1.0}
POST /api/backend/mode            # {"mode":"normal"|"timeout"}
POST /api/reset                   # 复位熔断器
```

诊断响应关键字段：`ok` / `rejected` / `reason` / `is_probe` /
`backend_latency_ms` / `cb_result_applied`（本次结果是否被状态机采纳）/
`cb`（应用后的状态、代数、失败计数）/ `backend`（后端真实诊断数据或错误）。

## 自测

自动化测试共 19 项：12 项 Lua 状态机单测 + 7 项端到端 HTTP 测试。

### Docker Compose 环境

```bash
docker compose run --rm --build \
  -e REDIS_URL=redis://redis:6379/0 \
  -v "$PWD:/work" -w /work proxy \
  sh -c "pip install pytest && pytest tests/test_breaker_lua.py tests/test_e2e_http.py -v"
```
（端到端测试会自行拉起 backend/proxy 子进程，端口 18000-18002 需空闲；
自测环境把冷却/租约缩短为 1.5s / 2.5s，语义与默认 5s / 10s 一致。）

手动核对重启语义：

```bash
docker compose restart proxy-a      # 冷却期重启代理：仍 COOLDOWN，不提前放行
docker compose restart redis        # AOF 恢复：OPEN 状态与冷却计时原样保留
docker compose stop redis           # 期间诊断返回 503 REDIS_DOWN，恢复后自愈
```

### 本地直跑

需要一个本机 Redis 与已安装 `requirements.txt` 的 Python：

```bash
REDIS_URL=redis://127.0.0.1:6379/0 tests/self_test.sh

# 手工查看网页（另开一个终端；无需 Docker）
REDIS_URL=redis://127.0.0.1:6379/0 tests/start_local.sh   # http://127.0.0.1:18180
tests/stop_local.sh
```

## 关键参数

| 参数                | 位置 / 环境变量        | 默认值 | 说明                          |
|---------------------|------------------------|--------|-------------------------------|
| 失败阈值            | `FAILURE_THRESHOLD`    | 3      | 两代理合计连续超时次数        |
| 冷却时长            | `COOLDOWN_MS`          | 5000ms | OPEN → HALF_OPEN 的等待时间   |
| 探测租约 TTL        | `PROBE_TTL_MS`         | 10000ms| 探测在途停顿/代理退出后自动释放 |
| 代理后端超时        | `BACKEND_TIMEOUT`      | 2.5s   | 后端 timeout 模式挂 2.0s      |
| Redis 操作超时      | `proxy/app/main.py`    | 1s     | Redis 故障时快速明确拒绝      |
| 网页上游超时        | `WEB_HTTP_TIMEOUT`     | 6s     | 网页聚合 API 访问代理/后端    |
