"""
knowledge.py — lightweight, local, user-populated RAG for the Fiduciary.

Point KNOWLEDGE_DIR at a folder (bind-mounted from the host, read-only); drop .md / .txt / .pdf
files in and they're indexed for the Fiduciary chat's `search_knowledge` tool. No cloud, no
heavyweight vector DB — the corpus is small (a handful of reference docs), so we embed chunks via
the LLM adapter's bge-m3-style embed model and do cosine similarity in pure Python.

RESUME-TRACKER 2026-07-24h — Knowledge/RAG DB-backed storage (design finalized 2026-07-23g):
content is now imported into SQLite (`store.py`'s knowledge_docs/knowledge_chunks/knowledge_paths/
knowledge_removed tables), keyed by a SHA-256 hash of each file's raw bytes, NOT by path. This
replaces the old flat `/data/knowledge_index.json` blob (path+mtime/size keyed). Consequences,
all deliberate:
  - TRUE dedup: the same content under two different filenames is embedded exactly once — a
    reindex that finds a hash already in `knowledge_docs` just points the new path at it.
  - A rename (same bytes, new path) no longer forces a re-embed — only an actual content change
    (which changes the hash) does.
  - The DB is now the source of truth for what's SEARCHABLE, not the folder. Deleting a file from
    the (still read-only) folder does NOT remove it from the index by itself anymore — only the
    new in-app `delete_doc()` action does. The folder remains purely the intake mechanism: a
    reindex still auto-imports anything new dropped into it, exactly like before.
  - If a document is explicitly removed via `delete_doc()` but its source file is still sitting
    in the folder, the next reindex will see its hash as "new" again and re-import it — surfaced
    in the reindex result's `reimported` list rather than happening silently (Jason, 2026-07-23g:
    this cuts both ways — a bug magnet if the doc was meant to be retired permanently, but a
    safety net if the wrong one was deleted by mistake — so don't suppress it either way).

Legacy `/data/knowledge_index.json` (if one exists from before this migration) is imported once,
on first use, WITHOUT re-embedding anything: the old vectors are paired with a freshly-computed
content hash for each file still present on disk, so a deploy of this change doesn't burn GPU
time re-embedding a corpus that was already indexed. The legacy file is left on disk untouched
afterward (never deleted), same convention as Storage Stage A/B.
"""
import hashlib
import io
import os
import json
import math
import re
import threading
import time

import llm     # RESUME-TRACKER 2026-07-24 — LLM-agnostic backend adapter (_llm_chat/_llm_embed)
import store   # RESUME-TRACKER 2026-07-24h — Knowledge/RAG DB-backed storage

KNOWLEDGE_DIR = os.environ.get("KNOWLEDGE_DIR", "/knowledge")
LEGACY_KNOWLEDGE_INDEX = os.environ.get("KNOWLEDGE_INDEX", "/data/knowledge_index.json")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
CHUNK_CHARS = int(os.environ.get("KNOWLEDGE_CHUNK_CHARS", "1400"))
CHUNK_OVERLAP = int(os.environ.get("KNOWLEDGE_CHUNK_OVERLAP", "200"))
EMBED_TIMEOUT = int(os.environ.get("KNOWLEDGE_EMBED_TIMEOUT", "120"))
SUPPORTED = (".md", ".txt", ".pdf")

# Synthetic "path" used as a store.py app_state key for a couple of small scalars (last-built-at,
# last-active embed model) — reuses Stage A's generic key-value table instead of a new one-row
# table for two values. `store.write_json` never touches disk for this; `store.read_json` returns
# `default` if the key isn't there yet (no real file named this ever needs to exist).
_META_KEY = "knowledge_meta.json"

# ---- reindex progress (module-level, lock-protected — polled by GET /api/knowledge while
# a background job thread runs ensure_index(force=True)). Chunk-level, not a rough estimate.
_PROGRESS_LOCK = threading.Lock()
_PROGRESS = {"reindexing": False, "done_chunks": 0, "total_chunks": 0, "current_file": None}
_LAST_RESULT = {"reimported": [], "errors": []}   # from the most recent ensure_index() pass

_MIGRATION_LOCK = threading.Lock()
_migration_done = False


def progress():
    """Snapshot of the in-flight (or last) reindex progress, for the UI to poll."""
    with _PROGRESS_LOCK:
        return dict(_PROGRESS)


