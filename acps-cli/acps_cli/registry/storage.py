"""本地 token 存储工具。"""

from __future__ import annotations

import json
import os
from pathlib import Path


class TokenStore:
    """在本地文件中读写 token，并设置受限权限。"""

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def load(self) -> dict[str, str] | None:
        if not self.file_path.exists():
            return None
        try:
            with open(self.file_path, encoding="utf-8") as file:
                raw = json.load(file)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        return {k: str(v) for k, v in raw.items() if isinstance(k, str)}

    def save(self, token_data: dict[str, str]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as file:
            json.dump(token_data, file, ensure_ascii=True, indent=2)
            file.write("\n")
        os.chmod(self.file_path, 0o600)

    def clear(self) -> None:
        if self.file_path.exists():
            self.file_path.unlink()
