"""AMP 路径解析：image-mode（wheel 安装）与本地开发共用。

日志目录：
  1. AMP_LOG_DIR（部署显式配置，image-mode 必填，如 /opt/acps/app/logs）
  2. 仓库根目录下的 logs/（本地开发回退）

ACS 路径（优先使用宿主机/镜像挂载的 bootstrap ACS，避免 wheel 内占位 AIC）：
  1. ACPS_APP_ROOT/leader/atr/acs.json
  2. LEADER_RUNTIME_ROOT/leader/atr/acs.json（host-mode install 写入）
  3. leader/atr/acs.json（本地开发 / wheel 回退）
"""

from __future__ import annotations

import os
from pathlib import Path

# leader/assistant/amp_paths.py → 仓库根目录（demo-leader/）
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# leader/assistant/amp_paths.py → leader/
_LEADER_PKG_ROOT = Path(__file__).resolve().parent.parent


def resolve_amp_log_dir() -> Path:
    """解析 AMP jsonl 日志目录。"""
    amp_log_dir = os.environ.get("AMP_LOG_DIR")
    if amp_log_dir:
        return Path(amp_log_dir)

    return _REPO_ROOT / "logs"


def resolve_leader_acs_file() -> Path:
    """解析 LEADER ACS（atr/acs.json）路径。"""
    for env_key in ("ACPS_APP_ROOT", "LEADER_RUNTIME_ROOT"):
        root = os.environ.get(env_key)
        if root:
            candidate = Path(root) / "leader" / "atr" / "acs.json"
            if candidate.is_file():
                return candidate

    return _LEADER_PKG_ROOT / "atr" / "acs.json"
