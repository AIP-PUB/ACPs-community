# Demo Partner ACS 模板

`*/acs.json` 中的 `https://localhost:902x` / `amqps://rabbitmq:5671` 是**待安装器改写的占位**：

- HTTPS/JSONRPC → 本机 `acps_advertise_host`（Public 平面）
- AMQP → image 同机 `rabbitmq`，否则 / host 一律 `acps_group_addr(rabbitmq)`

改写入口：`scripts/rewrite_acs_endpoints.py`（`cert_provision` bootstrap 前 + `demo_partner` role）。
勿把这些 localhost URL 当作运行时真相。
