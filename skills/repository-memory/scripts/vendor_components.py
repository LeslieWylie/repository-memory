"""Report the pinned upstream components bundled with this Skill.

The vendor snapshot is source material, not a second retrieval backend. Keeping
this small reader in the Python runtime makes ``doctor`` able to prove which
upstream component set is installed without importing the TypeScript package or
guessing from a local checkout outside this repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def vendor_root() -> Path:
    return Path(__file__).resolve().parents[1] / "vendor" / "tencentdb-agent-memory-reference"


def manifest_path() -> Path:
    return vendor_root() / "MANIFEST.json"


def _read_manifest() -> dict[str, Any]:
    try:
        value = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def report() -> dict[str, Any]:
    manifest = _read_manifest()
    components = manifest.get("components") if isinstance(manifest.get("components"), dict) else {}
    available = manifest_path().is_file() and bool(components)
    files = sum(1 for path in vendor_root().rglob("*") if path.is_file()) if available else 0
    return {
        "available": available,
        "root": str(vendor_root()) if available else None,
        "manifest": str(manifest_path()) if available else None,
        "upstream_repository": manifest.get("upstream_repository"),
        "upstream_commit": manifest.get("upstream_commit"),
        "import_method": manifest.get("import_method"),
        "tracked_source_count": manifest.get("tracked_source_count"),
        "import_scope": manifest.get("import_scope", []),
        "runtime_generated_excluded": manifest.get("runtime_generated_excluded", []),
        "dirty_worktree_changes_excluded": manifest.get("dirty_worktree_changes_excluded") is True,
        "file_count": files,
        "components": {
            name: {
                "root": value.get("root"),
                "modules": value.get("modules", []),
                "role": value.get("role"),
            }
            for name, value in components.items()
            if isinstance(value, dict)
        },
    }


def has_module(relative: str) -> bool:
    path = vendor_root() / relative
    try:
        path.relative_to(vendor_root())
    except ValueError:
        return False
    return path.exists()