def _set_progress(**kw):
    with _PROGRESS_LOCK:
        _PROGRESS.update(kw)


def _meta():
    return store.read_json(_META_KEY, default={}) or {}


def _set_meta(**kw):
    m = _meta()
    m.update(kw)
    store.write_json(_META_KEY, m)


def _supported_files():
    """(relpath, abspath, mtime, size) for every supported file under KNOWLEDGE_DIR."""
    out = []
    for root, _dirs, files in os.walk(KNOWLEDGE_DIR):
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in SUPPORTED:
                continue
            ap = os.path.join(root, fn)
            try:
                st = os.stat(ap)
            except OSError:
                continue
            rel = os.path.relpath(ap, KNOWLEDGE_DIR)
            out.append((rel, ap, int(st.st_mtime), int(st.st_size)))
    return sorted(out, key=lambda x: x[0])


def _hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _extract_text_bytes(ext, raw):
    if ext in (".md", ".txt"):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except Exception:
            return ""   # pypdf not installed -> skip PDFs gracefully
        try:
            reader = PdfReader(io.BytesIO(raw))
            return "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception:
            return ""
    return ""


def _chunk(text):
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    chunks, i, n = [], 0, len(text)
    while i < n:
        end = min(n, i + CHUNK_CHARS)
        if end < n:                       # try to break on a paragraph/sentence boundary near the end
            b = text.rfind("\n\n", i, end)
            if b < 0:
                b = text.rfind(". ", i, end)
            if b > i + CHUNK_CHARS // 2:
                end = b + 1
        seg = text[i:end].strip()
        if seg:
            chunks.append(seg)
        i = max(end - CHUNK_OVERLAP, end) if end >= n else end - CHUNK_OVERLAP
    return chunks


def _embed_one(text):
    return llm._llm_embed(text, EMBED_MODEL, timeout=EMBED_TIMEOUT)


def _cos(a, b):
    if not a or not b:
        return -1.0
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (s / (na * nb)) if na and nb else -1.0


def _migrate_legacy_index():
    """One-time: if the DB has no documents yet AND the old flat-JSON index exists on disk,
    import its already-embedded vectors WITHOUT re-embedding — pairs each old (path, vectors)
    entry with a freshly-computed content hash for the file (still readable from the folder at
    migration time), so this deploy doesn't cost a full corpus re-embed. No-ops instantly on
    every call after the first (module-level guard), and no-ops permanently once the DB already
    has content (so it never overwrites anything a real import has since built)."""
    global _migration_done
    if _migration_done:
        return
    with _MIGRATION_LOCK:
        if _migration_done:
            return
        _migration_done = True
        if store.knowledge_docs_all():
            return   # DB already has content -- not a fresh install, don't touch it
        try:
            with open(LEGACY_KNOWLEDGE_INDEX, encoding="utf-8") as f:
                legacy = json.load(f) or {}
        except Exception:
            return
        vecs = legacy.get("vectors") or []
        if not vecs:
            return
        legacy_model = legacy.get("model") or EMBED_MODEL
        by_file = {}
        for v in vecs:
            by_file.setdefault(v.get("file"), []).append(v)
        now = int(time.time())
        for (rel, ap, mt, sz) in _supported_files():
            file_vecs = by_file.get(rel)
            if not file_vecs:
                continue
            try:
                with open(ap, "rb") as f:
                    raw = f.read()
            except Exception:
                continue
            h = _hash_bytes(raw)
            if store.knowledge_doc_get(h):
                store.knowledge_path_upsert(rel, h, mt, sz)
                continue
            store.knowledge_doc_upsert(h, rel, sz, now, raw)
            ordered = sorted(file_vecs, key=lambda v: v.get("chunk", 0))
            chunk_rows = [(v.get("chunk", i), v.get("text", ""), v.get("vec") or [])
                         for i, v in enumerate(ordered)]
            store.knowledge_chunks_replace(h, legacy_model, chunk_rows)
            store.knowledge_path_upsert(rel, h, mt, sz)
        _set_meta(model=legacy_model, built_at=legacy.get("built_at") or now)


