# AIP 通信如何防止身份伪造

本教程回答一个常见问题：在 AIP 通信中，如何防止一个 Agent 冒充另一个 Agent 发送业务消息？

先给结论。AIP 防身份伪造依赖两个配合使用的技术点：

1. mTLS 证明“连接对端是谁”。
2. 证书身份和业务数据对比，证明“业务消息声称的发送者就是连接对端本人”。

只做第一步还不够。mTLS 能确认对端持有合法证书，但业务消息里的 `senderId` 是 JSON body 的一部分，客户端可以自己构造。如果一个合法 Agent 用自己的证书建立连接，却在 body 里写成别人的 `senderId`，这就是业务身份伪造。

所以 AIP 需要把连接身份和业务身份绑定起来：

```text
连接身份 == 业务消息 senderId
```

相关规范：

- [AIP 智能体交互协议规范](../../acps-specs/07-ACPs-spec-AIP/ACPs-spec-AIP.md)
- [AIA 身份认证规范](../../acps-specs/05-ACPs-spec-AIA/ACPs-spec-AIA.md)

---

## 1. mTLS 解决什么问题

mTLS 解决的是“连接层身份认证”问题。

在 AIP 直连通信中，双方使用 HTTPS + mTLS。服务端验证客户端证书，客户端也可以验证服务端证书。证书中的 CN 是 Agent 的 AIC，SAN 可以作为补充身份信息。

这意味着接收方可以知道：

- 这条 TLS 连接来自一个持有合法证书的 Agent。
- 证书里的 CN 可以作为这个连接对端的 AIC。
- 连接内容受到 TLS 保护，传输过程中不容易被窃听或篡改。

但是 mTLS 不会自动理解 AIP body。它不会替你检查 JSON 里的 `senderId` 是否诚实。

例如：

```text
TLS 证书 CN：1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
body.senderId：1.2.156.3088.1.1.34C2.478BDF.3GF548.0P65
```

从 mTLS 角度看，这条连接是合法的，因为证书是真的。  
从 AIP 业务角度看，这条消息应该被拒绝，因为业务消息声称的发送者和证书身份不一致。

---

## 2. 证书和业务数据对比解决什么问题

证书和业务数据对比解决的是“业务身份伪造”问题。

AIP 消息里的 `senderId` 表示“这条业务消息声称是谁发出的”。但 `senderId` 位于业务数据中，发送方可以自己填写。接收方不能只因为 body 中写了某个 AIC，就相信消息来自那个 AIC。

因此，接收方需要执行这样的检查：

```text
从 mTLS 证书取得 peer AIC
读取 AIP body.senderId
比较 peer AIC == body.senderId
```

如果相等，说明连接身份和业务身份一致，可以继续处理。  
如果不相等，说明发送方在业务数据里声称了另一个身份，应该拒绝。

这就是 Identity Binding。它不是替代 mTLS，而是在 mTLS 已经证明连接身份之后，把这个身份继续约束到 AIP 业务消息上。

---

## 3. Direct、Stream 和 Notification 怎么防伪造

Direct RPC、Stream、Notification callback 都是直连 HTTP 通信，所以规则是一致的。

```text
mTLS peer certificate CN
  -> peer AIC
  -> 必须等于 AIP payload 的 senderId
```

可以把它理解成下面这张检查表：

| 来源 | 含义 | 是否可信 |
| --- | --- | --- |
| mTLS 证书 CN | 连接对端的 AIC | 可信，来自证书校验 |
| body.senderId | 业务消息声明的发送者 | 需要校验，不能单独信任 |

接收方要做的事情就是比较两者：

```text
证书 CN 中的 AIC == body.senderId
```

如果不相等，消息应该在进入业务 handler 前被拒绝。

Notification callback 也是同样逻辑。callback 请求体中通常是 `TaskResult`，它的 `senderId` 必须等于 callback 请求的 mTLS peer AIC。

---

## 4. Group / RabbitMQ 怎么防伪造

群组模式不一样。消息不是接收方和发送方直接建立 HTTP 连接，而是经过 RabbitMQ 中转。

因此消费端不能拿“当前连接的 mTLS 证书 CN”和 `body.senderId` 直接比较，因为消费端看到的是 RabbitMQ 连接，不是原始发送者连接。

群组模式需要把身份分成两段传递：

```text
发布方 mTLS 证书 CN
  -> RabbitMQ authenticated username
  -> AMQP user_id
  -> message body.senderId
```

这里有两层校验：

