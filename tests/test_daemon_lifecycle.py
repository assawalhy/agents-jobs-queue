"""Daemon lifecycle: one daemon per state dir, and safe socket teardown."""

from __future__ import annotations

import os
import socket
import unittest
from unittest import mock

import helpers

from ajq import daemon as ajq_daemon


def _start(server) -> None:
    """startup() without installing signal handlers in the test process."""
    with mock.patch.object(ajq_daemon._Server, "_install_signals"):
        server.startup()


class TestSingleton(helpers.AjqTestCase):
    def test_second_daemon_refuses_the_state_dir(self):
        first = ajq_daemon._Server()
        second = ajq_daemon._Server()
        try:
            _start(first)
            with self.assertRaises(RuntimeError) as caught:
                _start(second)
            self.assertIn("another ajqd", str(caught.exception))
            self.assertIsNone(second._lock_fd)
        finally:
            if first.listener is not None:
                first.teardown()

    def test_release_lets_the_next_daemon_start(self):
        first = ajq_daemon._Server()
        _start(first)
        first.teardown()
        second = ajq_daemon._Server()
        try:
            _start(second)
            self.assertIsNotNone(second._lock_fd)
        finally:
            if second.listener is not None:
                second.teardown()

    def test_teardown_keeps_a_replaced_socket(self):
        server = ajq_daemon._Server()
        _start(server)
        path = server.socket_path
        # Another daemon takes over the path: the file is a new inode now.
        os.unlink(path)
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(path)
        replacement.listen(1)
        try:
            server._unlink_socket()
            self.assertTrue(os.path.exists(path))
        finally:
            replacement.close()
            try:
                os.unlink(path)
            except OSError:
                pass
            server.teardown()


class TestEnsurePreference(helpers.AjqTestCase):
    def test_installed_unit_does_not_spawn_detached(self):
        with mock.patch.object(ajq_daemon, "_unit_installed", return_value=True), \
                mock.patch.object(ajq_daemon, "_systemctl_start", return_value=False), \
                mock.patch.object(ajq_daemon.platform, "IS_LINUX", True), \
                mock.patch.object(ajq_daemon, "spawn_detached") as spawn, \
                mock.patch.object(ajq_daemon, "_autostart_disabled", return_value=False), \
                mock.patch.object(ajq_daemon, "_wait_for_socket", return_value=False):
            self.assertFalse(ajq_daemon.ensure_running(0.5))
            spawn.assert_not_called()

    def test_no_unit_falls_back_to_detached_spawn(self):
        with mock.patch.object(ajq_daemon, "_unit_installed", return_value=False), \
                mock.patch.object(ajq_daemon, "spawn_detached", return_value=True) as spawn, \
                mock.patch.object(ajq_daemon, "_autostart_disabled", return_value=False), \
                mock.patch.object(ajq_daemon, "_wait_for_socket", return_value=True):
            self.assertTrue(ajq_daemon.ensure_running(0.5))
            spawn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
