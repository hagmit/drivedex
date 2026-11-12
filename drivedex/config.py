"""Where drivedex keeps its config, tokens and index."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def is_store_python(executable: str | None = None) -> bool:
    """Microsoft Store Python silently redirects writes under AppData into its
    package folder, where browsers and Explorer cannot see them."""
    exe = (executable or sys.executable).lower().replace("/", "\\")
    return "\\windowsapps\\" in exe or "\\packages\\pythonsoftwarefoundation." in exe


def legacy_home() -> Path | None:
    return Path(os.environ["LOCALAPPDATA"]) / "drivedex" if os.environ.get("LOCALAPPDATA") else None


def home(executable: str | None = None) -> Path:
    if os.environ.get("DRIVEDEX_HOME"):
        return Path(os.environ["DRIVEDEX_HOME"])
    if os.environ.get("LOCALAPPDATA") and not is_store_python(executable):
        return Path(os.environ["LOCALAPPDATA"]) / "drivedex"
    return Path.home() / ".drivedex"


def migrate(old: Path | None, new: Path) -> bool:
    """Copy data written by 0.1.0 under (virtualized) AppData to the new home once."""
    if old is None or new.exists() or not old.is_dir() or old.resolve() == new.resolve():
        return False
    shutil.copytree(old, new)
    return True


class Config:
    def __init__(self, root: Path | None = None):
        if root is None:
            root = home()
            if migrate(legacy_home(), root):
                print(f"データを {root} へ移しました", file=sys.stderr)
        self.root = Path(root)
        self.path = self.root / "config.json"
        self.data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def provider(self, name: str) -> dict:
        return self.data.get(name, {})

    def set_provider(self, name: str, values: dict) -> None:
        self.data.setdefault(name, {}).update({k: v for k, v in values.items() if v is not None})
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
