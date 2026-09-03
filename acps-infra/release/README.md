# ACPs release 打包层

新打包部署方案落在本目录（`app-packaging` / `image-packaging` / `install-packaging`）。

## 目录边界

| 目录 | 职责 | 主要产物 |
| --- | --- | --- |
| `app-packaging/` | 应用发布包装配 | app-release kit / 薄包 |
| `image-packaging/` | image-mode 镜像包 | `*.image.tar.gz` / 镜像清单 |
| `install-packaging/` | 安装包组装 + Ansible | `acps-*-install-*.tar` |
| `lib/` | 共享 Python 库（如 `runtime_package.py`） | — |

概念见仓库根 [`README.md`](../README.md)；命令见 [acps-docs](../../acps-docs/README.md)。
