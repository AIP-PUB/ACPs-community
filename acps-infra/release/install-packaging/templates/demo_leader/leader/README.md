# Demo Leader 树模板（ACS / scenario）

- `atr/acs.json`：Leader 自身（多为 AMQP）；安装器注入 advertise + AMQP host。
- `scenario/expert/*/*.json`：静态 Partner ACS 快照；HTTPS 改写为 **demo_partner** 的
  `acps_advertise_host`（不是 Leader 本机，除非同机）。

模板内 `localhost` 仅为占位，由 `scripts/rewrite_acs_endpoints.py` 在安装时改写。
