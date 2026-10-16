import os
import sys
import subprocess
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import noded


class NodeAddressDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.socket_patch = mock.patch.object(noded.socket, "socket")
        sock = self.socket_patch.start().return_value
        sock.getsockname.return_value = ("192.168.1.10", 12345)
        self.addCleanup(self.socket_patch.stop)
        self.host_patch = mock.patch.object(
            noded.socket, "gethostbyname_ex",
            return_value=("test-host", [], ["127.0.0.1", "192.168.1.10"]),
        )
        self.host_patch.start()
        self.addCleanup(self.host_patch.stop)

    def test_windows_repeated_probes_suppress_console_and_keep_adapter_addresses(self):
        no_window = 0x08000000
        output = "IPv4 Address . . . : 192.168.1.10\nIPv4 Address . . . : 100.64.1.5\n"
        with mock.patch.object(noded.os, "name", "nt"), \
             mock.patch.object(noded.subprocess, "CREATE_NO_WINDOW", no_window, create=True), \
             mock.patch.object(noded.subprocess, "run", return_value=mock.Mock(stdout=output)) as run:
            for _ in range(2):
                self.assertEqual(noded._local_ips(), ["192.168.1.10", "100.64.1.5"])
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0], ["ipconfig"])
            self.assertTrue(call.kwargs.get("creationflags", 0) & no_window)

    def test_posix_address_discovery_does_not_pass_windows_flags(self):
        output = "inet 192.168.1.10 netmask 0xffffff00\ninet 10.0.0.5 netmask 0xffffff00\n"
        with mock.patch.object(noded.os, "name", "posix"), \
             mock.patch.object(noded.subprocess, "run", return_value=mock.Mock(stdout=output)) as run:
            self.assertEqual(noded._local_ips(), ["192.168.1.10", "10.0.0.5"])
        self.assertEqual(run.call_args.args[0], ["ifconfig"])
        self.assertNotIn("creationflags", run.call_args.kwargs)

    def test_probe_failure_keeps_socket_discovered_address(self):
        with mock.patch.object(noded.os, "name", "nt"), \
             mock.patch.object(noded.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             mock.patch.object(noded.subprocess, "run", side_effect=subprocess.TimeoutExpired("ipconfig", 5)):
            self.assertEqual(noded._local_ips(), ["192.168.1.10"])


if __name__ == "__main__":
    unittest.main()
