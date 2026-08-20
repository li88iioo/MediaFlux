from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.modules.network_addresses import (
    _linux_proc_addresses,
    discover_lan_ipv4_addresses,
)


class LinuxNetworkAddressTests(unittest.TestCase):
    def test_linux_proc_addresses_extracts_local_private_ips(self) -> None:
        sample_fib_trie = """
-     +-- 192.168.88.0/24 2 0 2
|-- 192.168.88.0
   /32 link BROADCAST
|-- 192.168.88.11
   /32 host LOCAL
|-- 192.168.88.255
   /32 link BROADCAST
-     +-- 127.0.0.0/8 2 0 2
|-- 127.0.0.1
   /32 host LOCAL
"""
        with tempfile.TemporaryDirectory() as temporary:
            fake_proc = Path(temporary) / "fib_trie"
            fake_proc.write_text(sample_fib_trie, encoding="utf-8")
            extracted = _linux_proc_addresses(proc_path=fake_proc)
            self.assertIn("192.168.88.11", extracted)
            lan_addresses = discover_lan_ipv4_addresses(
                hostname_source=lambda: (),
                route_source=lambda: (),
                proc_source=lambda: extracted,
            )
            self.assertEqual(lan_addresses, ["192.168.88.11"])


if __name__ == "__main__":
    unittest.main()
