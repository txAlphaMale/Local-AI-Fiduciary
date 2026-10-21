# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_custom_themes.py -- custom theme editor (tester request 2026-10-21): colour maths, name/value sanitizing, derived overlays.
Runs the real helper functions from index.html under node."""
import json, os, shutil, subprocess, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def _fn(name):
    i = HTML.index(f"function {name}(")
    depth, k = 0, HTML.index("{", i)
    while True:
        depth += HTML[k] == "{"
        depth -= HTML[k] == "}"
        k += 1
        if depth == 0:
            return HTML[i:k]


def _const(name):
    i = HTML.index(f"const {name}=")
    j = HTML.index(";\n", i) + 2
    return HTML[i:j]


PRELUDE = (_const("THEME_VARS") + _const("THEME_OVERLAY_VARS") + _const("hexOk")
           + "const THEME_LIST=[{key:'amber',label:'AMBER TERMINAL'},{key:'cobalt',label:'COBALT'}];\n"
           + "".join(_fn(n) for n in ("themeNameOk", "hexToRgb", "rgbToHex", "rgbToHsl", "hslToRgb", "relLum", "contrastRatio",
                                       "themeOverlays", "loadCustomThemes", "saveCustomThemes")))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestThemeHelpers(unittest.TestCase):
    def js(self, body):
        r = subprocess.run(["node", "-e", PRELUDE + body], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def js2(self, body):
        r = subprocess.run(["node", "-e", PRELUDE + _fn("wheelHitToHs") + _fn("wheelHsToXY") + body], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_colour_conversions_round_trip_and_known_values(self):
        out = self.js("""
        let bad=0; for(let i=0;i<4000;i++){ const r=Math.floor(Math.random()*256),g=Math.floor(Math.random()*256),b=Math.floor(Math.random()*256);
          const [h,s,l]=rgbToHsl(r,g,b); const [r2,g2,b2]=hslToRgb(h,s,l); if(Math.abs(r-r2)>1||Math.abs(g-g2)>1||Math.abs(b-b2)>1) bad++; }
        console.log(JSON.stringify([bad, rgbToHex(240,168,35), hexToRgb('#f0a823'), rgbToHsl(255,0,0), hslToRgb(120,100,50), rgbToHex(300,-5,'x')]));""")
        self.assertEqual(out[0], 0)
        self.assertEqual(out[1:], ["#f0a823", [240, 168, 35], [0, 100, 50], [0, 255, 0], "#ff0000"])

    def test_contrast_ratio_matches_wcag(self):
        out = self.js("console.log(JSON.stringify([contrastRatio('#000000','#ffffff'), contrastRatio('#777777','#777777')]))")
        self.assertAlmostEqual(out[0], 21.0, places=1)
        self.assertEqual(out[1], 1)

    def test_names_must_be_safe_and_not_shadow_built_ins(self):
        out = self.js("console.log(JSON.stringify(['My Theme','a','x'.repeat(30),'x'.repeat(31),'','<b>','a\"b','Cobalt','amber terminal','-lead','ok_name-2'].map(themeNameOk)))")
        self.assertEqual(out, [True, True, True, False, False, False, False, False, False, False, True])

    def test_overlays_follow_the_palette_and_flip_for_light_backgrounds(self):
        out = self.js("""
        const v={'--bg':'#000000','--amber':'#ff0000','--grn':'#00ff00','--red':'#0000ff','--blu':'#ffffff'};
        const dark=themeOverlays(v), light=themeOverlays(Object.assign({},v,{'--bg':'#ffffff'}));
        console.log(JSON.stringify([dark['--overlay-accent-weak'],dark['--overlay-positive-weak'],dark['--overlay-white-weak'],light['--overlay-white-weak']]));""")
        self.assertEqual(out, ["rgba(255,0,0,0.13)", "rgba(0,255,0,0.12)", "rgba(255,255,255,0.02)", "rgba(0,0,0,0.03)"])

    def test_stored_themes_are_sanitized_on_load(self):
        out = self.js("""
        const good={}; THEME_VARS.forEach(t=>good[t.v]='#112233');
        const evil=Object.assign({},good,{'--bg':'red;background:url(//evil)'});
        const store={fidCustomThemes:JSON.stringify({'Good One':{vars:good},'<script>':{vars:good},'Evil':{vars:evil},'Cobalt':{vars:good},'Short':{vars:{'--bg':'#000000'}}})};
        global.localStorage={getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=v;}};
        console.log(JSON.stringify([Object.keys(loadCustomThemes()), saveCustomThemes(loadCustomThemes()), Object.keys(JSON.parse(store.fidCustomThemes))]));""")
        self.assertEqual(out, [["Good One"], True, ["Good One"]])

    def test_the_twelve_editable_variables_are_the_tier1_primitives_every_theme_overrides(self):
        import re
        root = HTML[HTML.index(":root{"):HTML.index("--color-bg:")]
        defined = set(re.findall(r"(--[a-z0-9]+):\s*#", root))
        editable = set(re.findall(r"\{v:'(--[a-z0-9]+)'", HTML.split("const THEME_OVERLAY_VARS")[0]))
        self.assertEqual(editable, defined & editable)
        self.assertEqual(len(editable), 12)
        block = HTML[HTML.index(':root[data-theme="cobalt"]'):HTML.index(':root[data-theme="highcontrast"]')]
        self.assertEqual(editable, set(re.findall(r"(--[a-z0-9]+):#", block)))   # a built-in theme sets exactly these

    def test_wheel_geometry_round_trips_and_clamps_to_the_rim(self):
        out = self.js2("""
        let bad=0; for(let h=0;h<360;h+=7) for(let s=5;s<=100;s+=19){ const p=wheelHsToXY(h,s,100), q=wheelHitToHs(p.x,p.y,100);
          if(Math.abs(q.s-s)>0.01 || Math.abs(((q.h-h+540)%360)-180)>0.01) bad++; }
        const far=wheelHitToHs(500,0,100), left=wheelHitToHs(-50,0,100), down=wheelHitToHs(0,50,100);
        console.log(JSON.stringify([bad, far.s, far.h, Math.round(left.h), Math.round(left.s), Math.round(down.h)]));""")
        self.assertEqual(out, [0, 100, 0, 180, 50, 90])

    def test_ui_wiring(self):
        for needle in ("themeEditor()", "openColorDialog(", "CUSTOMIZE…", "NEW CUSTOM THEME FROM THIS", "SAVE THEME", "IMPORT…", "EXPORT", "Readability",
                       "THEME_EDIT=null; applyTheme(t.key)", "clearInlineTheme();\n  let custom=null;"):
            self.assertIn(needle, HTML)
        self.assertEqual(len(__import__("re").findall(r"\{v:'--[a-z0-9]+'", HTML.split("const THEME_OVERLAY_VARS")[0].split("const THEME_VARS")[1])), 12)


if __name__ == "__main__":
    unittest.main()
