"""Tests for the update check and the update install."""

from __future__ import annotations

import contextlib
import io
import json
import os
import ssl
import tempfile
import unittest
import urllib.error
from unittest import mock

import helpers

from ajq import cli, paths, update

ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:/assawalhy/agents-jobs-queue/v0.2.0</id>
    <link rel="alternate" type="text/html"
          href="https://github.com/assawalhy/agents-jobs-queue/releases/tag/v0.2.0"/>
    <title>v0.2.0</title>
    <content type="html">&lt;p&gt;Second release.&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:/assawalhy/agents-jobs-queue/v0.1.2</id>
    <link rel="alternate" type="text/html"
          href="https://github.com/assawalhy/agents-jobs-queue/releases/tag/v0.1.2"/>
    <title>v0.1.2</title>
    <content type="html">&lt;p&gt;First release.&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:/assawalhy/agents-jobs-queue/v0.1.0-rc1</id>
    <link rel="alternate" type="text/html"
          href="https://github.com/assawalhy/agents-jobs-queue/releases/tag/v0.1.0-rc1"/>
    <title>v0.1.0-rc1</title>
    <content type="html">&lt;p&gt;Release candidate.&lt;/p&gt;</content>
  </entry>
</feed>
"""

API_BODY = json.dumps(
    {
        "tag_name": "v0.2.0",
        "name": "0.2.0",
        "body": "Second release.",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "ajq.zip",
                "size": 1234,
                "browser_download_url": "https://example.test/ajq.zip",
            }
        ],
    }
)

REGISTRY = (
    "version|v0.1.2| 0.1.0\n"
    "shared|/home/you/.local/share/ajq| hooks/ajq-ensure.sh hooks/ajq-session-start.sh\n"
    "opencode|/home/you/.config/opencode/skills| skills/ajq/SKILL.md harnesses/opencode/ajq.js\n"
    "claude|/home/you/.claude/skills| skills/ajq/SKILL.md\n"
)


class TestParseVersion(unittest.TestCase):
    def test_a_tag_is_a_version(self):
        self.assertEqual(update.parse_version("v0.1.2"), (0, 1, 2))

    def test_a_bare_version_is_a_version(self):
        self.assertEqual(update.parse_version("0.2"), (0, 2))

    def test_a_prerelease_keeps_the_numbers(self):
        self.assertEqual(update.parse_version("v1.2.3-rc1"), (1, 2, 3))

    def test_a_branch_is_not_a_version(self):
        self.assertIsNone(update.parse_version("main"))
        self.assertIsNone(update.parse_version("feat/tui"))
        self.assertIsNone(update.parse_version(""))

    def test_a_prerelease_sorts_below_the_release(self):
        self.assertLess(update.version_key("v1.2.3-rc1"), update.version_key("v1.2.3"))

    def test_the_numbers_are_padded_for_the_comparison(self):
        self.assertEqual(update.version_key("0.2"), update.version_key("0.2.0"))

    def test_the_version_of_a_ref_drops_the_v(self):
        self.assertEqual(update.version_of("v0.1.2"), "0.1.2")
        self.assertIsNone(update.version_of("main"))


class TestParseAtom(unittest.TestCase):
    def setUp(self):
        self.entries = update.parse_atom(ATOM_FEED)

    def test_every_entry_is_parsed(self):
        self.assertEqual(len(self.entries), 3)

    def test_the_refs_are_the_titles(self):
        self.assertEqual(
            [entry["ref"] for entry in self.entries],
            ["v0.2.0", "v0.1.2", "v0.1.0-rc1"],
        )

    def test_the_notes_are_the_contents(self):
        self.assertEqual(self.entries[0]["note"], "Second release.")
        self.assertEqual(self.entries[2]["note"], "Release candidate.")

    def test_a_broken_feed_is_an_error(self):
        with self.assertRaises(update.UpdateError):
            update.parse_atom("not xml at all")


class TestParseApi(unittest.TestCase):
    def setUp(self):
        self.release = update.parse_api_release(API_BODY)

    def test_the_ref_is_the_tag(self):
        self.assertEqual(self.release["ref"], "v0.2.0")

    def test_the_note_is_the_body(self):
        self.assertEqual(self.release["note"], "Second release.")

    def test_the_assets_are_listed(self):
        self.assertEqual(len(self.release["assets"]), 1)
        self.assertEqual(self.release["assets"][0]["name"], "ajq.zip")

    def test_a_body_that_is_not_json_is_an_error(self):
        with self.assertRaises(update.UpdateError):
            update.parse_api_release("<html>rate limited</html>")


class TestLatest(unittest.TestCase):
    def test_the_highest_version_wins(self):
        candidates = update.parse_atom(ATOM_FEED)
        self.assertEqual(update.latest(candidates)["ref"], "v0.2.0")

    def test_a_release_wins_over_a_tag_of_the_same_version(self):
        candidates = [
            {"ref": "v0.2.0", "note": "from a tag", "release": False},
            {"ref": "v0.2.0", "note": "from the api", "release": True},
        ]
        winner = update.latest(candidates)
        self.assertEqual(winner["note"], "from the api")

    def test_a_prerelease_loses_to_the_release(self):
        candidates = [
            {"ref": "v1.0.0-rc1", "note": "rc", "release": True},
            {"ref": "v1.0.0", "note": "final", "release": True},
        ]
        self.assertEqual(update.latest(candidates)["ref"], "v1.0.0")

    def test_a_ref_that_is_not_a_version_is_ignored(self):
        candidates = [{"ref": "main", "note": "", "release": False}]
        self.assertIsNone(update.latest(candidates))


class TestInstalled(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ajq-registry-")
        self.path = os.path.join(self.dir, "installed.txt")

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_the_registry_line_gives_the_ref_and_the_version(self):
        self.write(REGISTRY)
        state = update.read_installed(self.path)
        self.assertEqual(state["ref"], "v0.1.2")
        self.assertEqual(state["version"], "0.1.2")
        self.assertTrue(state["tracked"])

    def test_a_ref_that_is_not_a_version_falls_back_to_the_code_version(self):
        self.write("version|main| 0.1.0\n")
        state = update.read_installed(self.path)
        self.assertEqual(state["ref"], "main")
        self.assertEqual(state["version"], "0.1.0")

    def test_a_missing_registry_is_not_an_error(self):
        state = update.read_installed(os.path.join(self.dir, "absent.txt"))
        self.assertIsNone(state["ref"])
        self.assertIsNone(state["version"])
        self.assertFalse(state["tracked"])

    def test_a_registry_without_a_version_line_is_untracked(self):
        self.write("shared|/home/you/.local/share/ajq| hooks/ajq-ensure.sh\n")
        state = update.read_installed(self.path)
        self.assertFalse(state["tracked"])

    def test_the_harnesses_are_the_other_registry_keys(self):
        self.write(REGISTRY)
        self.assertEqual(update.read_harnesses(self.path), ["opencode", "claude"])

    def test_a_missing_registry_names_no_harness(self):
        self.assertEqual(update.read_harnesses(os.path.join(self.dir, "absent.txt")), [])


class TestReport(unittest.TestCase):
    def test_an_update_is_available(self):
        installed = {"ref": "v0.1.2", "version": "0.1.2"}
        newest = {"ref": "v0.2.0", "note": "Second release.", "release": True}
        report = update.build_report(installed, newest)
        self.assertEqual(report["installed_version"], "0.1.2")
        self.assertEqual(report["installed_ref"], "v0.1.2")
        self.assertEqual(report["latest_version"], "0.2.0")
        self.assertEqual(report["latest_ref"], "v0.2.0")
        self.assertTrue(report["update_available"])
        self.assertEqual(report["note"], "Second release.")

    def test_no_update_is_available(self):
        installed = {"ref": "v0.2.0", "version": "0.2.0"}
        newest = {"ref": "v0.2.0", "note": "Second release.", "release": True}
        report = update.build_report(installed, newest)
        self.assertFalse(report["update_available"])

    def test_an_untracked_install_gets_the_newest(self):
        installed = {"ref": None, "version": None}
        newest = {"ref": "v0.2.0", "note": "", "release": True}
        report = update.build_report(installed, newest)
        self.assertTrue(report["update_available"])
        self.assertIsNone(report["installed_version"])


class TestInstall(unittest.TestCase):
    def test_the_command_is_the_copy_paste_form(self):
        command = update.install_command(update.REPO, "v0.2.0", ["opencode", "claude"])
        self.assertEqual(
            command,
            "curl -fsSL https://raw.githubusercontent.com/assawalhy/agents-jobs-queue/v0.2.0/install.sh"
            " | AJQ_VERSION=v0.2.0 bash -s -- --target opencode,claude",
        )

    def test_the_command_without_a_harness_has_no_arguments(self):
        command = update.install_command(update.REPO, "main", [])
        self.assertTrue(command.endswith("bash -s --"))

    def test_the_installer_runs_with_the_arguments_and_the_environment(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(update, "fetch", return_value="#!/usr/bin/env bash\n"), \
                mock.patch.object(update.subprocess, "run", runner):
            update.install(update.REPO, "v0.2.0", ["opencode", "claude"])
        args, kwargs = runner.call_args
        self.assertEqual(
            args[0], ["bash", "-s", "--", "--target", "opencode,claude"]
        )
        self.assertEqual(kwargs["input"], "#!/usr/bin/env bash\n")
        self.assertEqual(kwargs["env"]["AJQ_VERSION"], "v0.2.0")

    def test_a_failing_installer_is_an_error(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=1))
        with mock.patch.object(update, "fetch", return_value="script"), \
                mock.patch.object(update.subprocess, "run", runner):
            with self.assertRaises(update.UpdateError):
                update.install(update.REPO, "v0.2.0", [])

    def test_a_missing_bash_is_an_error(self):
        with mock.patch.object(update, "fetch", return_value="script"), \
                mock.patch.object(update.subprocess, "run", side_effect=FileNotFoundError("bash")):
            with self.assertRaises(update.UpdateError):
                update.install(update.REPO, "v0.2.0", [])

    def test_a_network_failure_is_an_error(self):
        with mock.patch.object(update, "fetch", side_effect=update.UpdateError("offline")):
            with self.assertRaises(update.UpdateError):
                update.install(update.REPO, "v0.2.0", [])

    def test_a_certificate_error_says_what_to_do(self):
        reason = ssl.SSLCertVerificationError("certificate verify failed")
        with mock.patch.object(
            update.urllib.request, "urlopen", side_effect=urllib.error.URLError(reason)
        ):
            with self.assertRaises(update.UpdateError) as caught:
                update.fetch("https://github.com/example")
        self.assertIn("SSL_CERT_FILE", str(caught.exception))


def fake_fetch(url: str) -> str:
    if url.endswith("releases.atom"):
        return ATOM_FEED
    if url.endswith("/releases/latest"):
        return API_BODY
    raise AssertionError(f"unexpected url: {url}")


class TestCli(helpers.AjqTestCase):
    def setUp(self):
        super().setUp()
        self.data_dir = tempfile.mkdtemp(prefix="ajq-update-")
        with open(os.path.join(self.data_dir, "installed.txt"), "w", encoding="utf-8") as handle:
            handle.write(REGISTRY)
        patcher = mock.patch.object(paths, "DATA_DIR", self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(update, "fetch", side_effect=fake_fetch):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_check_prints_the_report_as_json(self):
        code, out, _ = self.run_cli(["update", "--check", "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["installed_version"], "0.1.2")
        self.assertEqual(payload["installed_ref"], "v0.1.2")
        self.assertEqual(payload["latest_version"], "0.2.0")
        self.assertEqual(payload["latest_ref"], "v0.2.0")
        self.assertTrue(payload["update_available"])
        self.assertEqual(payload["note"], "Second release.")

    def test_check_is_zero_when_the_install_is_the_newest(self):
        with open(os.path.join(self.data_dir, "installed.txt"), "w", encoding="utf-8") as handle:
            handle.write("version|v0.2.0| 0.1.0\n")
        code, out, _ = self.run_cli(["update", "--check", "--json"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["update_available"])

    def test_check_is_two_when_the_check_cannot_be_made(self):
        with mock.patch.object(update, "fetch", side_effect=update.UpdateError("offline")):
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["update", "--check", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("offline", err.getvalue())

    def test_check_changes_nothing(self):
        code, out, _ = self.run_cli(["update", "--check"])
        self.assertEqual(code, 1)
        self.assertIn("installed  0.1.2", out)
        self.assertIn("v0.2.0", out)

    def test_a_failed_check_does_not_say_the_update_is_not_needed(self):
        with mock.patch.object(update, "fetch", side_effect=update.UpdateError("offline")):
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["update", "--check"])
        self.assertEqual(code, 2)
        self.assertIn("update     unknown", out.getvalue())
        self.assertNotIn("not needed", out.getvalue())

    def test_dry_run_prints_the_command_and_runs_nothing(self):
        with mock.patch.object(update, "install") as installer:
            code, out, _ = self.run_cli(["update", "--dry-run", "--yes"])
        self.assertEqual(code, 0)
        installer.assert_not_called()
        self.assertIn("AJQ_VERSION=v0.2.0", out)
        self.assertIn("--target opencode,claude", out)

    def test_the_install_runs_the_installer_from_the_ref(self):
        with mock.patch.object(update, "install", return_value=0) as installer:
            code, _, _ = self.run_cli(["update", "--yes"])
        self.assertEqual(code, 0)
        installer.assert_called_once_with(update.REPO, "v0.2.0", ["opencode", "claude"])

    def test_an_explicit_ref_is_installed_as_it_is(self):
        with mock.patch.object(update, "install", return_value=0) as installer:
            code, _, _ = self.run_cli(["update", "--ref", "main", "--yes"])
        self.assertEqual(code, 0)
        installer.assert_called_once_with(update.REPO, "main", ["opencode", "claude"])