def ensure_index(force=False):
    """Incrementally import the knowledge folder into SQLite and embed any new/changed content.
    Reuses existing chunks for unchanged (same-hash) content — including across a rename, and
    including true cross-file duplicates (dedup for free). A change to EMBED_MODEL forces a full
    re-embed of every stored document (from its saved bytes — the folder doesn't need to still
    have the file). Returns {"reimported": [...], "errors": [...]} for THIS pass."""
    _migrate_legacy_index()
    meta = _meta()
    same_model = meta.get("model") == EMBED_MODEL
    files = _supported_files()
    known_paths = store.knowledge_paths_all()

    changed = force or not same_model
    if not changed:
        for (rel, ap, mt, sz) in files:
            kp = known_paths.get(rel)
            if not kp or kp["mtime"] != mt or kp["size"] != sz:
                changed = True
                break
    if not changed:
        return {"reimported": [], "errors": []}

    reimported, errors = [], []
    _set_progress(reindexing=True, done_chunks=0, total_chunks=0, current_file=None)
    try:
        # ---- pass 1: everything currently in the folder ----
        prepared = []   # (rel, hash, raw, chunks)
        covered_hashes = set()
        queued_hashes = set()   # hashes already queued for embedding THIS pass (true cross-file
                                # dedup even before either occurrence has actually been written to
                                # knowledge_docs/knowledge_chunks yet)
        for (rel, ap, mt, sz) in files:
            kp = known_paths.get(rel)
            needs_check = force or not same_model or not kp or kp["mtime"] != mt or kp["size"] != sz
            if not needs_check:
                continue
            try:
                with open(ap, "rb") as f:
                    raw = f.read()
            except Exception as e:
                errors.append(f"{rel}: could not read ({e})")
                continue
            h = _hash_bytes(raw)
            covered_hashes.add(h)
            already_embedded = same_model and (store.knowledge_chunk_count(h) > 0 or h in queued_hashes)
            if already_embedded:
                # unchanged content at this path, OR a dedup match to content imported under a
                # different filename (possibly one queued earlier in this SAME pass) -- no re-embed
                # needed, just (re)point this path at it.
                store.knowledge_path_upsert(rel, h, mt, sz)
                if not store.knowledge_doc_get(h):
                    store.knowledge_doc_upsert(h, rel, sz, int(time.time()), raw)
                continue
            removed = store.knowledge_removed_get(h)
            if removed:
                reimported.append(rel)
            queued_hashes.add(h)
            ext = os.path.splitext(ap)[1].lower()
            text = _extract_text_bytes(ext, raw)
            chunks = _chunk(text)
            prepared.append((rel, mt, sz, h, raw, chunks, bool(removed)))

        total_chunks = sum(len(p[5]) for p in prepared)
        _set_progress(total_chunks=total_chunks)
        now = int(time.time())
        for (rel, mt, sz, h, raw, chunks, was_removed) in prepared:
            chunk_rows = []
            for ci, ch in enumerate(chunks):
                _set_progress(current_file=rel)
                try:
                    vec = _embed_one(ch)
                except Exception as e:
                    errors.append(f"{rel}: {e}")
                    vec = []
                if vec:
                    chunk_rows.append((ci, ch, vec))
                with _PROGRESS_LOCK:
                    _PROGRESS["done_chunks"] += 1
            if not chunks:
                errors.append(f"{rel}: no extractable text")
            store.knowledge_doc_upsert(h, rel, sz, now, raw)
            store.knowledge_chunks_replace(h, EMBED_MODEL, chunk_rows)
            store.knowledge_path_upsert(rel, h, mt, sz)
            if was_removed:
                store.knowledge_removed_clear(h)

        # ---- pass 2: model changed -- also refresh any stored doc NOT covered by the folder
        # scan above (its source file may since have been removed from the folder; the DB is
        # the source of truth, so a model upgrade still needs to reach it from its saved bytes).
        if not same_model:
            for d in store.knowledge_docs_all():
                h = d["hash"]
                if h in covered_hashes:
                    continue
                raw = store.knowledge_doc_get_content(h)
                if raw is None:
                    continue
                ext = os.path.splitext(d.get("filename") or "")[1].lower()
                text = _extract_text_bytes(ext, raw)
                chunks = _chunk(text)
                with _PROGRESS_LOCK:
                    _PROGRESS["total_chunks"] += len(chunks)
                chunk_rows = []
                for ci, ch in enumerate(chunks):
                    _set_progress(current_file=d.get("filename"))
                    try:
                        vec = _embed_one(ch)
                    except Exception as e:
                        errors.append(f"{d.get('filename')}: {e}")
                        vec = []
                    if vec:
                        chunk_rows.append((ci, ch, vec))
                    with _PROGRESS_LOCK:
                        _PROGRESS["done_chunks"] += 1
                store.knowledge_chunks_replace(h, EMBED_MODEL, chunk_rows)

        _set_meta(model=EMBED_MODEL, built_at=now)
        _LAST_RESULT["reimported"] = reimported
        _LAST_RESULT["errors"] = errors[:20]
        return {"reimported": reimported, "errors": errors[:20]}
    finally:
        _set_progress(reindexing=False, current_file=None)


