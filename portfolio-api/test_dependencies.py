# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_dependencies.py -- guards on requirements.txt (the production image installs exactly these pins)."""
import os, re, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
REQ = open(os.path.join(_HERE, "requirements.txt"), encoding="utf-8").read()
APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
REGISTER = os.path.join(_HERE, "..", "snaptrade_register.py")


def pins():
    out = {}
    for line in REQ.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?(==|>=|<=|~=|>|<)?\s*([0-9][^\s;]*)?", line)
        out[m.group(1).lower()] = (m.group(3), m.group(4))
    return out


class TestRequirements(unittest.TestCase):
    def test_every_requirement_is_pinned_exactly(self):
        for name, (op, ver) in pins().items():
            self.assertEqual(op, "==", f"{name} must be pinned with == (reproducible image, auditable by pip-audit)")
            self.assertTrue(ver, name)

    def test_snaptrade_stays_on_the_sdk_line_the_code_was_written_for(self):
        """SnapTrade 12.1+/13.x require SnapTrade(auth=...) and return typed models; app.py and
        snaptrade_register.py still use SnapTrade(consumer_key=..., client_id=...). Bumping the pin
        without migrating both (and testing against a live SnapTrade account) would break the
        optional SnapTrade provider silently, because no unit test reaches the network."""
        op, ver = pins()["snaptrade-python-sdk"]
        uses_old_constructor = "SnapTrade(consumer_key=ckey, client_id=cid)" in APP
        if uses_old_constructor:
            major, minor = (int(x) for x in ver.split(".")[:2])
            self.assertEqual((major, minor), (12, 0),
                             "snaptrade-python-sdk >= 12.1 needs the code migrated to SnapTrade(auth=...) first")
            self.assertIn("SnapTrade(consumer_key=ckey, client_id=cid)", open(REGISTER, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
