"""Shared test fixtures for ajq.

Import this module FIRST in every test file: it puts `src/` on sys.path and
pins the state/config/socket environment before ajq.paths is imported.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

_TMP_ROOT = tempfile.mkdtemp(prefix="ajq-tests-")
os.environ.setdefault("AJQ_STATE_DIR", os.path.join(_TMP_ROOT, "state"))
os.environ.setdefault("AJQ_CONFIG", os.path.join(_TMP_ROOT, "config", "ajq", "config.json"))
os.environ.setdefault("AJQ_SOCKET", os.path.join(_TMP_ROOT, "run", "ajqd.sock"))
# the suite must never spawn a real daemon that outlives the test run
os.environ["AJQ_NO_AUTOSTART"] = "1"

from ajq import config as ajq_config  # noqa: E402
from ajq import paths  # noqa: E402


@contextlib.contextmanager
def isolated_state():
    """Point ajq.paths at a throwaway state dir for the duration of a test."""
    root = tempfile.mkdtemp(prefix="ajq-state-", dir=_TMP_ROOT)
    saved = {
        "STATE_DIR": paths.STATE_DIR,
        "DB_PATH": paths.DB_PATH,
        "CONFIG_PATH": paths.CONFIG_PATH,
        "SOCKET_PATH": paths.SOCKET_PATH,
    }
    paths.STATE_DIR = root
    paths.DB_PATH = os.path.join(root, "state.db")
    paths.CONFIG_PATH = os.path.join(root, "config.json")
    paths.SOCKET_PATH = os.path.join(root, "ajqd.sock")
    paths.ensure_dir(root)
    try:
        yield root
    finally:
        paths.STATE_DIR = saved["STATE_DIR"]
        paths.DB_PATH = saved["DB_PATH"]
        paths.CONFIG_PATH = saved["CONFIG_PATH"]
        paths.SOCKET_PATH = saved["SOCKET_PATH"]
        shutil.rmtree(root, ignore_errors=True)


def load_config(**overrides):
    """A Config built from DEFAULTS with a deep-merged `overrides` dict."""
    return ajq_config.load_config(path=None, overrides=overrides)


def store():
    """An open Store in an isolated state dir; caller closes it."""
    from ajq.store import Store

    return Store(paths.DB_PATH)


def wait_until(predicate, timeout=10.0, interval=0.05):
    """Poll `predicate` until it is truthy; return its last value or None."""
    import time

    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return value


class AjqTestCase(unittest.TestCase):
    """Base class giving every test an isolated state dir."""

    def setUp(self):
        self._iso = isolated_state()
        self.state_dir = self._iso.__enter__()

    def tearDown(self):
        self._iso.__exit__(None, None, None)