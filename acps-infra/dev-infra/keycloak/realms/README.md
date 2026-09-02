# Keycloak Realm Imports

本目录提供 `dev-infra` Keycloak 使用的 3 个 realm import 模板：

- `acps-registry.json`
- `acps-monitor.json`
- `acps-leader.json`

导入目标：

1. 为三个项目创建彼此隔离的 realm。
2. 为每个 realm 预置 `<project>-web` 与 `<project>-api` 两个基础 client；`../bootstrap-eddsa.sh` 会继续补齐 `registry-cli` / `monitor-cli` 这类 Device Grant CLI client。
3. 为每个 realm 预置项目角色、可选 OIDC scopes、以及若干测试用户。

注意事项：

1. 这些 JSON 主要承载 realm、client、role、scope、测试用户和 audience / claim mapper。
2. `redirectUris` 与 `webOrigins` 由 `dev-infra` 启动后的 `bootstrap-dev-keycloak.sh`（内部调用 `bootstrap-eddsa.sh`）按本地开发 URL（`REGISTRY_WEB_BASE_URL`、`MONITOR_WEB_BASE_URL`、`LEADER_WEB_BASE_URL`）自动对齐。
3. Keycloak 的签名 key material 是运行时状态，不建议直接把私钥材料放入仓库。realm import 已把 `defaultSignatureAlgorithm` 设为 `EdDSA`，并由 `bootstrap-eddsa.sh` 补齐每个 realm 的 `eddsa-generated` key provider（`Ed25519`，active/enabled）。若手工启动 Keycloak，仍应在管理界面或 Admin API 中核对：
   - `Realm Settings > Tokens > Default Signature Algorithm = EdDSA`
   - `Realm Settings > Keys` 中存在 active/enabled 的 `eddsa-generated` key，曲线为 `Ed25519`
4. `roles` client scope 已显式把 `<project>-api` 的 client roles 映射到 `resource_access.<project>-api.roles`。`bootstrap-eddsa.sh` 会同步补齐对应的 role scope mappings，使 `fullScopeAllowed=false` 仍能签发包含项目角色的 access token。不要把 `fullScopeAllowed` 打开来“修复”角色缺失；需要新增角色时，应继续在对应 `*-api` client roles 和 mapper / scope mapping 约定内调整。
5. `acps-monitor` realm 中的 `tenant_id` / `allowed_aics` 通过用户属性 + mapper 进入 access token。若后续切换为权限服务或 group 映射，应同步调整 mapper。

默认测试用户密码统一为 `demo123`。

产品交付（安装包 / 镜像包）另有各自的 Keycloak realm 模板副本，见 `release/install-packaging/templates/keycloak/realms/` 与 `release/image-packaging/infra/keycloak/realms/`。
