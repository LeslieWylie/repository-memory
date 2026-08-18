#!/usr/bin/env python3
"""Contract tests for the installer.

``install.py`` is the first code a new user runs and was the largest module in
this repository with no coverage at all: 389 statements, 0%.  Everything it
does is irreversible from the user's point of view — it replaces directories
under ``~/.claude`` and ``~/.codex``, rewrites host MCP registrations, and drops
an executable on ``PATH``.  The guards that keep those operations safe (the
managed-directory markers, the atomic replaces, the runtime-python fallback)
had no test holding them in place, so a refactor could remove one and every
job here would still go green.

Every test runs against an isolated HOME.  A test that leaked would rewrite the
developer's own agent configuration, which is precisely the failure the
installer's guards exist to prevent.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import install


@contextmanager
def isolated_home():
    """Point HOME, USERPROFILE and XDG_DATA_HOME at a throwaway directory."""

    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw).resolve()
        environment = {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_DATA_HOME": str(home / "data"),
        }
        # CODEX_HOME/CLAUDE_CONFIG_DIR are read from the environment too, and a
        # developer running the suite may well have them pointed at a real
        # profile.  Clear them so the defaults under the fake HOME are used.
        with patch.dict(os.environ, environment, clear=False):
            for leaked in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "REPOSITORY_MEMORY_RUNTIME_PYTHON"):
                os.environ.pop(leaked, None)
            yield home


def make_source(root: Path) -> Path:
    """Build a miniature skill tree shaped like the real one."""

    skill = root / "skill"
    (skill / "scripts" / "__pycache__").mkdir(parents=True)
    (skill / "scripts" / "repository-memory.py").write_text("# entry\n", encoding="utf-8")
    (skill / "scripts" / "audit_proxy.py").write_text("# proxy\n", encoding="utf-8")
    (skill / "scripts" / "__pycache__" / "core.cpython-312.pyc").write_bytes(b"\x00")
    (skill / "scripts" / "stale.pyo").write_bytes(b"\x00")
    (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (root / "eval" / "public").mkdir(parents=True)
    (root / "eval" / "public" / "queries.jsonl").write_text("{}\n", encoding="utf-8")
    return skill


class CopyGuardTest(unittest.TestCase):
    def test_compiled_python_is_never_published(self):
        # A stale .pyc published into a Skill directory shadows the source that
        # replaced it, so the host keeps running last release's code.
        self.assertEqual(
            install._ignore("scripts", ["core.py", "__pycache__", "a.pyc", "b.pyo"]),
            {"__pycache__", "a.pyc", "b.pyo"},
        )

    def test_fresh_copy_writes_the_managed_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_source(root)
            destination = root / "out" / "repository-memory"
            install._copy_skill(source, destination)

            marker = destination / ".repository-memory-install.json"
            self.assertTrue(marker.is_file())
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["schema_version"], 1)
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertFalse((destination / "scripts" / "__pycache__").exists())
            self.assertFalse((destination / "scripts" / "stale.pyo").exists())

    def test_reinstall_replaces_its_own_directory_and_drops_removed_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_source(root)
            destination = root / "out" / "repository-memory"
            install._copy_skill(source, destination)
            (destination / "REMOVED-IN-NEXT-RELEASE.md").write_text("old\n", encoding="utf-8")

            install._copy_skill(source, destination)

            self.assertTrue((destination / "SKILL.md").is_file())
            # An install that merged instead of replacing would leave this
            # behind, and the skill directory would accumulate every file this
            # project has ever shipped.
            self.assertFalse((destination / "REMOVED-IN-NEXT-RELEASE.md").exists())

    def test_unmanaged_directory_is_left_untouched(self):
        # The installer publishes into ~/.claude/skills/<name>.  If a user
        # already keeps a hand-written skill under that name, replacing it is
        # silent data loss with no undo.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_source(root)
            destination = root / "out" / "repository-memory"
            destination.mkdir(parents=True)
            handwritten = destination / "SKILL.md"
            handwritten.write_text("the user's own skill\n", encoding="utf-8")

            with self.assertRaises(RuntimeError) as raised:
                install._copy_skill(source, destination)

            self.assertIn("unmanaged", str(raised.exception))
            self.assertEqual(handwritten.read_text(encoding="utf-8"), "the user's own skill\n")

    def test_failed_copy_leaves_no_staging_directory_behind(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = make_source(root)
            destination = root / "out" / "repository-memory"
            destination.mkdir(parents=True)
            (destination / "SKILL.md").write_text("the user's own skill\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                install._copy_skill(source, destination)

            leftovers = [path.name for path in destination.parent.iterdir() if path.name.startswith(".repository-memory-")]
            self.assertEqual(leftovers, [])

    def test_openclaw_extension_refuses_an_unmanaged_plugin(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "openclaw-extension"
            source.mkdir()
            (source / "index.mjs").write_text("export default {}\n", encoding="utf-8")
            destination = root / "extensions" / "repository-memory-autocapture"
            destination.mkdir(parents=True)
            (destination / "index.mjs").write_text("someone else's plugin\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                install._copy_openclaw_extension(source, destination)
            self.assertEqual((destination / "index.mjs").read_text(encoding="utf-8"), "someone else's plugin\n")

    def test_openclaw_extension_marks_what_it_owns(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "openclaw-extension"
            source.mkdir()
            (source / "index.mjs").write_text("export default {}\n", encoding="utf-8")
            destination = root / "extensions" / "repository-memory-autocapture"

            install._copy_openclaw_extension(source, destination)
            self.assertTrue((destination / ".repository-memory-autocapture-managed").is_file())
            # Owning it is what makes the next upgrade legal.
            install._copy_openclaw_extension(source, destination)
            self.assertTrue((destination / "index.mjs").is_file())


class JsonStateTest(unittest.TestCase):
    def test_written_config_is_private_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "nested" / "config.json"
            install._atomic_json(path, {"runtime": {"python": "/usr/bin/python3"}})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["runtime"]["python"], "/usr/bin/python3")
            self.assertEqual([item.name for item in path.parent.iterdir()], ["config.json"])
            if os.name != "nt":
                # These files carry host credentials in some deployments.
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_config_reads_as_empty_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(install._read_json(Path(raw) / "absent.json"), {})

    def test_a_json_array_is_rejected_instead_of_being_used_as_a_mapping(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.json"
            path.write_text("[1, 2, 3]\n", encoding="utf-8")
            with self.assertRaises(TypeError):
                install._read_json(path)


class RuntimeResolutionTest(unittest.TestCase):
    def test_default_runtime_is_the_interpreter_running_the_installer(self):
        with isolated_home():
            self.assertEqual(install._runtime_python(), sys.executable)

    def test_configured_runtime_must_exist_and_be_executable(self):
        with isolated_home() as home:
            config = home / ".config" / "repository-memory" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"runtime": {"python": str(home / "gone")}}), encoding="utf-8")
            # A runtime that was uninstalled since it was configured must fall
            # back, not register an MCP command that cannot start.
            self.assertEqual(install._runtime_python(), sys.executable)

    @unittest.skipIf(os.name == "nt", "POSIX executable bit")
    def test_a_configured_executable_runtime_is_honoured(self):
        with isolated_home() as home:
            runtime = home / "venv-python"
            runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runtime.chmod(0o755)
            config = home / ".config" / "repository-memory" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"runtime": {"python": str(runtime)}}), encoding="utf-8")
            self.assertEqual(install._runtime_python(), str(runtime))

    def test_a_corrupt_config_does_not_abort_the_install(self):
        with isolated_home() as home:
            config = home / ".config" / "repository-memory" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text("{not json", encoding="utf-8")
            self.assertEqual(install._runtime_python(), sys.executable)


class McpCommandTest(unittest.TestCase):
    def test_every_host_gets_the_same_audited_command(self):
        with isolated_home():
            canonical = install._canonical_skill()
            command, args = install._mcp_command(canonical)

            self.assertEqual(command, sys.executable)
            # The audit proxy is the whole privacy story: it is what lets a user
            # see what the server was asked for.  Registering the server
            # directly would work and silently drop the audit trail.
            self.assertEqual(args[0], str(canonical / "scripts" / "audit_proxy.py"))
            self.assertIn("--log", args)
            self.assertEqual(args[-1], "mcp")
            self.assertEqual(Path(args[args.index("--log") + 1]).parent, install._data_home() / "repository-memory")

    def test_the_canonical_skill_follows_xdg_data_home(self):
        with isolated_home() as home:
            self.assertEqual(
                install._canonical_skill(),
                (home / "data" / "repository-memory" / "skill" / install.SKILL_NAME).resolve(),
            )


class CliWrapperTest(unittest.TestCase):
    def test_wrapper_is_executable_and_survives_a_path_with_spaces(self):
        with isolated_home() as home:
            canonical = home / "Application Support" / "skill"
            (canonical / "scripts").mkdir(parents=True)
            destination = install._install_cli(canonical)

            self.assertTrue(destination.is_file())
            body = destination.read_text(encoding="utf-8")
            self.assertIn("Application Support", body)
            if os.name != "nt":
                self.assertTrue(body.startswith("#!/bin/sh"))
                self.assertTrue(os.access(destination, os.X_OK))
                # An unquoted path breaks the wrapper on any account whose home
                # has a space in it, which is the default on macOS for a user
                # with a space in their name.
                self.assertIn(json.dumps(str(canonical / "scripts" / "repository-memory.py")), body)


class HostRegistrationTest(unittest.TestCase):
    """The get → remove → add sequence, without invoking a real host CLI."""

    def test_claude_skill_lands_in_the_configured_directory_without_mcp(self):
        with isolated_home() as home, tempfile.TemporaryDirectory() as raw:
            source = make_source(Path(raw))
            result = install._install_claude(source, register_mcp=False)

            self.assertEqual(result["mcp_registered"], False)
            self.assertEqual(result["mcp_detail"], "skipped")
            self.assertTrue((home / ".claude" / "skills" / install.SKILL_NAME / "SKILL.md").is_file())

    def test_an_existing_registration_is_removed_before_the_audited_one_is_added(self):
        calls: list[list[str]] = []

        def fake_run(command, env=None):
            calls.append(list(command))
            return True, ""

        with isolated_home(), tempfile.TemporaryDirectory() as raw:
            source = make_source(Path(raw))
            with patch.object(install.shutil, "which", return_value="/usr/local/bin/claude"), \
                    patch.object(install, "_run", side_effect=fake_run):
                result = install._install_claude(source, register_mcp=True)

        verbs = [call[1:3] for call in calls]
        self.assertEqual(verbs, [["mcp", "get"], ["mcp", "remove"], ["mcp", "add"]])
        # Adding without removing first is the bug this ordering exists to
        # prevent: the host keeps the old unaudited command and the new one is
        # rejected as a duplicate name.
        self.assertTrue(result["mcp_registered"])
        self.assertEqual(result["mcp_detail"], "replaced with audited proxy")
        self.assertIn("--scope", calls[-1])
        self.assertIn("audit_proxy.py", " ".join(calls[-1]))

    def test_a_registration_that_cannot_be_removed_is_reported_not_ignored(self):
        def fake_run(command, env=None):
            if command[1:3] == ["mcp", "remove"]:
                return False, "permission denied"
            return True, ""

        with isolated_home(), tempfile.TemporaryDirectory() as raw:
            source = make_source(Path(raw))
            with patch.object(install.shutil, "which", return_value="/usr/local/bin/claude"), \
                    patch.object(install, "_run", side_effect=fake_run):
                result = install._install_claude(source, register_mcp=True)

        self.assertFalse(result["mcp_registered"])
        self.assertIn("permission denied", result["mcp_detail"])


class DeclaredSupportTest(unittest.TestCase):
    def test_the_runtime_floor_matches_requires_python(self):
        # `requires-python` is a promise pip enforces at install time; the
        # check inside install() is the one a user actually hits.  If they ever
        # disagree, one of them is lying to somebody.
        pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'requires-python\s*=\s*"[><=]*\s*(\d+)\.(\d+)"', pyproject)
        self.assertIsNotNone(declared, "requires-python is missing from pyproject.toml")
        floor = (int(declared.group(1)), int(declared.group(2)))

        source = (SCRIPTS / "install.py").read_text(encoding="utf-8")
        enforced = re.search(r"if version < \((\d+), (\d+)\):", source)
        self.assertIsNotNone(enforced, "install() no longer enforces a Python floor")
        self.assertEqual((int(enforced.group(1)), int(enforced.group(2))), floor)

    def test_the_advertised_mcp_tools_are_the_ones_the_server_exposes(self):
        # _verify() fails the whole install on any mismatch between this list
        # and the server's tools/list response, so the list is load-bearing.
        from mcp_server import _tool_schema  # noqa: PLC0415 - resolved via SCRIPTS above

        exposed = sorted(tool["name"] for tool in _tool_schema())
        self.assertEqual(sorted(install.MCP_TOOLS), exposed)


if __name__ == "__main__":
    unittest.main()
