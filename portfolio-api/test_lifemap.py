"""LIFE MAP: evaluator, scoring, variants, config validation, the default config's links, personas with expected top cards, endpoints."""
import asyncio
import copy
import datetime
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lifemap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "lifemap_default.json"), encoding="utf-8"))
HTML = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
TODAY = datetime.date(2026, 6, 15)               # outside every calendar window


def ctx(age, target, children=0, no_income=False, default_target=False):
    return {"age": age, "target_age": target, "children": children, "no_income": no_income, "years_to_stop": target - age,
            "stopped": age >= target or no_income, "target_age_is_default": default_target}


def sources(**kw):
    src = {"retirement": {"fi_number": 2_000_000, "fi_age": 55, "progress_pct": 40, "assumptions": {"current_age": 49, "target_age": 55}, "monte_carlo": {"success_pct": 85}, "bridge": {"status": "no_bridge_needed"}},
           "cashdebt": {"emergency_fund": {"status": "ok", "months_covered": 7, "target_months": 6, "gap_dollars": 0, "designated": True, "undesignated_cash": 0},
                      "avalanche": {"rows": [], "invest_hurdle_pct": 7.5}, "idle_cash": {"drag_annual": 0, "tbill_pct": 4.1}},
           "insurance": {"setup_needed": False, "household": {"children": 0}, "policies_echo": [{"n": 1}], "gap_table": [], "umbrella": {"recommended": 0, "in_force": 0}, "pnc": [], "alerts": [], "knobs": {"education_per_child": 100000}},
           "benefits": {"setup_needed": False, "sections": {"k401": {"match": {"full_capture": True, "left_on_table": 0, "needed_for_full_pct": 6}, "headroom": 1000}, "hsa": {"headroom": 500}, "roth_ira": {"remaining_total": 0, "pacing_self": {"status": "on_pace"}}}},
           "continuity": {"doc_currency": [{"doc_type": d, "status": "current"} for d in ("will", "durable_poa", "healthcare_directive", "beneficiary_review")],
                          "account_map": [{"passes_by": "beneficiary designation"}], "executor_name": "A", "will_location": "B", "contacts": ["C"]},
           "estate": {"snapshot": {"taxable_estate_if_today": 0, "years_until_exemption_crossed": None, "state_estate_tax": False}, "state_of_residence_source": "config"},
           "rebalance": {"concentration": [], "allocation": []},
           "ss": {"available": True, "household_monthly_benefit": 3000}, "roth": {"rmd_age": 75, "trad_balance": 0},
           "hsa": {"balance": 8000, "pct_of_need": 20, "need": {"total": 40000}}, "ltc": {"ok": True, "uncovered": 0, "total_cost": 0, "funding": {"uncovered_pct_of_investable": 0}},
           "grounding": {"status": "ok", "stale_count": 0}, "taxaudit": {"tax_loss_harvest": {"count": 0, "total_harvestable_loss": 0}, "asset_location": {"count": 0}},
           "fees": {"fees": {"funds": [], "total_annual_fee": 100}}, "registry": {"accounts": []},
           "timeline": {"summary": {"short_year_count": 0, "lost_subsidy_year_count": 0, "first_lost_subsidy_year": None, "medicare_surcharge_years": 0, "first_year_short": None}, "rows": []}}
    for k, v in kw.items():
        src[k] = v
    return src