def status():
    """Folder + index status for the UI (no vectors, no full chunk text). Includes live
    reindex progress and the most recent pass's `reimported` list, so the panel can poll this
    single endpoint while a background reindex job is running."""
    _migrate_legacy_index()
    files = _supported_files()
    known_paths = store.knowledge_paths_all()
    meta = _meta()
    stale = meta.get("model") != EMBED_MODEL
    if not stale:
        for (rel, ap, mt, sz) in files:
            kp = known_paths.get(rel)
            if not kp or kp["mtime"] != mt or kp["size"] != sz:
                stale = True
                break
    docs = store.knowledge_docs_all()
    chunk_counts = {d["hash"]: store.knowledge_chunk_count(d["hash"]) for d in docs}
    out = {
        "dir": KNOWLEDGE_DIR, "model": EMBED_MODEL,
        "built_at": meta.get("built_at"), "stale": bool(stale),
        "doc_count": len(docs), "chunk_count": sum(chunk_counts.values()),
        "errors": _LAST_RESULT.get("errors") or [],
        "reimported": _LAST_RESULT.get("reimported") or [],
        "docs": [{"hash": d["hash"], "file": d.get("filename"), "size": d.get("size"),
                  "chunks": chunk_counts.get(d["hash"], 0)} for d in docs],
        "note": "Drop .md/.txt/.pdf files into the knowledge folder; they're imported + indexed "
                "for the Fiduciary's search_knowledge tool. Content is deduped by hash — the same "
                "file under two names is only embedded once. Removing a file from the folder no "
                "longer removes it from the index by itself; use the delete action below for that.",
    }
    out.update(progress())
    return out


def list_docs():
    """Doc metadata for the SETTINGS -> KNOWLEDGE delete UI (same shape as status()['docs'])."""
    docs = store.knowledge_docs_all()
    return [{"hash": d["hash"], "file": d.get("filename"), "size": d.get("size"),
             "chunks": store.knowledge_chunk_count(d["hash"])} for d in docs]


def delete_doc(doc_hash):
    """In-app removal (the genuinely new capability this migration was built for): drops a
    document + its chunks from the DB. The source file, if still in the read-only folder, is
    NOT touched -- it will be flagged as 'reimported' on the next reindex that finds it again."""
    doc_hash = (doc_hash or "").strip().lower()
    if not doc_hash:
        return {"ok": False, "error": "missing hash"}
    if not store.knowledge_doc_get(doc_hash):
        return {"ok": False, "error": "no document with that hash"}
    fn = store.knowledge_doc_delete(doc_hash)
    return {"ok": True, "deleted": doc_hash, "filename": fn}


def search(query, k=4):
    """Embed the query and return the top-k most similar chunks (with source file + score).
    Auto-(re)indexes if the folder changed. Read-only w.r.t. the folder (writes only to the DB)."""
    query = (query or "").strip()
    if not query:
        return {"error": "empty query", "results": []}
    ensure_index()
    vecs = store.knowledge_chunks_all()
    if not vecs:
        return {"results": [], "note": "knowledge base is empty or could not be embedded "
                "(is the embed model pulled and the LLM backend reachable?)",
                "errors": _LAST_RESULT.get("errors") or []}
    try:
        qv = _embed_one(query)
    except Exception as e:
        return {"error": f"query embedding failed: {e}", "results": []}
    scored = sorted(((round(_cos(qv, v["vec"]), 4), v) for v in vecs), key=lambda x: -x[0])
    results = [{"source": v["file"], "score": sc, "text": v["text"]} for (sc, v) in scored[:max(1, int(k))]]
    return {"query": query, "results": results,
            "note": "Educational reference material — cite the source doc; verify against current IRS/SEC guidance."}
