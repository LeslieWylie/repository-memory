"""Single runtime version source for the copied Skill distribution."""

from pathlib import Path
from importlib.metadata import PackageNotFoundError, version as installed_version


def _read_version() -> str:
    source_version = Path(__file__).resolve().parents[1] / "VERSION"
    if source_version.is_file():
        value = source_version.read_text(encoding="utf-8").strip()
    else:
        try:
            value = installed_version("repository-memory")
        except PackageNotFoundError as exc:
            raise RuntimeError("repository-memory VERSION metadata is unavailable") from exc
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("invalid repository-memory VERSION file")
    return value


VERSION = _read_version()