class Evaluator(unittest.TestCase):
    def test_conditions(self):
        v = {"a": 5, "s": "Life (you)"}
        for cond, want in ((True, True), ([ "a", ">", 3], True), (["a", "<", 3], False), (["zz", "missing"], True), (["a", "present"], True), (["zz", ">", 1], False),
                           ({"all": [["a", ">", 1], ["a", "<", 9]]}, True), ({"any": [["a", ">", 9], ["s", "startswith", "Life"]]}, True), ({"not": ["a", "==", 5]}, False),
                           (["a", "in", [1, 5]], True), (None, False)):
            self.assertEqual(lifemap.eval_cond(cond, v), want, cond)

    def test_let_collectors(self):
        src = {"rows": [{"p": "x", "r": 10.0, "d": "will", "s": "missing"}, {"p": "x", "r": 20.0, "d": "poa", "s": "ok"}, {"p": "y", "r": 5.0, "d": "dir", "s": "missing"}], "a": {"b": 3}, "m": 9, "n": 4}
        out = lifemap.resolve_let({"c": {"count": "rows", "where": ["p", "==", "x"]}, "mx": {"max": "rows", "where": ["p", "==", "x"], "field": "r"},
                                   "sm": {"sum": "rows", "field": "r"}, "ls": {"list": "rows", "where": ["s", "==", "missing"], "field": "d"},
                                   "f": {"first": "rows", "where": ["p", "==", "y"], "field": "d"}, "ab": "a.b", "gone": "a.zzz", "diff": {"sub": ["m", "n"]}}, src)
        self.assertEqual((out["c"], out["mx"], out["sm"], out["ls"], out["f"], out["ab"], out["diff"]), (2, 20.0, 35.0, ["will", "dir"], "dir", 3, 5))
        self.assertNotIn("gone", out)

    def test_templates_never_print_a_blank(self):
        self.assertEqual(lifemap.render("You have {n|n0} items ({gap|usd}).", {"n": 3, "gap": 1234.4}), "You have 3 items ($1,234).")
        self.assertIsNone(lifemap.render("Short by {gap|usd}.", {}))
        self.assertEqual(lifemap.render("{xs|list}", {"xs": ["a", "b"]}), "a, b")

    def test_missing_figure_makes_the_check_not_enough_information(self):
        chk = {"id": "X", "label": "Thing", "source": "s", "let": {"gap": "g"}, "rules": [{"when": True, "state": "attention", "severity": "high", "reason": "Short by {gap|usd}."}]}
        r = lifemap.run_check(chk, {"s": {}})
        self.assertEqual(r["state"], "noinfo")
        self.assertIn("Not enough information", r["reason"])
        self.assertEqual(lifemap.run_check(chk, {"s": {"_error": "x"}})["state"], "error")
        self.assertEqual(lifemap.run_check(chk, {})["state"], "error")


class Config(unittest.TestCase):
    def test_default_config_is_valid(self):
        self.assertEqual(lifemap.validate(CFG), [])

    def test_validation_refuses_bad_weights_duplicates_and_advice_wording(self):
        bad = copy.deepcopy(CFG)
        bad["weights"]["safety"]["start"] = 7
        bad["topics"].append(copy.deepcopy(bad["topics"][0]))
        bad["topics"][1]["checks"][0]["rules"][0]["reason"] = "You should buy more insurance."
        errs = " | ".join(lifemap.validate(bad))
        self.assertIn("whole numbers 0 to 3", errs)
        self.assertIn("duplicate topic id", errs)
        self.assertIn("reads as advice", errs)
        self.assertTrue(lifemap.validate({}))

    def test_no_default_wording_reads_as_advice(self):
        text = json.dumps(CFG)
        self.assertFalse(lifemap.BANNED.search(text), lifemap.BANNED.search(text))

    def test_every_page_link_in_the_config_exists_on_the_page(self):
        top = set(re.findall(r"TAB==='([a-z]+)'", HTML))
        subs = set(re.findall(r"\{key:'([a-z]+)',label:", HTML))
        links = []
        for t in CFG["topics"]:
            links += [(p["tab"], p.get("subtab")) for p in t.get("pages", [])]
            for c in t.get("checks", []):
                links.append((c["from"]["tab"], c["from"].get("subtab")))
                for r in c["rules"]:
                    if r.get("next"):
                        links.append((r["next"]["tab"], r["next"].get("subtab")))
        for tab, sub in links:
            self.assertIn(tab, top | {"plan", "calculators", "settings", "insights", "audits", "research", "strategist"}, tab)
            if sub:
                self.assertIn(sub, subs, f"{tab}/{sub}")

    def test_weights_cover_every_stage_for_every_status_topic(self):
        sids = [s["id"] for s in CFG["stages"]]
        for t in CFG["topics"]:
            if t["kind"] == "status":
                self.assertEqual(sorted(k for k in CFG["weights"][t["id"]] if k != "why"), sorted(sids), t["id"])


