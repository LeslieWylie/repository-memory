"""Single runtime version source for the copied Skill distribution."""

from pathlib import Path


def _read_version() -> str:
    value = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("invalid repository-memory VERSION file")
    return value


VERSION = _read_version()
