# 在运行 `site.yml`（或单独部署 ca-server）之前，将 ca-server 中间 CA 材料放在此目录。
#
# 必需（可部署集合）：
# ca.crt — 中间 CA 证书
# ca.key — 中间 CA 私钥（chmod 600）
# root-ca.crt — 根 CA 证书（trust anchor）
#
# 切勿将根私钥放在本目录根下。
# 自签辅助脚本仅将其保留在 offline/ 下：
# ../../scripts/generate_ca_materials.sh --out.
#
# 生产环境：将离线根签名的材料复制到本目录。
# 控制节点 ca-materials：供 ca_server 首装使用。