class Personas(unittest.TestCase):
    """Expected top cards for people at different stages (invented figures). If a decision changes the answer, this is where it shows."""

    def run_map(self, c, src, pins=None, variant=None, stage=None):
        return lifemap.build(CFG, src, c, pins=pins, today=TODAY, variant=variant, stage_override=stage)

    def test_jason_49_fire_with_insurance_and_beneficiary_gaps(self):
        src = sources(insurance={"setup_needed": False, "household": {"children": 0}, "policies_echo": [],
                                 "gap_table": [{"line": "Life (you)", "need": 600000, "in_force": 0, "gap": 600000, "status": "gap"}]},
                      continuity={"doc_currency": [{"doc_type": "will", "status": "current"}, {"doc_type": "durable_poa", "status": "missing"},
                                                   {"doc_type": "healthcare_directive", "status": "missing"}, {"doc_type": "beneficiary_review", "status": "missing"}],
                                  "account_map": [{"passes_by": "beneficiary designation (401k)"}] * 4, "executor_name": None},
                      retirement={"fi_number": 2_000_000, "fi_age": 61, "progress_pct": 38, "assumptions": {"current_age": 49, "target_age": 55}, "monte_carlo": {"success_pct": 72}})
        r = self.run_map(ctx(49, 55), src)
        self.assertEqual(r["stage"]["id"], "peak")
        self.assertEqual(set(r["top"]), {"handover", "protect", "independence"})

    def test_28_without_an_emergency_fund_sees_safety_first(self):
        src = sources(cashdebt={"emergency_fund": {"status": "critical", "months_covered": 0.4, "target_months": 6, "gap_dollars": 14000},
                                "avalanche": {"rows": [{"priority": "pay-first", "rate_pct": 24.0, "annual_interest": 900}], "invest_hurdle_pct": 7.5}},
                      retirement={"fi_number": 1_500_000, "fi_age": 70, "progress_pct": 2, "assumptions": {"current_age": 28, "target_age": 65}, "monte_carlo": {"success_pct": 90}})
        r = self.run_map(ctx(28, 65), src)
        self.assertEqual(r["stage"]["id"], "start")
        self.assertEqual(r["top"][0], "safety")

    def test_retired_people_are_in_early_then_later_retirement_and_education_never_shows(self):
        src = sources(retirement={"fi_number": 1_000_000, "fi_age": 60, "progress_pct": 100, "assumptions": {"current_age": 70, "target_age": 62}, "monte_carlo": {"success_pct": 88}})
        self.assertEqual(self.run_map(ctx(70, 62), src)["stage"]["id"], "early")             # under 73
        r = self.run_map(ctx(75, 62), src)
        self.assertEqual(r["stage"]["id"], "later")
        self.assertNotIn("education", r["bands"]["now"])
        self.assertEqual({x["id"]: x for x in r["topics"]}["education"]["parts"]["relevance"], 0)

    def test_children_lift_insurance_and_handover(self):
        src = sources(insurance={"setup_needed": False, "household": {"children": 2}, "policies_echo": [{"x": 1}],
                                 "gap_table": [{"line": "Life (you)", "need": 800000, "in_force": 100000, "gap": 700000, "status": "gap"}]})
        r = self.run_map(ctx(36, 62, children=2), src)
        t = {x["id"]: x for x in r["topics"]}
        self.assertEqual(t["protect"]["parts"]["relevance"], 3)
        self.assertIn({"id": "children", "add": 1}, t["handover"]["parts"]["modifiers"])
        self.assertEqual(t["protect"]["checks"][1]["severity"], "high")

    def test_a_failing_check_can_be_missing_data_without_breaking_the_map(self):
        r = self.run_map(ctx(49, 55), {"retirement": sources()["retirement"]})
        self.assertTrue(all(t["status"] in ("error", "unchecked", "ok", "noinfo", "attention", "todo") for t in r["topics"] if t["kind"] == "status"))

    def test_checks_that_cannot_run_say_could_not_check_and_are_never_called_fine(self):
        r = self.run_map(ctx(49, 55), {"retirement": sources()["retirement"]})
        t = {x["id"]: x for x in r["topics"]}
        for k in ("taxes", "health", "income", "education", "safety"):
            self.assertIn(t[k]["status"], ("error", "noinfo"), k)
            self.assertNotEqual(t[k]["status"], "ok", k)

    def test_a_topic_with_no_checks_at_all_is_listed_but_never_ranked(self):
        cfg = copy.deepcopy(CFG)
        for tp in cfg["topics"]:
            if tp["id"] == "taxes":
                tp["checks"] = []
        r = lifemap.build(cfg, sources(), ctx(49, 55), today=TODAY)
        t = {x["id"]: x for x in r["topics"]}["taxes"]
        self.assertEqual((t["status"], t["score"], t["band"]), ("unchecked", 0.0, "later"))


