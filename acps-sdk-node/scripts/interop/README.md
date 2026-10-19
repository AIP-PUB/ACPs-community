# Python ↔ Node 互通测试

让官方 Python SDK（`../acps-sdk`）与本包（Node SDK）**互为对端**，在真实 mTLS 与真实 RabbitMQ 上跑同一套场景，
逐项断言线上数据与身份绑定行为。需要 uv/Python 和 Node 两套环境，所以不在 `npm test` 里，按需手动运行。

## 运行

```bash
cd acps-sdk-node
# 只跑 HTTP 场景（RPC / 流式 / 通知）
npm run interop
# 加上群组模式：指向一个 RabbitMQ（AMQP 端口），并让脚本在容器里建好以 AIC 命名的用户
ACPS_INTEROP_RABBITMQ=127.0.0.1:5672 ACPS_INTEROP_RABBITMQ_CONTAINER=<容器名> npm run interop
# 常用参数：-v 打印每条断言；--only rpc,stream,notification,group；
#           --baseline 另跑 Py↔Py、Node↔Node 作对照；--debug 打开两边 SDK 日志
npm run interop -- -v --baseline
```

`npm run interop` 会先 `npm run build`：Node 端 `agent.mts` 运行时加载 `dist/`（用 Node 自带的类型剥离直接运行，
需 Node ≥ 22.18），类型检查则对照 `src/`，所以 `tsc --noEmit` 不依赖 `dist/`。

测试证书（CA + Leader / Partner / Other 三个 AIC 的叶子证书，外加一张由不受信任 CA 签发的证书）在运行时生成到临时目录，
结束即删除，不写进仓库。

## 群组模式的前提

PLAIN 认证下 RabbitMQ 要求 AMQP `user_id` 等于认证用户名，所以每个 AIC 都要有同名用户：

| 环境变量                          | 说明                                                                             |
| --------------------------------- | -------------------------------------------------------------------------------- |
| `ACPS_INTEROP_RABBITMQ`           | `host:port`；不设置则群组场景记为 SKIP                                           |
| `ACPS_INTEROP_RABBITMQ_VHOST`     | 默认 `/`                                                                         |
| `ACPS_INTEROP_RABBITMQ_PASSWORD`  | 三个 AIC 用户的密码，默认 `acps-interop`                                         |
| `ACPS_INTEROP_RABBITMQ_CONTAINER` | 可选；设置后脚本用 `docker exec <容器> rabbitmqctl` 建用户、设密码和权限（幂等） |

不用容器时手动建用户（对 `1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4`、`…3GF547.0JU5`、`…3GF548.0JU6` 各执行一次）：

```bash
rabbitmqctl add_user <AIC> acps-interop
rabbitmqctl set_permissions -p / <AIC> '.*' '.*' '.*'
```

邀请走 MQ 收件箱（`inviteViaInbox` → `joinGroupFromInvitation`）：Partner 用自己的用户连接，身份绑定全程开启。
直连 RPC 邀请（`invitePartner`）在两边 SDK 里都把 **Leader 自己的** `user:password` 放进 `server.accessToken`，
Partner 会以 Leader 身份登录，PLAIN + 身份绑定下它发出的 `user_id=<Partner AIC>` 会被 broker 拒绝；
该路径要配 AMQPS + EXTERNAL（mTLS），本脚本不覆盖。

## 结果

最后打印「场景 × 方向」矩阵：PASS / FAIL / XFAIL / SKIP，括号内是断言条数。有 FAIL 时退出码为 1。

XFAIL 表示命中已登记的**Python SDK 已知问题**（`--baseline` 下 Py Leader → Py Partner 同样复现，与 Node 无关）：

- Python `GroupPartnerMqClient` 在群组队列上收到 `LEAVE_GROUP` / `DISBAND` 后，退出清理在 aio-pika 的消费回调任务里执行，
  `connection.close()` 会取消这个任务本身，导致 `_reset_group_state()` 与 disconnect 回调不执行，状态停在 `leaving`。
  线上消息（`connected=false`、回显 `sessionId`）正常。

XFAIL 是严格的：问题修复后断言反而通过时会记为 FAIL，提示去掉 `run_interop.py` 里的标记。

## 文件

| 文件                            | 作用                                                                           |
| ------------------------------- | ------------------------------------------------------------------------------ |
| `run_interop.py`                | 驱动：生成证书、准备 RabbitMQ 用户、按方向跑场景、打印矩阵                     |
| `interop_py.py`                 | Python 端（在驱动进程内运行 SDK；服务端是 uvicorn + `AipPeerCertH11Protocol`） |
| `interop_node.py` / `agent.mts` | Node 端（子进程，stdin/stdout 上的 JSON 行协议）                               |
| `interop_common.py`             | AIC 常量、证书生成、两端共用的 Partner 产出物规则、断言记录                    |
