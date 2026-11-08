#!/usr/bin/env python3
"""Render the public "Security pipeline" page from the scanners' own output.

Stdlib only, no network. Inputs are files produced by the security-report workflow:
  Trivy JSON (all severities-of-interest, and --ignore-unfixed), pip-audit JSON, Checkov JSON,
  a list of recent workflow runs, the required-check list, .trivyignore, .checkov.yaml and
  security/remediation-log.json.

Public-safety rule: the page shows COUNTS for open findings, never a per-package list of
unfixed vulnerabilities. Fixed items and documented exceptions are listed by name.
"""
import argparse
import collections
import datetime
import html
import json
import os
import re
import sys


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def trivy_counts(doc):
    """{'CRITICAL': n, 'HIGH': n} of unique (CVE, package) pairs."""
    seen = {}
    for res in (doc or {}).get("Results") or []:
        for v in res.get("Vulnerabilities") or []:
            seen[(v.get("VulnerabilityID"), v.get("PkgName"))] = v.get("Severity")
    c = collections.Counter(seen.values())
    return {"CRITICAL": c.get("CRITICAL", 0), "HIGH": c.get("HIGH", 0)}


def pip_audit_count(doc):
    n = 0
    deps = doc.get("dependencies") if isinstance(doc, dict) else doc
    for d in deps or []:
        n += len(d.get("vulns") or [])
    return n


def checkov_summary(doc):
    docs = doc if isinstance(doc, list) else ([doc] if doc else [])
    out = {}
    for d in docs:
        s = d.get("summary") or {}
        out[d.get("check_type", "?")] = {k: s.get(k, 0) for k in ("passed", "failed", "skipped")}
    return out


def parse_trivyignore(text):
    """Returns [(cve, expiry, reason)] using the comment block directly above each entry."""
    rows, buf = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            buf.append(s.lstrip("# ").strip())
        elif s:
            m = re.match(r"(CVE-\d+-\d+)(?:\s+exp:(\d{4}-\d{2}-\d{2}))?", s)
            if m:
                reason = " ".join(buf[:2]) if buf else ""
                rows.append((m.group(1), m.group(2) or "none", reason))
            buf = []
        else:
            buf = []
    return rows


def parse_checkov_skips(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"\s*-\s+(CKV2?_[A-Z]+_\d+)\s+#\s*(.*)", line)
        if m:
            rows.append((m.group(1), m.group(2).strip()))
    return rows


def latest_by_workflow(runs):
    best = {}
    for r in runs or []:
        name = r.get("name")
        if name and (name not in best or r.get("created_at", "") > best[name].get("created_at", "")):
            best[name] = r
    return best


def esc(x):
    return html.escape(str(x), quote=True)


def badge(result):
    cls = {"success": "ok", "failure": "bad"}.get(result, "warn")
    label = {"success": "passing", "failure": "failing"}.get(result, result or "unknown")
    return f'<span class="b {cls}">{esc(label)}</span>'


def build(args):
    all_c = trivy_counts(load_json(args.trivy_all, {}))
    fix_c = trivy_counts(load_json(args.trivy_fixable, {}))
    unfixed = {k: max(all_c[k] - fix_c[k], 0) for k in all_c}
    pip_n = pip_audit_count(load_json(args.pip_audit, {}))
    ck = checkov_summary(load_json(args.checkov, []))
    runs = latest_by_workflow(load_json(args.runs, []))
    required = load_json(args.required, [])
    ti = parse_trivyignore(read_text(args.trivyignore))
    cs = parse_checkov_skips(read_text(args.checkov_yaml))
    log = (load_json(args.remediation, {}).get("entries") or [])
    log = sorted(log, key=lambda e: e.get("fixed", ""), reverse=True)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    summary = {
        "generated": now, "commit": args.commit,
        "trivy_image": {"fixable": fix_c, "unfixed": unfixed},
        "pip_audit_vulnerabilities": pip_n, "checkov": ck,
        "required_checks": required,
        "trivy_exceptions": [{"cve": c, "expires": e} for c, e, _ in ti],
        "checkov_skips": len(cs), "remediated": len(log),
    }

    run_rows = "".join(
        f"<tr><td>{esc(n)}</td><td>{badge(r.get('conclusion'))}</td><td>{esc((r.get('created_at') or '')[:10])}</td>"
        f"<td>{esc(r.get('event',''))}</td></tr>" for n, r in sorted(latest_by_workflow(
            [x for x in (load_json(args.runs, [])) if x.get("name") in args.show_workflows or not args.show_workflows]).items()))
    ck_rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{v['passed']}</td><td>{v['failed']}</td><td>{v['skipped']}</td></tr>"
        for k, v in sorted(ck.items()))
    ti_rows = "".join(f"<tr><td>{esc(c)}</td><td>{esc(e)}</td><td>{esc(r[:230])}</td></tr>" for c, e, r in ti)
    cs_rows = "".join(f"<tr><td>{esc(c)}</td><td>{esc(r[:230])}</td></tr>" for c, r in cs)
    log_rows = "".join(
        f"<tr><td>{esc(e.get('found',''))}</td><td>{esc(e.get('fixed',''))}</td><td>{esc(e.get('severity',''))}</td>"
        f"<td>{esc(e.get('summary',''))}</td><td>{esc(e.get('ref',''))}</td></tr>" for e in log)
    req = "".join(f"<li>{esc(c)}</li>" for c in required) or "<li>unavailable at build time</li>"

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Security pipeline</title>
<style>
:root{{--bg:#fff;--fg:#1b1f24;--mut:#59636e;--card:#f6f8fa;--line:#d1d9e0;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#9198a1;--card:#151b23;--line:#3d444d;--ok:#3fb950;--bad:#f85149;--warn:#d29922}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:980px;margin:0 auto;padding:24px 16px 56px}}
h1{{margin:.2em 0}}h2{{margin-top:2em;border-bottom:1px solid var(--line);padding-bottom:.2em}}
p.s{{color:var(--mut);margin:.2em 0 1em}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}
.t{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}}
.t b{{display:block;font-size:26px}}.t span{{color:var(--mut);font-size:13px}}
table{{width:100%;border-collapse:collapse;font-size:14px;display:block;overflow-x:auto}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--mut);font-weight:600}}
.b{{padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid currentColor}}
.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}.warn{{color:var(--warn)}}
code{{background:var(--card);padding:1px 5px;border-radius:4px}}
</style></head><body><main>
<h1>Security pipeline</h1>
<p class="s">Generated {esc(now)} from commit <code>{esc(args.commit[:8])}</code>. Numbers come from the scanners' own output on the latest build of <code>main</code>; nothing here is hand-edited except the remediation log.</p>

