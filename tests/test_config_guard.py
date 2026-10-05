"""Config precedence and command-classifier tests."""

from __future__ import annotations

import json
import os
import unittest

import helpers

from ajq import config as ajq_config
from ajq import guard


class TestConfig(helpers.AjqTestCase):
    def config_file(self, payload):
        os.makedirs(os.path.dirname(paths_config()), exist_ok=True)
        with open(paths_config(), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return paths_config()

    def test_defaults_available_without_a_file(self):
        config = ajq_config.load_config(path=os.path.join(self.state_dir, "missing.json"))
        self.assertEqual(config.get("limits.max_concurrent"), 8)
        self.assertEqual(config.get("limits.pools.heavy"), 2)
        self.assertEqual(config.get("defaults.timeout_s"), 1800)
        self.assertEqual(config.get("hooks.guard_mode"), "warn")

    def test_dotted_access(self):
        config = ajq_config.load_config(path=None)
        self.assertEqual(config["resources.nice"], 10)
        self.assertEqual(config.get("nope.nothing", "fallback"), "fallback")

    def test_user_file_overrides_defaults(self):
        path = self.config_file({"limits": {"pools": {"heavy": 1}}, "defaults": {"timeout_s": 5}})
        config = ajq_config.load_config(path=path)
        self.assertEqual(config.get("limits.pools.heavy"), 1)
        self.assertEqual(config.get("defaults.timeout_s"), 5)
        self.assertEqual(config.get("limits.max_concurrent"), 8)  # untouched default

    def test_flags_beat_file_and_env(self):
        path = self.config_file({"defaults": {"timeout_s": 5}})
        os.environ["AJQ_TIMEOUT_S"] = "99"
        self.addCleanup(os.environ.pop, "AJQ_TIMEOUT_S", None)
        config = ajq_config.load_config(path=path, overrides={"defaults": {"timeout_s": 7}})
        self.assertEqual(config.get("defaults.timeout_s"), 7)
        env_only = ajq_config.load_config(path=path)
        self.assertEqual(env_only.get("defaults.timeout_s"), 99)

    def test_malformed_file_falls_back(self):
        path = os.path.join(self.state_dir, "broken.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        config = ajq_config.load_config(path=path)  # must not raise
        self.assertEqual(config.get("defaults.timeout_s"), 1800)

    def test_seed_only_writes_when_absent(self):
        path = os.path.join(self.state_dir, "seed.json")
        self.assertTrue(ajq_config.seed_config(path))
        self.assertFalse(ajq_config.seed_config(path))
        self.assertTrue(ajq_config.seed_config(path, force=True))

    def test_default_config_json_is_valid_and_annotated(self):
        text = ajq_config.default_config_json()
        payload = json.loads(text)
        self.assertIn("_doc", payload)
        self.assertEqual(payload["limits"]["max_concurrent"], 8)

    def test_every_documented_env_var_is_honoured(self):
        cases = {
            "AJQ_MAX_CONCURRENT": ("limits.max_concurrent", "3"),
            "AJQ_TIMEOUT_S": ("defaults.timeout_s", "11"),
            "AJQ_MAX_OUTPUT_BYTES": ("defaults.max_output_bytes", "4096"),
            "AJQ_MEMORY_MB": ("resources.memory_mb", "777"),
            "AJQ_CPU_PERCENT": ("resources.cpu_percent", "55"),
            "AJQ_NICE": ("resources.nice", "7"),
            "AJQ_POOL": ("defaults.pool", "service"),
            "AJQ_GUARD_MODE": ("hooks.guard_mode", "block"),
            "AJQ_BACKEND": ("resources.backend", "posix"),
            "AJQ_LIGHT_THRESHOLD_S": ("estimates.light_threshold_s", "12"),
            "AJQ_KILL_GRACE_S": ("defaults.kill_grace_s", "4"),
        }
        for name, (dotted, value) in cases.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)
            got = ajq_config.load_config(path=None).get(dotted)
            self.assertEqual(str(got), value, f"{name} -> {dotted} was {got!r}")


class TestGuard(unittest.TestCase):
    def test_heavy_commands_land_in_heavy_pool(self):
        for argv in (
            ["npm", "run", "build"],
            ["pnpm", "test"],
            ["make"],
            ["cargo", "build", "--release"],
            ["go", "test", "./..."],
            ["./gradlew", "test"],
            ["mvn", "-q", "package"],
            ["docker", "build", "."],
            ["nix", "build"],
            ["tsc", "--noEmit"],
            ["next", "build"],
            ["pytest", "-q"],
            ["tox"],
        ):
            verdict = guard.classify(argv)
            self.assertIn(verdict["kind"], guard.HEAVY_KINDS, argv)
            self.assertEqual(verdict["pool"], "heavy", argv)
            self.assertTrue(verdict["heavy"], argv)

    def test_light_commands_land_in_light_pool(self):
        for argv in (
            ["eslint", "src"],
            ["prettier", "--check", "."],
            ["ruff", "check"],
            ["black", "--check"],
            ["gofmt", "-l", "."],
            ["rustfmt", "--check"],
            ["shellcheck", "run.sh"],
            ["git", "status"],
            ["rg", "todo"],
        ):
            verdict = guard.classify(argv)
            self.assertIn(verdict["kind"], guard.LIGHT_KINDS | {"check"}, argv)
            self.assertEqual(verdict["pool"], "light", argv)
            self.assertFalse(verdict["heavy"], argv)

    def test_unknown_command_is_normal_and_not_heavy(self):
        verdict = guard.classify(["some-random-binary", "--flag"])
        self.assertEqual(verdict["pool"], "normal")
        self.assertFalse(verdict["heavy"])
        self.assertEqual(verdict["kind"], "unknown")

    def test_verdict_reports_tool_and_reason(self):
        verdict = guard.classify(["pytest", "-q", "tests/"])
        self.assertEqual(verdict["tool"], "pytest")
        self.assertTrue(verdict["reason"])

    def test_is_heavy_command_string_form(self):
        self.assertTrue(guard.is_heavy_command("npm run build && echo done"))
        self.assertFalse(guard.is_heavy_command("ls -la"))
        self.assertFalse(guard.is_heavy_command('echo "unbalanced ( quote'))

    def test_explain_mentions_the_recommended_command(self):
        text = guard.explain(["npm", "run", "build"])
        self.assertIn("ajq", text)
        self.assertIn("heavy", text.lower())

    def test_tool_names_are_unwrapped(self):
        """Launchers must not become the cache key, or the same tool never hits."""
        cases = {
            ("uv", "run", "pytest", "-q"): "pytest",
            ("poetry", "run", "mypy", "."): "mypy",
            ("env", "FOO=1", "pytest"): "pytest",
            ("FOO=1", "pytest"): "pytest",
            ("nice", "-n", "10", "cargo", "build"): "cargo",
            ("timeout", "300", "pytest", "-q"): "pytest",
            ("python3", "-m", "pytest", "-q"): "pytest",
            ("npm", "run", "build"): "npm:build",
        }
        for argv, expected in cases.items():
            self.assertEqual(guard.classify(list(argv))["tool"], expected, argv)

    def test_inline_code_never_becomes_the_tool_name(self):
        """`python -c "<code>"` would otherwise mint a cache key per snippet."""
        for argv in (
            ["python3", "-c", "import time; time.sleep(120)"],
            ["python", "-c", "print(1)"],
        ):
            tool = guard.classify(argv)["tool"]
            self.assertEqual(tool, os.path.basename(argv[0]))
            self.assertLessEqual(len(tool), 32)

    def test_tool_names_are_length_bounded(self):
        tool = guard.classify(["some-tool-" + "x" * 200])["tool"]
        self.assertLessEqual(len(tool), 32)


def paths_config():
    from ajq import paths

    return paths.CONFIG_PATH


if __name__ == "__main__":
    unittest.main()