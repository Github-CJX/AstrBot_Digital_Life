"""B 站请求配额/风控状态的本地持久化（替代 bili_whale 的 store.Store）。

只保留 TrafficGuard 需要的最小接口：``quota`` 字典 + 原子写。
布局仿 whale：os.replace 原子写，坏文件保留 .broken.json 后重建。
文件位于插件数据目录 ``bilibili/quota.json``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class QuotaStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.quota: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                self.quota = data
                return
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self._park_broken()
        self.quota = {}

    def _park_broken(self) -> None:
        try:
            broken = self.path.with_name(self.path.name + ".broken.json")
            if self.path.exists():
                os.replace(self.path, broken)
        except OSError:
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps(self.quota, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError:
            pass
