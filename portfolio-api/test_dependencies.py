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

    def test_snaptrade_code_matches_the_pinned_sdk_generation(self):
        """The code must use the constructor style of the pinned SDK major: 13.x needs SnapTrade(auth=SnapTradeAuth...);
        12.0.x accepted SnapTrade(consumer_key=..., client_id=...). Bumping/downgrading the pin without
        the matching code would break the optional SnapTrade provider silently (no unit test reaches the network)."""
        op, ver = pins()["snaptrade-python-sdk"]
        major = int(ver.split(".")[0])
        integ = open(os.path.join(_HERE, "snaptrade_integration.py"), encoding="utf-8").read()
        reg = open(REGISTER, encoding="utf-8").read()
        if major >= 13:
            for src in (integ, reg):
                self.assertIn("SnapTradeAuth", src)
                self.assertNotIn("SnapTrade(consumer_key=", src)
            self.assertNotIn("SnapTrade(consumer_key=ckey, client_id=cid)", APP)
        else:
            self.fail("snaptrade-python-sdk < 13: the integration was migrated to 13.x (SnapTradeAuth); pin must stay >= 13")


if __name__ == "__main__":
    unittest.main()