class NewChecks(unittest.TestCase):
    """The second batch of checks (the design's remaining probes), each against a source shaped like the endpoint."""

    def check(self, topic, cid, c=None, **kw):
        src = sources(**kw)
        r = lifemap.build(CFG, src, c or ctx(49, 55), today=TODAY)
        return {x["id"]: x for x in {t["id"]: t for t in r["topics"]}[topic]["checks"]}[cid]

    def test_emergency_fund_designation_and_idle_cash(self):
        cd = sources()["cashdebt"]
        self.assertEqual(self.check("safety", "S2", cashdebt={**cd, "emergency_fund": {**cd["emergency_fund"], "designated": False}})["state"], "todo")
        self.assertEqual(self.check("safety", "S5", cashdebt={**cd, "idle_cash": {"drag_annual": 900, "tbill_pct": 4.1}})["state"], "attention")

    def test_umbrella_state_estate_and_tax_placement(self):
        ins = sources()["insurance"]
        r = self.check("protect", "P4", insurance={**ins, "umbrella": {"recommended": 2_000_000, "in_force": 0}})
        self.assertEqual(r["state"], "attention")
        self.assertIn("$2,000,000", r["reason"])
        self.assertEqual(self.check("handover", "H9", estate={**sources()["estate"], "state_of_residence_source": "default_unconfirmed"})["state"], "noinfo")
        self.assertEqual(self.check("grow", "G6", taxaudit={**sources()["taxaudit"], "asset_location": {"count": 3}})["state"], "attention")

    def test_timeline_checks_use_the_summary_counts(self):
        tl = {"summary": {"short_year_count": 4, "first_year_short": 2043, "lost_subsidy_year_count": 4, "first_lost_subsidy_year": 2038, "medicare_surcharge_years": 2}, "rows": []}
        i5 = self.check("independence", "I5", timeline=tl)
        self.assertEqual((i5["state"], i5["severity"]), ("attention", "high"))
        self.assertIn("2043", i5["reason"])
        self.assertIn("2038", self.check("health", "C7", timeline=tl)["reason"])
        self.assertEqual(self.check("health", "C6", timeline=tl)["state"], "attention")
        self.assertEqual(self.check("independence", "I5")["state"], "ok")

    def test_checks_that_use_the_household_context_and_stage(self):
        roth = {"rmd_age": 75, "trad_balance": 500000}
        early = self.check("taxes", "T4", c=ctx(66, 60), roth=roth)             # stopped, 9 years before required withdrawals
        self.assertEqual((early["state"], early["severity"]), ("attention", "low"))
        near = self.check("taxes", "T4", c=ctx(71, 60), roth=roth)              # 4 years left
        self.assertEqual((near["state"], near["severity"]), ("attention", "normal"))
        self.assertEqual(self.check("taxes", "T4", c=ctx(40, 65), roth=roth)["state"], "ok")                     # still working: a fact
        self.assertEqual(self.check("taxes", "T4", roth={"rmd_age": 75, "trad_balance": 0})["state"], "na")
        ret = sources()["retirement"]
        aca_over = {**ret, "bridge": {"status": "bridge_needed", "aca": {"flags": {"over_cliff": True}, "subsidy": {"fpl_pct": 450}}}}
        c1 = self.check("health", "C1", c=ctx(56, 55), retirement=aca_over)
        self.assertEqual((c1["state"], c1["severity"]), ("attention", "high"))   # stage 'early'
        self.assertEqual(self.check("health", "C1", c=ctx(66, 55), retirement=aca_over)["state"], "na")        # 65 or older: Medicare

    def test_social_security_uses_two_sources_and_the_stage(self):
        n1 = self.check("income", "N1", c=ctx(58, 60), ss={"available": False})
        self.assertEqual((n1["state"], n1["severity"]), ("noinfo", "high"))                              # final approach
        self.assertEqual(self.check("income", "N1", c=ctx(35, 65), ss={"available": False})["severity"], "normal")
        ret = {**sources()["retirement"], "assumptions": {"current_age": 49, "target_age": 55, "ss_claim_age": 67}}
        self.assertIn("$3,000 a month from age 67", self.check("income", "N2", retirement=ret)["reason"])

    def test_student_loans_are_a_name_guess_and_say_so(self):
        reg = {"accounts": [{"group": "liability", "name": "Nelnet - xx5352", "balance": 15752.0, "tax_status": "na"},
                            {"group": "liability", "name": "Visa", "balance": 900.0, "tax_status": "na"},
                            {"group": "taxable", "name": "529 Plan", "balance": 3000.0, "tax_status": "education"}]}
        e3 = self.check("education", "E3", registry=reg)
        self.assertIn("look like student loans ($15,752), going by their names", e3["reason"])
        self.assertIn("$3,000", self.check("education", "E2", registry=reg)["reason"])

    def test_education_topic_stays_not_enough_information_without_the_profile(self):
        self.assertEqual(self.check("education", "E5")["state"], "noinfo")


