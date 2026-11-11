"""Year-keyed tax tables (tax_tables/<year>.json): 2026 is byte-for-byte what the code held before; 2027 activates on 2027-01-01;
unconfirmed or unpublished figures carry forward and are flagged, never invented."""
import datetime, json, os, sys, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import taxtables, tax, aca, ss, estate, roth, benefits, grounding  # noqa: E402

GOLD = json.load(open(os.path.join(_HERE, "golden", "constants_2026.json"), encoding="utf-8"))
MODS = {"tax": tax, "aca": aca, "ss": ss, "estate": estate, "roth": roth, "benefits": benefits}
JAN27 = datetime.date(2027, 1, 15)


def norm(v):
    if isinstance(v, dict):
        return {str(k): norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    return v


class Switch27:
    """Run engine code as if it were January 2027, then put the live (2026) figures back."""
    def __enter__(self):
        taxtables.reload_all(JAN27)
        return self

    def __exit__(self, *a):
        taxtables.reload_all()


class TestTwentyTwentySixIsUnchanged(unittest.TestCase):
    def test_every_constant_equals_the_snapshot_taken_before_the_refactor(self):
        for mod, names in GOLD.items():
            for n, want in names.items():
                self.assertEqual(norm(getattr(MODS[mod], n)), want, f"{mod}.{n}")

    def test_engines_still_give_the_same_answers(self):
        self.assertEqual(tax.ordinary_tax(100000, "mfj"), round(0.10 * 24800 + 0.12 * (100000 - 24800), 2))
        self.assertEqual(tax.standard_deduction("mfj", age=66, spouse_age=64), 32200 + 1650)
        self.assertEqual(aca.fpl(4), 32150); self.assertEqual(aca.fpl(10), 54150 + 2 * 5500)


class TestTwentyTwentySevenActivates(unittest.TestCase):
    def test_primary_figures_switch_on(self):
        with Switch27():
            self.assertEqual(tax.TAX_YEAR, 2027)
            self.assertEqual(tax.ORDINARY_BRACKETS["mfj"][1], (0.12, 25600))
            self.assertEqual(tax.ordinary_tax(100000, "mfj"), round(0.10 * 25600 + 0.12 * (100000 - 25600), 2))
            self.assertEqual(tax.STANDARD_DEDUCTION, {"single": 16600, "mfj": 33200, "hoh": 24925})
            self.assertEqual(tax.standard_deduction("single", age=70), 16600 + 2100)
            self.assertEqual(tax.standard_deduction("mfj", age=66, spouse_age=66), 33200 + 2 * 1700)
            self.assertEqual(tax.LTCG_BRACKETS["mfj"], [(0.0, 0), (0.15, 102100), (0.2, 633600)])
            self.assertEqual(aca.COVERAGE_YEAR, 2027); self.assertEqual(aca.fpl(1), 15960); self.assertEqual(aca.fpl(9), 55720 + 5680)
            self.assertEqual(aca.APPLICABLE_PCT_2026[-1], (300.0, 400.0, 10.22, 10.22))
            self.assertEqual(estate.TAX_YEAR, 2027); self.assertEqual(estate.ESTATE_EXEMPTION, 15480000); self.assertEqual(estate.ANNUAL_EXCLUSION, 20000)
        self.assertEqual(tax.TAX_YEAR, 2026)                                          # restored

    def test_statutory_figures_do_not_move(self):
        with Switch27():
            self.assertEqual(tax.NIIT_THRESHOLD["mfj"], 250000)
            self.assertEqual(ss.TAX_THRESHOLDS["mfj"], (32000, 44000))
            self.assertEqual(estate.TOP_RATE, 0.40)

    def test_unconfirmed_social_security_figures_are_ignored_until_a_person_confirms(self):
        with Switch27():
            self.assertEqual(ss.BEND_POINTS_2026, (1286, 7749))                       # still 2026: the 2027 entry has confirmed=false
            self.assertEqual(ss.COLA_2026, 0.028); self.assertEqual(ss.OASDI_WAGE_BASE_2026, 184500)
            self.assertEqual(ss.EARNINGS_TEST_2026["under_fra_annual"], 24480)
        keys = {u["key"] for u in taxtables.unconfirmed(2027)}
        self.assertEqual(keys, {"ss.bend_points", "ss.cola_wagebase_max", "ss.earnings_test"})

    def test_unpublished_figures_carry_forward_and_are_flagged_stale(self):
        with Switch27():
            r = taxtables.resolve("roth.irmaa", JAN27)
            self.assertEqual((r["year"], r["stale"]), (2026, True)); self.assertEqual(roth.IRMAA_PART_B_STD_MONTHLY, 202.90)
            b = taxtables.resolve("benefits.contribution_limits", JAN27)
            self.assertTrue(b["partial"] and b["stale"] and "k401_employee" in b["carried_fields"] and b["carried_from"] == 2026)
            self.assertEqual(benefits.LIMITS_2026["hsa_self"], 4500); self.assertEqual(benefits.LIMITS_2026["hsa_family"], 9000)
            self.assertEqual(benefits.LIMITS_2026["k401_employee"], 24500)           # not published: carried, never invented
            self.assertEqual(benefits.LIMITS_2026["roth_ira_mfj_phaseout"], (242000.0, 252000.0))
            self.assertTrue(taxtables.resolve("aca.cliff_regime", JAN27)["stale"])    # a policy regime must be re-checked against legislation
            self.assertFalse(taxtables.resolve("tax.niit", JAN27)["stale"])          # statutory: carrying forward is correct
            self.assertFalse(taxtables.resolve("tax.ordinary_brackets", JAN27)["stale"])

    def test_the_watchdog_flags_exactly_the_carried_figures_in_january_2027(self):
        with Switch27():
            stale = {a["key"] for a in grounding.alerts(JAN27) if a["level"] == "stale"}
        self.assertEqual(stale, {"aca.cliff_regime", "ss.bend_points", "ss.cola_wagebase_max", "ss.earnings_test", "roth.irmaa",
                                 "benefits.contribution_limits", "hsa.healthcare_need", "ltc.cost_of_care"})   # Fidelity publishes its new estimate each year


class TestResolution(unittest.TestCase):
    def test_date_before_any_table_is_an_error_not_a_guess(self):
        with self.assertRaises(KeyError):
            taxtables.resolve("tax.ordinary_brackets", datetime.date(2020, 6, 1))

    def test_unknown_group_is_an_error(self):
        with self.assertRaises(KeyError):
            taxtables.resolve("tax.nonsense")

    def test_year_change_detection_and_reload(self):
        self.assertFalse(taxtables.year_changed())
        self.assertTrue(taxtables.year_changed(JAN27))
        taxtables.reload_all(JAN27)
        try:
            self.assertEqual(taxtables._loaded_year, 2027); self.assertFalse(taxtables.year_changed(JAN27))
        finally:
            taxtables.reload_all()
        self.assertEqual(tax.TAX_YEAR, 2026)


class TestFilesAreSound(unittest.TestCase):
    def test_every_group_is_a_known_registry_key_with_a_source_and_valid_status(self):
        keys = {s["key"] for s in grounding.PARAM_SPECS}
        for y in taxtables.years():
            groups = taxtables._file(y)["groups"]
            self.assertTrue(set(groups) <= keys, y)
            for k, e in groups.items():
                self.assertIn(e["status"], ("primary", "secondary", "statutory", "policy"), f"{y} {k}")
                self.assertTrue(e.get("source") and str(e.get("source_url", "")).startswith("https://"), f"{y} {k}")
        self.assertEqual(set(taxtables._file(2026)["groups"]), keys)                 # the base year is complete

    def test_unconfirmed_entries_say_what_to_do(self):
        for k, e in taxtables._file(2027)["groups"].items():
            if e.get("confirmed") is False:
                self.assertIn("confirm", e["note"].lower(), k)

    def test_2027_matches_the_researched_proposal_file(self):
        prop = {i["key"]: i for i in json.load(open(os.path.join(_HERE, "tax_updates", "2027.json"), encoding="utf-8"))["items"]}
        g = taxtables._file(2027)["groups"]
        self.assertEqual(g["tax.ordinary_brackets"]["values"], prop["tax.ordinary_brackets"]["values"])
        self.assertEqual(g["tax.standard_deduction"]["values"]["base"]["mfj"], prop["tax.standard_deduction"]["values"]["mfj"])
        self.assertEqual(g["estate.exemption"]["values"]["exemption"], prop["estate.exemption"]["values"]["exemption"])
        self.assertEqual(g["aca.fpl"]["values"]["fpl"]["1"], prop["aca.fpl"]["values"]["48_states"][0])
        self.assertEqual(g["benefits.contribution_limits"]["values"]["hsa_self"], prop["benefits.contribution_limits"]["values"]["hsa_self"])

    def test_the_loader_never_touches_the_network(self):
        src = open(os.path.join(_HERE, "taxtables.py"), encoding="utf-8").read()
        for bad in ("requests", "urllib", "http.client", "socket"):
            self.assertNotIn("import " + bad, src)


if __name__ == "__main__":
    unittest.main()
