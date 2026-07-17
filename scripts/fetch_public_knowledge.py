#!/usr/bin/env python3
"""
fetch_public_knowledge.py — preload the knowledge/ RAG folder with PUBLIC reference docs.

Downloads the public-domain starter set (IRS publications + the SEC release) straight from
the primary sources into knowledge/, so a fresh clone gets the same library without the repo
carrying ~11 MB of PDFs. Copyrighted-but-public docs (Vanguard's investing principles, an
asset-class periodic table) are LINKED, not redistributed — grab them manually if wanted.

    python3 scripts/fetch_public_knowledge.py            # into ./knowledge
    KNOWLEDGE_DIR=/path python3 scripts/fetch_public_knowledge.py

Then reindex: dashboard ⚙ SETTINGS → KNOWLEDGE → REINDEX (or POST /api/knowledge/reindex).
Safe to re-run: existing files are skipped unless --force. Only stdlib (urllib), no deps.
"""
import os, sys, urllib.request

DEST = os.environ.get("KNOWLEDGE_DIR") or os.path.join(os.path.dirname(__file__), "..", "knowledge")
FORCE = "--force" in sys.argv
UA = {"User-Agent": "Fiduciary-knowledge-fetch (personal finance tool; contact: repo owner)"}

# Public-domain primary sources (US-government works). Filenames match the legacy library.
PUBLIC = [
    ("p590a.pdf",    "https://www.irs.gov/pub/irs-pdf/p590a.pdf",
     "IRS Pub 590-A — Contributions to IRAs"),
    ("p590b.pdf",    "https://www.irs.gov/pub/irs-pdf/p590b.pdf",
     "IRS Pub 590-B — Distributions from IRAs"),
    ("i1040gi.pdf",  "https://www.irs.gov/pub/irs-pdf/i1040gi.pdf",
     "IRS Form 1040 general instructions"),
    ("p550.pdf",     "https://www.irs.gov/pub/irs-pdf/p550.pdf",
     "IRS Pub 550 — Investment Income and Expenses (bonus: wash sales, basis)"),
    ("34-86031.pdf", "https://www.sec.gov/rules/interp/2019/34-86031.pdf",
     "SEC Release 34-86031 — broker-dealer standard of conduct"),
]

# Public but copyrighted — do not redistribute; print links for a manual grab.
LINKED = [
    ("vanguards_principles_for_investing_success.pdf",
     "https://corporate.vanguard.com/content/dam/corp/research/pdf/Principles_for_investing_success.pdf"),
    ("periodic-table-collection.pdf",
     "search: 'Callan periodic table of investment returns PDF' (annual editions)"),
]


def main():
    dest = os.path.abspath(DEST)
    os.makedirs(dest, exist_ok=True)
    print("Knowledge folder:", dest)
    ok = skip = fail = 0
    for name, url, label in PUBLIC:
        path = os.path.join(dest, name)
        if os.path.exists(path) and os.path.getsize(path) > 10_000 and not FORCE:
            print("  = %-16s already present (%s)" % (name, label))
            skip += 1
            continue
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if not data.startswith(b"%PDF"):
                raise ValueError("response is not a PDF (source may have moved)")
            with open(path, "wb") as f:      # write only AFTER validation — no stub on failure
                f.write(data)
            print("  + %-16s %s (%.1f MB)" % (name, label, len(data) / 1e6))
            ok += 1
        except Exception as e:
            print("  ! %-16s FAILED: %s — %s" % (name, e, url))
            fail += 1
    print("\nCopyrighted-but-public (grab manually if wanted, drop into %s):" % dest)
    for name, url in LINKED:
        print("  · %-50s %s" % (name, url))
    print("\n%d downloaded, %d already present, %d failed." % (ok, skip, fail))
    print("Next: REINDEX (dashboard ⚙ SETTINGS → KNOWLEDGE, or POST /api/knowledge/reindex).")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