class RankingRules(unittest.TestCase):
    def test_pin_lifts_but_does_not_force_and_a_critical_gap_still_wins(self):
        src = sources(cashdebt={"emergency_fund": {"status": "critical", "months_covered": 0.2, "target_months": 6, "gap_dollars": 20000}, "avalanche": {"rows": [], "invest_hurdle_pct": 7.5}})
        r = lifemap.build(CFG, src, ctx(49, 55), pins=["independence"], today=TODAY)
        self.assertEqual(r["order"][0], "safety")
        t = {x["id"]: x for x in r["topics"]}
        self.assertEqual(t["independence"]["parts"]["relevance"], 3)

    def test_timing_window_boosts_only_the_checks_it_names(self):
        src = sources(benefits={"setup_needed": False, "sections": {"k401": {"match": {"full_capture": False, "left_on_table": 4000, "needed_for_full_pct": 6}}}})
        inwin = lifemap.build(CFG, src, ctx(35, 65), today=datetime.date(2026, 11, 20))
        out = lifemap.build(CFG, src, ctx(35, 65), today=TODAY)
        a = {x["id"]: x for x in inwin["topics"]}["grow"]["parts"]["timing"]
        b = {x["id"]: x for x in out["topics"]}["grow"]["parts"]["timing"]
        self.assertEqual((a, b), (1.3, 1.0))

    def test_look_ahead_changes_the_stage_not_the_checks(self):
        r = lifemap.build(CFG, sources(), ctx(35, 65), today=TODAY, stage_override="approach")
        self.assertEqual(r["stage"]["id"], "approach")


class Variants(unittest.TestCase):
    """Decision points can be compared: each variant is a named partial override of the baseline."""

    def test_variants_exist_and_change_the_stage_or_the_order(self):
        src = sources()
        res = lifemap.compare(CFG, src, ctx(49, 55), ["default", "age_stages", "pins_force_top", "no_timing", "equal_weights"], pins=["taxes"], today=TODAY)
        by = {r["variant"]: r for r in res}
        self.assertEqual(by["default"]["stage"], "peak")
        self.assertEqual(by["age_stages"]["stage"], "peak")                 # age 49 is in the 40-50 band of the age-only stage rule
        self.assertIn("variants", CFG)
        self.assertEqual(len(res), 5)

    def test_age_only_stages_disagree_with_years_to_stop_for_an_early_retiree(self):
        c = ctx(45, 50)                                     # five years from stopping, but only 45
        self.assertEqual(lifemap.build(CFG, sources(), c, today=TODAY)["stage"]["id"], "approach")
        self.assertEqual(lifemap.build(CFG, sources(), c, today=TODAY, variant="age_stages")["stage"]["id"], "peak")

    def test_pins_force_top_variant_overrides_the_ordering(self):
        src = sources(cashdebt={"emergency_fund": {"status": "critical", "months_covered": 0.2, "target_months": 6, "gap_dollars": 9000}, "avalanche": {"rows": [], "invest_hurdle_pct": 7.5}})
        base = lifemap.build(CFG, src, ctx(49, 55), pins=["taxes"], today=TODAY)
        forced = lifemap.build(CFG, src, ctx(49, 55), pins=["independence"], today=TODAY, variant="pins_force_top")
        self.assertEqual(base["order"][0], "safety")
        self.assertEqual(forced["order"][0], "independence")

    def test_unknown_variant_falls_back_to_the_default(self):
        self.assertEqual(lifemap.build(CFG, sources(), ctx(49, 55), today=TODAY, variant="nope")["order"],
                         lifemap.build(CFG, sources(), ctx(49, 55), today=TODAY)["order"])


