"""The bind-host refusal in ``aicouncil.__main__``.

This app can execute a coding agent with auto-approve flags, so leaving the
loopback interface is a real change in exposure. The check is a pure function
precisely so it can be tested without spinning up a socket or a container -
the desktop path and the Docker path share this one rule.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil.__main__ import LOOPBACK_HOSTS, _bind_refusal, _truthy_env  # noqa: E402


class TestBindRefusal(unittest.TestCase):
    def test_loopback_hosts_are_always_fine(self):
        for host in LOOPBACK_HOSTS:
            self.assertIsNone(_bind_refusal(host, allow_lan=False))
            self.assertIsNone(_bind_refusal(host, allow_lan=True))

    def test_a_lan_host_is_refused_without_the_flag(self):
        message = _bind_refusal("0.0.0.0", allow_lan=False)
        self.assertIsNotNone(message)
        self.assertIn("--allow-lan", message)

    def test_a_lan_host_is_permitted_with_the_flag(self):
        self.assertIsNone(_bind_refusal("0.0.0.0", allow_lan=True))

    def test_any_non_loopback_address_needs_the_flag_too(self):
        self.assertIsNotNone(_bind_refusal("192.168.1.50", allow_lan=False))
        self.assertIsNone(_bind_refusal("192.168.1.50", allow_lan=True))


class TestTruthyEnv(unittest.TestCase):
    def setUp(self):
        import os
        self.os = os
        self._previous = os.environ.get("AI_COUNCIL_ALLOW_LAN")

    def tearDown(self):
        if self._previous is None:
            self.os.environ.pop("AI_COUNCIL_ALLOW_LAN", None)
        else:
            self.os.environ["AI_COUNCIL_ALLOW_LAN"] = self._previous

    def test_recognised_spellings(self):
        for value in ("1", "true", "True", "yes", "on"):
            self.os.environ["AI_COUNCIL_ALLOW_LAN"] = value
            self.assertTrue(_truthy_env("AI_COUNCIL_ALLOW_LAN"), value)

    def test_unset_or_falsy_is_false(self):
        self.os.environ.pop("AI_COUNCIL_ALLOW_LAN", None)
        self.assertFalse(_truthy_env("AI_COUNCIL_ALLOW_LAN"))
        for value in ("0", "false", "no", ""):
            self.os.environ["AI_COUNCIL_ALLOW_LAN"] = value
            self.assertFalse(_truthy_env("AI_COUNCIL_ALLOW_LAN"), value)


if __name__ == "__main__":
    unittest.main()
