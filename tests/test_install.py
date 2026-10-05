"""Installer tests: fake-HOME install/uninstall, non-destructive JSON merge.

These run the real install.sh with HOME (and the XDG vars) redirected into a
temp dir, so nothing on the developer's machine is touched. --no-daemon keeps
systemd and launchd out of it.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import helpers

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"

# Entry shapes that other tooling owns and that must survive our merge.
HERDR_CLAUDE = {
    "permissions": {"allow": ["mcp__zvec_grep__*"]},
    "hooks": {
        "SessionStart": [
            {
                "matcher": "^(startup|resume|clear|compact|fork)$",
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash '/home/someone/.claude/hooks/herdr-agent-state.sh' session",
                        "timeout": 10,
                    }
                ],
            }
        ]
    },
}
PLANNATOR_CODEX = {
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash '/home/someone/.codex/herdr-agent-state.sh' session",
                        "timeout": 10,
                    }
                ]
            }
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "/home/someone/.local/bin/plannotator",
                        "timeout": 345600,
                    }
                ]
            }
        ],
    }
}


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ajq-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = {
            "HOME": self.home,
            "PATH": os.environ["PATH"],
            "XDG_DATA_HOME": os.path.join(self.home, ".local", "share"),
            "XDG_STATE_HOME": os.path.join(self.home, ".local", "state"),
            "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
            "XDG_RUNTIME_DIR": os.path.join(self.home, "run"),
            "AJQ_NO_AUTOSTART": "1",
        }
        os.makedirs(self.env["XDG_RUNTIME_DIR"], exist_ok=True)

    def write(self, relpath, payload):
        path = pathlib.Path(self.home) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return path

    def read(self, relpath):
        with open(pathlib.Path(self.home) / relpath, encoding="utf-8") as handle:
            return json.load(handle)

    def install(self, *args, expect=0):
        result = subprocess.run(
            ["bash", str(INSTALL), "--no-daemon", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def uninstall(self, *args, expect=0):
        result = subprocess.run(
            ["bash", str(INSTALL), "--no-daemon", "--uninstall", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    # -- helpers ------------------------------------------------------------
    def commands_in(self, payload, event):
        found = []
        for group in payload.get("hooks", {}).get(event, []) or []:
            for handler in group.get("hooks", []) or []:
                if handler.get("command"):
                    found.append(handler["command"])
        return found

    def ajq_commands(self, payload, event):
        return [cmd for cmd in self.commands_in(payload, event) if "ajq" in cmd]


class TestCliInstall(InstallerCase):
    def test_zipapp_is_built_and_runs(self):
        self.install("--target", "claude")
        binary = pathlib.Path(self.home) / ".local" / "bin" / "ajq"
        self.assertTrue(binary.exists(), "zipapp not installed")
        self.assertTrue(os.access(binary, os.X_OK))
        result = subprocess.run([str(binary), "version"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ajq", result.stdout)

    def test_config_is_seeded_once_and_never_overwritten(self):
        self.install("--target", "claude")
        config = pathlib.Path(self.home) / ".config" / "ajq" / "config.json"
        self.assertTrue(config.exists())
        payload = self.read(".config/ajq/config.json")
        self.assertEqual(payload["limits"]["max_concurrent"], 8)
        payload["limits"]["max_concurrent"] = 3  # user edit
        with open(config, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        self.install("--target", "claude")  # update run
        self.assertEqual(self.read(".config/ajq/config.json")["limits"]["max_concurrent"], 3)
        self.install("--target", "claude", "--force-config")
        self.assertEqual(self.read(".config/ajq/config.json")["limits"]["max_concurrent"], 8)

    def test_skill_installed_per_harness(self):
        self.install("--target", "claude,opencode,codex")
        for relpath in (
            ".claude/skills/ajq/SKILL.md",
            ".config/opencode/skills/ajq/SKILL.md",
            ".codex/skills/ajq/SKILL.md",
        ):
            self.assertTrue((pathlib.Path(self.home) / relpath).exists(), relpath)

    def test_hook_scripts_land_in_the_shared_prefix(self):
        self.install("--target", "claude")
        hooks = pathlib.Path(self.home) / ".local" / "share" / "ajq" / "hooks"
        for name in ("ajq-ensure.sh", "ajq-session-start.sh", "ajq-pre-tool-use.sh"):
            path = hooks / name
            self.assertTrue(path.exists(), name)
            self.assertTrue(os.access(path, os.X_OK), name)


class TestJsonMerge(InstallerCase):
    def test_claude_settings_keep_herdr_entry(self):
        self.write(".claude/settings.json", HERDR_CLAUDE)
        self.install("--target", "claude")
        payload = self.read(".claude/settings.json")
        self.assertEqual(payload["permissions"], HERDR_CLAUDE["permissions"])
        self.assertIn("herdr-agent-state.sh", " ".join(self.commands_in(payload, "SessionStart")))
        self.assertTrue(self.ajq_commands(payload, "SessionStart"), "our SessionStart hook missing")
        self.assertTrue(self.ajq_commands(payload, "PreToolUse"), "our PreToolUse hook missing")
        self.assertEqual(payload["hooks"]["PreToolUse"][0]["matcher"], "Bash")

    def test_codex_hooks_keep_plannotator_and_herdr(self):
        self.write(".codex/hooks.json", PLANNATOR_CODEX)
        self.install("--target", "codex")
        payload = self.read(".codex/hooks.json")
        stop = " ".join(self.commands_in(payload, "Stop"))
        self.assertIn("plannotator", stop)
        self.assertIn("herdr-agent-state.sh", " ".join(self.commands_in(payload, "SessionStart")))
        self.assertTrue(self.ajq_commands(payload, "SessionStart"))

    def test_merge_is_idempotent(self):
        self.write(".claude/settings.json", HERDR_CLAUDE)
        self.install("--target", "claude")
        first = self.commands_in(self.read(".claude/settings.json"), "PreToolUse")
        self.install("--target", "claude")
        second = self.commands_in(self.read(".claude/settings.json"), "PreToolUse")
        self.assertEqual(first, second)
        self.assertEqual(len(second), 1)

    def test_missing_settings_file_is_created_valid(self):
        self.install("--target", "claude,codex")
        for relpath in (".claude/settings.json", ".codex/hooks.json"):
            payload = self.read(relpath)
            self.assertIn("hooks", payload)
            self.assertTrue(self.ajq_commands(payload, "SessionStart"), relpath)

    def test_uninstall_removes_only_our_entries(self):
        self.write(".claude/settings.json", HERDR_CLAUDE)
        self.install("--target", "claude")
        self.uninstall()
        payload = self.read(".claude/settings.json")
        self.assertEqual(self.ajq_commands(payload, "SessionStart"), [])
        self.assertEqual(self.ajq_commands(payload, "PreToolUse"), [])
        self.assertIn("herdr-agent-state.sh", " ".join(self.commands_in(payload, "SessionStart")))
        self.assertEqual(payload["permissions"], HERDR_CLAUDE["permissions"])


class TestPipedInstall(InstallerCase):
    """`curl … | bash` must fetch its own payload and leave nothing behind.

    Simulated with a bare directory holding only install.sh, piped into bash,
    and a file:// URL for the repo (the same shape as a real git clone).
    """

    def pipe(self, source_dir, *args, env_extra=None):
        env = dict(self.env)
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", "-s", "--", *args],
            input=pathlib.Path(source_dir, "install.sh").read_text(encoding="utf-8"),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=source_dir,
        )

    def test_piped_install_fetches_its_own_payload(self):
        bare = tempfile.mkdtemp(prefix="ajq-bare-")
        self.addCleanup(shutil.rmtree, bare, True)
        shutil.copy2(INSTALL, pathlib.Path(bare, "install.sh"))
        self.assertEqual(
            sorted(os.listdir(bare)), ["install.sh"], "the bare dir must start empty"
        )
        result = self.pipe(
            bare,
            "--no-daemon",
            "--target",
            "claude",
            env_extra={"AJQ_REPO_URL": f"file://{REPO}", "AJQ_FETCH": "git"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fetching", result.stdout)
        home = pathlib.Path(self.home)
        self.assertTrue((home / ".local/bin/ajq").exists())
        self.assertTrue((home / ".local/share/ajq/hooks/ajq-ensure.sh").exists())
        self.assertTrue((home / ".claude/skills/ajq/SKILL.md").exists())
        self.assertEqual(
            sorted(os.listdir(bare)), ["install.sh"], "the bare dir must be left clean"
        )

    def test_piped_install_tarball_path(self):
        bare = tempfile.mkdtemp(prefix="ajq-bare-")
        self.addCleanup(shutil.rmtree, bare, True)
        shutil.copy2(INSTALL, pathlib.Path(bare, "install.sh"))
        archive = pathlib.Path(tempfile.mkdtemp(prefix="ajq-tgz-"), "ajq.tgz")
        self.addCleanup(shutil.rmtree, archive.parent, True)
        subprocess.run(
            ["git", "archive", "--format=tar.gz", "-o", str(archive), "HEAD"],
            cwd=REPO,
            check=True,
            capture_output=True,
        )
        server = subprocess.Popen(
            # -u: the port line goes to a pipe, so it must not sit in a buffer
            [sys.executable, "-u", "-m", "http.server", "0", "--bind", "127.0.0.1"],
            cwd=archive.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(server.terminate)
        # http.server prints the port it bound; read it instead of guessing
        port = ""
        for _ in range(100):
            line = server.stdout.readline()
            if not line:
                break
            match = re.search(r"port (\d+)", line)
            if match:
                port = match.group(1)
                break
        self.assertTrue(port, "test http server never reported its port")
        result = self.pipe(
            bare,
            "--no-daemon",
            "--target",
            "claude",
            env_extra={
                "AJQ_FETCH": "tarball",
                "AJQ_TARBALL_URL": f"http://127.0.0.1:{port}/ajq.tgz",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fetching", result.stdout)
        self.assertTrue((pathlib.Path(self.home) / ".local/bin/ajq").exists())

    def test_bootstrap_is_a_noop_inside_a_checkout(self):
        result = subprocess.run(
            ["bash", str(INSTALL), "--dry-run", "--no-daemon", "--target", "claude"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=REPO,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("fetching", result.stdout)
        self.assertIn("would install", result.stdout)

    def test_unreachable_repo_reports_actionable_error(self):
        bare = tempfile.mkdtemp(prefix="ajq-bare-")
        self.addCleanup(shutil.rmtree, bare, True)
        shutil.copy2(INSTALL, pathlib.Path(bare, "install.sh"))
        result = self.pipe(
            bare,
            "--no-daemon",
            env_extra={"AJQ_REPO_URL": "file:///nonexistent-ajq-repo", "AJQ_FETCH": "git"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not fetch", result.stderr)
        self.assertIn("clone by hand", result.stderr)


class TestUninstall(InstallerCase):
    def test_uninstall_removes_installed_artifacts(self):
        self.install("--target", "claude,opencode,codex")
        self.uninstall()
        home = pathlib.Path(self.home)
        self.assertFalse((home / ".local/bin/ajq").exists())
        self.assertFalse((home / ".claude/skills/ajq/SKILL.md").exists())
        self.assertFalse((home / ".config/opencode/plugins/ajq.js").exists())
        self.assertFalse((home / ".codex/skills/ajq/SKILL.md").exists())
        self.assertFalse((home / ".local/share/ajq/installed.txt").exists())
        # the user config is kept unless --purge
        self.assertTrue((home / ".config/ajq/config.json").exists())

    def test_purge_removes_state_and_config(self):
        self.install("--target", "claude")
        state = pathlib.Path(self.home) / ".local/state/ajq/jobs"
        state.mkdir(parents=True, exist_ok=True)
        (state / "j-x").write_text("log", encoding="utf-8")
        self.uninstall("--purge")
        home = pathlib.Path(self.home)
        self.assertFalse((home / ".config/ajq").exists())
        self.assertFalse((home / ".local/state/ajq").exists())

    def test_uninstall_is_safe_when_nothing_installed(self):
        self.uninstall()  # must not fail


if __name__ == "__main__":
    unittest.main()