class Api(unittest.TestCase):
    def setUp(self):
        import app as appmod
        self.app = appmod
        self.p = [mock.patch.object(appmod, "_lifemap_sources", lambda request=None: sources()),
                  mock.patch.object(appmod, "_read_retire", lambda: {"target_age": 55})]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_get_lifemap_returns_the_stage_topics_and_variants(self):
        with mock.patch.object(self.app, "_lifemap_cfg", lambda: CFG):
            r = self.app.get_lifemap(None)
        self.assertEqual(r["stage"]["id"], "peak")
        self.assertIn("age_stages", r["variants"])
        self.assertEqual(len(r["topics"]), 11)
        self.assertIn("Informational only", r["note"])

    def test_pins_are_validated_and_compare_lists_variants(self):
        with mock.patch.object(self.app, "_lifemap_cfg", lambda: CFG):
            r = self.app.get_lifemap(None, pins="independence,notatopic")
            self.assertTrue({t["id"]: t for t in r["topics"]}["independence"]["pinned"])
            c = self.app.get_lifemap_compare("default,age_stages,bogus")
        self.assertEqual([v["variant"] for v in c["variants"]], ["default", "age_stages"])
        self.assertIn("five stages by age", c["why"]["age_stages"])

    def test_saving_a_config_validates_and_reset_restores_the_default(self):
        written = {}

        class Req:
            headers = {}
            client = None

            def __init__(self, b):
                self.b = b

            async def json(self):
                return self.b
        with mock.patch.object(self.app.store, "write_json", lambda p, d: written.update(d=d)), mock.patch.object(self.app, "_emit_config_changed", lambda *a, **k: None), \
                mock.patch.object(self.app, "_lifemap_cfg", lambda: CFG):
            bad = copy.deepcopy(CFG)
            bad["weights"]["safety"]["start"] = 9
            r = asyncio.run(self.app.post_lifemap_config(Req({"config": bad})))
            self.assertEqual(r.status_code, 400)
            self.assertNotIn("d", written)
            ok = asyncio.run(self.app.post_lifemap_config(Req({"config": CFG})))
            self.assertTrue(ok["ok"])
            asyncio.run(self.app.post_lifemap_reset(Req({})))
            self.assertEqual(written["d"], {})

    def test_viewers_may_read_the_map_and_pin_but_writes_stay_admin_only(self):
        self.assertNotIn("/api/lifemap/config", self.app._VIEWER_ALLOWED_WRITE_PATHS)
        self.assertNotIn("/api/lifemap/reset", self.app._VIEWER_ALLOWED_WRITE_PATHS)
        import prefs
        self.assertIn("fidLifePins", prefs.SYNC_KEYS)
        self.assertIn("'fidLifePins'", HTML.split("const PREFS_KEYS=")[1].split("];")[0])


class Page(unittest.TestCase):
    def test_tab_start_page_cards_and_topic_page_exist(self):
        for needle in ("['lifemap','LIFE MAP']", "function lifemapContent()", "function lifeTopicContent(id)", "TAB==='lifemap'", "Why this order?",
                       "Look ahead: my stage", "MAKE THIS MY START PAGE", "fidStartPage", "NEEDS ATTENTION NOW", "PAGES FOR THIS TOPIC"):
            self.assertIn(needle, HTML, needle)
        self.assertIn("TAB!=='lifemap'", HTML)                      # the cockpit strip is skipped on the map


if __name__ == "__main__":
    unittest.main()


class SettingsPage(unittest.TestCase):
    def test_the_editor_page_is_an_admin_only_settings_subtab_using_the_config_endpoints(self):
        for needle in ("_isAdminUser() && {key:'lifemapcfg',label:'LIFE MAP'", "function lifemapSettingsContent()", "fetch('/api/lifemap/config')",
                       "lmsPost('/api/lifemap/reset'", "SAVE FROM TEXT", "RESET TO DEFAULTS", "STAGE WEIGHTS"):
            self.assertIn(needle, HTML, needle)
