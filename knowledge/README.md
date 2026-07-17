# Knowledge folder — the Fiduciary's local RAG library

Drop reference documents in **this folder** and the Fiduciary chat can search them via its
`search_knowledge` tool (tax rules, IRA/Roth mechanics, investing-policy guidance, your IPS…).

- **Supported:** `.md`, `.txt`, `.pdf` (put files directly here or in sub-folders).
- **Add content:** copy a file in → click **Reindex** in the dashboard's ⚙ Settings → KNOWLEDGE
  tab (or `POST /api/knowledge/reindex`). It's also picked up automatically the next time the
  tool runs after a change.
- **Remove content:** delete the file → reindex → it drops out.
- **How it works:** each doc's text is chunked and embedded with Ollama **`bge-m3`** (pull it:
  `ollama pull bge-m3`); the query is embedded and matched by cosine similarity. Vectors are
  cached in `/data/knowledge_index.json`. No cloud, all local.

Everything you add here is **git-ignored** (this README is the only committed file), because your
docs may be personal (e.g. your Investment Policy Statement). On the host they live in
`~/projects/Fiduciary/knowledge/`, bind-mounted read-only into the container.

## Committed reference set (`reference/`)

The `reference/` sub-folder ships WITH the repo (git-whitelisted, non-personal): estate &
generational transfer (G2), business-acquisition/QSBS frameworks (G11), and a dated snapshot of
the grounded 2026 tax/ACA/SS/estate parameters (G13 — live truth stays AUDITS → GROUNDING).
Regenerate the parameter snapshot at each January re-verification pass.

## Suggested starter set

A good default library (public docs) — e.g.:
- Your **Investment Policy Statement** (`.md`) — targets, risk tolerance, rebalancing rules.
- IRS **Pub 590-A / 590-B** (IRA contributions / distributions), **1040 instructions**.
- **Vanguard's Principles for Investing Success**, an asset-class "periodic table of returns".

> These came from the old Open WebUI knowledge collection. If you're migrating, copy them in:
> `cp <old>/owui/knowledge/Fiduciary/* ~/projects/Fiduciary/knowledge/` then Reindex.

**Or fetch the public set automatically:** `python3 scripts/fetch_public_knowledge.py` downloads
the public-domain starter docs (IRS 590-A/590-B/1040 instructions/550 + the SEC release) straight
from irs.gov/sec.gov into this folder, and prints links for the copyrighted-but-public extras
(Vanguard principles, a periodic table of returns) to grab manually. Then Reindex.

Educational reference only — not tax or investment advice; verify against current official guidance.
