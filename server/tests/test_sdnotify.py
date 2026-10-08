from __future__ import annotations

import os
import socket
import tempfile
import unittest
from unittest import mock

from server import sdnotify


class SdNotifyTest(unittest.TestCase):
    def test_noop_without_notify_socket(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sdnotify.ready())
            self.assertFalse(sdnotify.watchdog())

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requiere sockets AF_UNIX")
    def test_messages_reach_the_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notify.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.addCleanup(server.close)
            server.bind(path)
            server.settimeout(2)
            with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}, clear=True):
                self.assertTrue(sdnotify.ready())
                self.assertEqual(server.recv(100), b"READY=1")
                self.assertTrue(sdnotify.watchdog())
                self.assertEqual(server.recv(100), b"WATCHDOG=1")
                self.assertTrue(sdnotify.stopping())
                self.assertEqual(server.recv(100), b"STOPPING=1")

    def test_unreachable_socket_is_not_fatal(self):
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": "/no/existe.sock"}, clear=True):
            self.assertFalse(sdnotify.watchdog())

    def test_watchdog_interval_is_half_the_deadline(self):
        with mock.patch.dict(os.environ, {"WATCHDOG_USEC": "30000000"}, clear=True):
            self.assertEqual(sdnotify.watchdog_interval(), 15.0)

    def test_watchdog_interval_none_when_not_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(sdnotify.watchdog_interval())

    def test_watchdog_interval_ignored_for_another_pid(self):
        env = {"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": str(os.getpid() + 1)}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(sdnotify.watchdog_interval())

    def test_watchdog_interval_garbage(self):
        with mock.patch.dict(os.environ, {"WATCHDOG_USEC": "abc"}, clear=True):
            self.assertIsNone(sdnotify.watchdog_interval())


if __name__ == "__main__":
    unittest.main()