1. RabbitMQ 校验 `AMQP user_id == authenticated username`。
2. 消费端 SDK 校验 `body.senderId == AMQP user_id`。

这样做的原因是：

- RabbitMQ 能验证发布连接是谁，但看不到也不应该理解 AIP JSON body。
- 消费端 SDK 能解析 AIP body，但它拿不到发布方当时的 TLS 连接。
- `AMQP user_id` 是两者之间的身份传递桥梁。

如果发布者试图伪造 `user_id`，RabbitMQ 应该拒绝发布。  
如果发布者使用真实 `user_id`，但在 body 里伪造 `senderId`，消费端 SDK 应该拒绝消费。

---

## 5. 为什么推荐主动伪造验证

阅读代码和查看配置只能说明“系统看起来有校验”。更可靠的方式是主动构造一条假消息，看它是否真的过不了边界。

主动伪造验证的关键不是伪造证书。证书在正常系统中不能被伪造。我们要做的是：

1. 使用真实、合法的证书建立连接。
2. 故意修改攻击者可以控制的字段，例如 `body.senderId` 或 AMQP `user_id`。
3. 观察系统是否拒绝。

这能验证三件事：

- mTLS 身份是否真的进入了 SDK 或 RabbitMQ。
- SDK 或 RabbitMQ 是否真的做了身份对比。
- 伪造消息是否在业务逻辑产生副作用之前被拒绝。

一个好的伪造测试应该同时观察：

- 返回状态或错误码是否符合预期。
- 错误信息是否能说明是身份不一致。
- 业务 handler 是否没有执行。
- 队列、任务、group queue 等业务状态是否没有被错误创建或更新。

下面每个场景都会先说明验证逻辑，再给出项目中已经写好的 e2e 样例。命令假设从 ACPs 工作区根目录执行，也就是包含 `demo-leader`、`demo-partner`、`mq-auth-server`、`acps-docs` 的目录。

---

## 6. 验证 Direct：伪造 `senderId`

Direct RPC 的验证方法是：

1. 使用真实 Leader 证书连接 Partner。
2. 构造一条 AIP RPC 请求。
3. 把请求体里的 `senderId` 改成不是 Leader 的 AIC。
4. 发送请求。
5. 观察 Partner 是否拒绝。

要验证的不是“请求能不能发出去”，而是“请求到了 Partner 边界后，会不会因为身份不一致被拒绝”。

伪造样例：

```text
证书身份：1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
body.senderId：1.2.156.3088.1.1.34C2.478BDF.3GF548.0P65
预期结果：拒绝
```

应观察到：

- RPC 返回 JSON-RPC error。
- error code 是 `-32009`。
- error data 或错误信息中包含 `senderId`。
- Partner 的业务处理逻辑没有执行。

项目中已经有对应的 e2e 样例，可以直接运行：

```bash
cd demo-partner
uv run pytest tests/e2e/test_identity_binding.py::test_direct_rpc_rejects_forged_sender_id -q
```

Stream 的验证逻辑相同，只是返回形式是 HTTP/SSE：

```bash
cd demo-partner
uv run pytest tests/e2e/test_identity_binding.py::test_stream_rejects_forged_sender_id -q
```

应观察到：

- HTTP status 是 `403`。
- response detail 中包含 `-32009`。
- 错误信息提到 `senderId`。
- 不产生正常业务流式事件。

---

## 7. 验证 Notification callback：伪造 `TaskResult.senderId`

Notification callback 的验证方法是：

1. 使用真实 Partner 证书调用 Leader 的 callback endpoint。
2. 构造一条 `TaskResult`。
3. 把 `TaskResult.senderId` 改成不是 Partner 的 AIC，例如改成 Leader 的 AIC。
4. 发送 callback 请求。
5. 观察 Leader 是否拒绝。

伪造样例：

```text
证书身份：1.2.156.3088.1.1.34C2.478BDF.3GF547.0GGS
TaskResult.senderId：1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
预期结果：拒绝
```

应观察到：

- HTTP status 是 `403`。
- response detail 中包含 `-32009`。
- 错误信息提到 `senderId`。
- Leader 不应把这条伪造 callback 当作真实 Partner 结果处理。

项目中已有对应 e2e 样例：

```bash
cd demo-leader
uv run pytest tests/e2e/test_notification_identity.py::test_notification_receiver_rejects_forged_sender_id_with_valid_partner_cert -q
```

---

## 8. 验证 Group：伪造 AMQP `user_id`

