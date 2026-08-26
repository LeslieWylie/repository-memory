"""Prepare writable runtime copies of bundled TencentDB components."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


GENERATED_RUNTIME_DIRS = {"__pycache__", "node_modules", "dist", ".venv"}


def runtime_root(component: str) -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser()
    return data_home / "repository-memory" / "tencentdb-runtime" / component


def prepare_runtime_source(component: str, source: Path) -> Path:
    """Refresh a writable runtime tree while preserving installed dependencies."""

    source = source.expanduser().resolve()
    destination = runtime_root(component).resolve()
    if source == destination:
        return destination
    if not source.is_dir():
        raise RuntimeError(f"{component} source is unavailable: {source}")
    destination.mkdir(parents=True, exist_ok=True)

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in GENERATED_RUNTIME_DIRS or name.endswith((".pyc", ".pyo"))
        }

    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)
    return destination
