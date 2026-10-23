# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_mix_pies.py -- DASHBOARD mix pies (holdings / sector / asset class), requested 2026-10-23: the cockpit warns about a heavy sector or
a single name; these show the mix. Sector is in its honest form: funds are ONE slice, never broken down."""
import json, os, re, shutil, subprocess, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def _fn(name):
    i = HTML.index(f"function {name}(")
    d, k = 0, HTML.index("{", i)
    while True:
        d += HTML[k] == "{"; d -= HTML[k] == "}"; k += 1
        if d == 0: return HTML[i:k]


PRELUDE = ("const FUNDS_SECTOR = 'Mutual funds & ETFs (not sector-classified)'; const SECTOR_CAP_PCT = 35;\n"
           + _fn("mixTopN") + _fn("mixHoldings") + _fn("mixSector") + _fn("mixAssetClass"))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestBuilders(unittest.TestCase):
    def js(self, body):
        r = subprocess.run(["node", "-e", PRELUDE + body], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_holdings_top_eight_plus_other_and_the_sum_is_preserved(self):
        out = self.js("""
        const cons=[]; for(let i=1;i<=12;i++) cons.push({ticker:'T'+i, value:1000-i*10});
        const m=mixHoldings({consolidated:cons}, null, null, false);
        console.log(JSON.stringify([m.items.length, m.items[8].label, m.items[8].other, Math.round(m.items.reduce((t,r)=>t+r.value,0)), m.total]));""")
        self.assertEqual(out[:3], [9, "Other (4)", True]); self.assertEqual(out[3], out[4])

    def test_a_name_over_your_cap_is_flagged_and_without_a_policy_20_percent_is(self):
        out = self.js("""
        const cons=[{ticker:'BIG',value:600},{ticker:'MID',value:300},{ticker:'SM',value:100}];
        const withPolicy=mixHoldings({consolidated:cons}, null, {concentration:[{ticker:'MID',cap_pct:10}]}, false).items.map(i=>[i.label,i.flag]);
        const noPolicy=mixHoldings({consolidated:cons}, null, null, false).items.map(i=>[i.label,i.flag]);
        console.log(JSON.stringify([withPolicy,noPolicy]));""")
        self.assertEqual(out[0], [["BIG", False], ["MID", True], ["SM", False]])      # the policy list decides, not a guess
        self.assertEqual(out[1], [["BIG", True], ["MID", True], ["SM", False]])        # fallback: > 20%

    def test_include_fund_contents_uses_the_look_through_and_labels_what_cannot_be_itemised(self):
        out = self.js("""
        const lt={invested_value:1000, true_exposure:[{ticker:'AAPL',total_value:300},{ticker:'FUND:XYZ',total_value:150},{ticker:'RESID:ABC',total_value:50},{name:'Some Co',ticker:null,total_value:100}]};
        const m=mixHoldings({consolidated:[]}, lt, {concentration:[{ticker:'AAPL'}]}, true);
        console.log(JSON.stringify([m.items.map(i=>[i.label,i.value,i.flag]), m.total]));""")
        slices, total = out
        self.assertEqual(total, 1000)
        self.assertEqual(sorted(s[0] for s in slices), ["AAPL", "Fund contents not itemized", "Smaller holdings", "Some Co"])
        by = {s[0]: s for s in slices}
        self.assertEqual((by["AAPL"][1], by["AAPL"][2]), (300, True))                       # flagged: it is over the cap
        self.assertEqual(by["Fund contents not itemized"][1], 200)                          # FUND: + RESID: rows combined
        self.assertEqual(by["Smaller holdings"][1], 400)                                    # the remainder so the slices sum to the total
        self.assertEqual(sum(s[1] for s in slices), 1000)

    def test_fund_view_falls_back_to_direct_positions_with_a_note_when_there_is_no_look_through(self):
        out = self.js("""
        const m=mixHoldings({consolidated:[{ticker:'Z',value:10}]}, null, null, true);
        console.log(JSON.stringify([m.items.map(i=>i.label), m.note]));""")
        self.assertEqual(out[0], ["Z"]); self.assertIn("not available yet", out[1])

    def test_sector_funds_are_one_labelled_slice_and_a_sector_over_35_is_flagged(self):
        out = self.js("""
        const exp={by_sector:[{sector:'Technology',value:500},{sector:'Funds (no look-through)',value:400},{sector:'Other',value:60},{sector:'Cash',value:40}]};
        const m=mixSector(exp);
        console.log(JSON.stringify([m.items.map(i=>[i.label,i.flag]), m.note.includes('one slice')]));""")
        self.assertEqual(out[0], [["Technology", True], ["Mutual funds & ETFs (not sector-classified)", False], ["Other / unclassified", False], ["Cash", False]])
        self.assertTrue(out[1])                                                          # funds are never "flagged" as a sector breach

    def test_sector_and_asset_class_degrade_to_a_message_not_an_empty_chart(self):
        out = self.js("""console.log(JSON.stringify([mixSector(null).available, mixSector({error:'x'}).available, mixSector({by_sector:[]}).available,
          mixAssetClass(null).available, mixAssetClass({}).available, mixSector(null).note, mixAssetClass(null).note]));""")
        self.assertEqual(out[:5], [False] * 5); self.assertIn("still being computed", out[5]); self.assertIn("LOOK-THROUGH", out[6])

    def test_asset_class_from_the_look_through(self):
        out = self.js("""
        const m=mixAssetClass({asset_class_value:{'US Equity':700,'Bond':200,'Cash':100,'Other':0}, funds_resolved:5, funds_total:6});
        console.log(JSON.stringify([m.items.map(i=>i.label), m.total, m.note]));""")
        self.assertEqual(out[0], ["US Equity", "Bond", "Cash"]); self.assertEqual(out[1], 1000); self.assertIn("5 of 6 funds looked through", out[2])


class TestWiring(unittest.TestCase):
    def test_panels_chart_painting_and_docs(self):
        for needle in ("mixPanel('MIX · HOLDINGS'", "mixPanel('MIX · SECTOR'", "mixPanel('MIX · ASSET CLASS'", "MIXCHARTS.forEach(m=>{",
                       "INCL. FUND CONTENTS", "W('mix-pies'", "over your limit — the cockpit flags this", "FUNDS_SECTOR = 'Mutual funds & ETFs (not sector-classified)'"):
            self.assertIn(needle, HTML)

    def test_the_two_existing_pies_are_untouched(self):
        self.assertIn("mk('c-inst', d.by_institution.slice(0,8));\n  mk('c-type', d.by_type.slice(0,8));", HTML)


if __name__ == "__main__":
    unittest.main()