Group 模式第一层要验证 RabbitMQ 是否拒绝伪造 `user_id`。

验证方法是：

1. 使用某个真实 Agent 证书连接 RabbitMQ。
2. 发布消息时，把 AMQP `user_id` 设置成另一个 AIC。
3. 观察 RabbitMQ 是否拒绝发布。

伪造样例：

```text
mTLS authenticated username：1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
AMQP user_id：1.2.156.3088.1.1.34C2.478BDF.3GF548.0P65
预期结果：RabbitMQ 拒绝发布
```

应观察到：

- 消息发布失败。
- RabbitMQ 或客户端返回权限相关错误。
- mq-auth-server 日志中能看到 username 和授权结果。
- 消息没有进入目标队列。

项目中已有对应 e2e 样例：

```bash
cd mq-auth-server
uv run pytest tests/e2e/test_rabbitmq_user_id_identity.py::test_validated_user_id_rejects_forged_user_id -q
```

如果这条验证没有失败，优先检查 RabbitMQ 是否启用了 EXTERNAL/mTLS，以及普通 Agent 是否被错误授予了可伪造 `user_id` 的权限。

---

## 9. 验证 Group：伪造 body.senderId

Group 模式第二层要验证消费端 SDK 是否拒绝 body 中的伪造身份。

这一步的重点是：`AMQP user_id` 是真实的，但 `body.senderId` 是假的。

验证方法是：

1. 使用真实 Agent 身份发布消息。
2. 让 AMQP `user_id` 等于发布者自己的 AIC。
3. 把消息 body 里的 `senderId` 改成另一个 AIC。
4. 让消费端收到消息。
5. 观察消费端 SDK 是否拒绝，并确认业务状态没有被错误改变。

伪造样例：

```text
AMQP user_id：1.2.156.3088.1.1.34C2.478BDF.3GF547.0GGS
body.senderId：1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
预期结果：消费端 SDK 拒绝
```

应观察到：

- 消费端记录身份不一致错误。
- 业务 handler 不应处理这条伪造消息。
- 不应创建伪造 group queue。
- 不应产生后续错误业务消息。

项目中已有对应 e2e 样例：

```bash
cd demo-leader
uv run pytest tests/e2e/test_group_identity.py::test_partner_inbox_consumer_rejects_forged_sender_identity -q
```

这条验证很重要，因为 RabbitMQ 不能读取 JSON body。即使 RabbitMQ 已经验证了 `user_id`，仍然需要消费端 SDK 检查 `body.senderId == AMQP user_id`。

---

## 10. 排障时看哪些日志

主动伪造测试是最可靠的验证方法，日志主要用于排障。

可以看这些位置：

- `mq-auth-server` 日志：搜索 `rabbitmq_auth_decision`、`authz_decision`、`username`、`decision`。
- demo-leader / demo-partner 应用日志：搜索 `senderId`、`identity binding`、`AuthorizationFailedError`、`-32009`。
- AMP 日志：`demo-leader/logs/amp_*.jsonl`、`demo-partner/logs/amp_*.jsonl`。

注意：当前 AMP audit / access / message 日志更适合看任务、访问和消息流转，不是专门的 identity-binding verdict 日志。它们可以辅助排障，但不能替代主动伪造测试。

---

## 11. 最小验证清单

如果只想快速确认 AIP 身份防伪造是否工作，可以跑下面几条伪造测试：

```bash
cd demo-partner
uv run pytest tests/e2e/test_identity_binding.py::test_direct_rpc_rejects_forged_sender_id -q
uv run pytest tests/e2e/test_identity_binding.py::test_stream_rejects_forged_sender_id -q

cd ../demo-leader
uv run pytest tests/e2e/test_notification_identity.py::test_notification_receiver_rejects_forged_sender_id_with_valid_partner_cert -q
uv run pytest tests/e2e/test_group_identity.py::test_partner_inbox_consumer_rejects_forged_sender_identity -q

cd ../mq-auth-server
uv run pytest tests/e2e/test_rabbitmq_user_id_identity.py::test_validated_user_id_rejects_forged_user_id -q
```

这些测试都通过，基本就能说明：

- Direct RPC / Stream 能拒绝伪造 `senderId`。
- Notification callback 能拒绝伪造 `TaskResult.senderId`。
- RabbitMQ 能拒绝伪造 `user_id`。
- Group consumer 能拒绝 `body.senderId != AMQP user_id`。

这就是 AIP 通信防止身份伪造的核心闭环。