<h2>Where things stand</h2>
<div class="tiles">
<div class="t"><b>{fix_c['CRITICAL'] + fix_c['HIGH']}</b><span>container CRITICAL/HIGH with a vendor fix (the gate fails on these)</span></div>
<div class="t"><b>{unfixed['CRITICAL'] + unfixed['HIGH']}</b><span>container CRITICAL/HIGH with no vendor fix yet (tracked, not gating); excludes {len(ti)} documented exception(s) listed below, which Trivy hides via .trivyignore</span></div>
<div class="t"><b>{pip_n}</b><span>known vulnerabilities in pinned Python dependencies</span></div>
<div class="t"><b>{sum(v['failed'] for v in ck.values())}</b><span>Checkov failures ({len(cs)} documented exceptions)</span></div>
</div>

<h2>Latest runs on main</h2>
<table><tr><th>Workflow</th><th>Result</th><th>Date</th><th>Trigger</th></tr>{run_rows}</table>

<h2>What blocks a change from reaching main</h2>
<p class="s">A pull request is required, with these checks passing. Force-push and branch deletion are blocked. A failed scan on main or on the schedule emails the owner through GitHub Actions notifications.</p>
<ul>{req}</ul>

<h2>Policy</h2>
<table><tr><th>Severity</th><th>Patch within</th><th>Enforcement</th></tr>
<tr><td>CRITICAL</td><td>7 days of disclosure</td><td>Fails the scan when a vendor fix exists</td></tr>
<tr><td>HIGH</td><td>30 days</td><td>Fails the scan when a vendor fix exists</td></tr>
<tr><td>MEDIUM</td><td>90 days</td><td>Reported</td></tr>
<tr><td>LOW</td><td>Best effort</td><td>Reported</td></tr></table>
<p class="s">Security updates are surfaced to the owner for approval, not left to troubleshooting. Exceptions buy time to fix; they are renewed only while the upstream authority still has no fix.</p>

<h2>Remediation log</h2>
<table><tr><th>Found</th><th>Fixed</th><th>Severity</th><th>What</th><th>Reference</th></tr>{log_rows}</table>

<h2>Documented exceptions</h2>
<h3>Container (Trivy) - expire and must be re-justified</h3>
<table><tr><th>CVE</th><th>Expires</th><th>Reason</th></tr>{ti_rows}</table>
<h3>Infrastructure and workflows (Checkov)</h3>
<table><tr><th>Check</th><th>Reason</th></tr>{cs_rows}</table>
<h3>Checkov results (with exceptions applied)</h3>
<table><tr><th>Framework</th><th>Passed</th><th>Failed</th><th>Skipped</th></tr>{ck_rows}</table>

<p class="s" style="margin-top:2em">Scanners: pip-audit, Trivy (image), Checkov (Dockerfile, Terraform, GitHub Actions), CodeQL (Python, JavaScript, Actions), Dependabot, GitHub secret scanning with push protection. This page reports aggregate counts only; the full results live in the repository's Actions and Security tabs.</p>
</main></body></html>"""
    return page, summary


def main(argv=None):
    p = argparse.ArgumentParser()
    for a in ("trivy-all", "trivy-fixable", "pip-audit", "checkov", "runs", "required",
              "trivyignore", "checkov-yaml", "remediation"):
        p.add_argument("--" + a, default="")
    p.add_argument("--commit", default="unknown")
    p.add_argument("--out-dir", default="site")
    p.add_argument("--show-workflows", nargs="*", default=[])
    args = p.parse_args(argv)
    for k in ("trivy_all", "trivy_fixable", "pip_audit", "checkov", "runs", "required"):
        if not getattr(args, k):
            setattr(args, k, os.devnull)
    page, summary = build(args)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(args.out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary["trivy_image"]), "pip:", summary["pip_audit_vulnerabilities"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
