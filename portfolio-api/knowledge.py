"""
knowledge.py — lightweight, local, user-populated RAG for the Fiduciary.

Point KNOWLEDGE_DIR at a folder (bind-mounted from the host); drop .md / .txt / .pdf
files in and they're indexed for the Fiduciary chat's `search_knowledge` tool. Remove a
file and it drops out on the next (re)index. No cloud, no heavyweight vector DB — the
corpus is small (a handful of reference docs), so we embed chunks with Ollama's `bge-m3`
and do cosine similarity in pure Python. The vector index is cached in /data and rebuilt
incrementally (only changed/added/removed files) when the folder changes.

Read-only: this only reads the knowledge folder and writes its own index cache.
"""
import os
import json
import math
import re
import time
import urllib.request

KNOWLEDGE_DIR = os.environ.get("KNOWLEDGE_DIR", "/knowledge")
KNOWLEDGE_INDEX = os.environ.get("KNOWLEDGE_INDEX", "/data/knowledge_index.json")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
CHUNK_CHARS = int(os.environ.get("KNOWLEDGE_CHUNK_CHARS", "1400"))
CHUNK_OVERLAP = int(os.environ.get("KNOWLEDGE_CHUNK_OVERLAP", "200"))
EMBED_TIMEOUT = int(os.environ.get("KNOWLEDGE_EMBED_TIMEOUT", "120"))
SUPPORTED = (".md", ".txt", ".pdf")


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


def _extract_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".md", ".txt"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except Exception:
            return ""   # pypdf not installed -> skip PDFs gracefully
        try:
            reader = PdfReader(path)
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
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode()
    req = urllib.request.Request(OLLAMA + "/api/embeddings", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as r:
        return json.loads(r.read().decode()).get("embedding") or []


def _cos(a, b):
    if not a or not b:
        return -1.0
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (s / (na * nb)) if na and nb else -1.0


def _load_index():
    try:
        with open(KNOWLEDGE_INDEX, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_index(idx):
    os.makedirs(os.path.dirname(KNOWLEDGE_INDEX) or ".", exist_ok=True)
    with open(KNOWLEDGE_INDEX, "w", encoding="utf-8") as f:
        json.dump(idx, f)


def _sig(files):
    return {rel: [mt, sz] for (rel, _ap, mt, sz) in files}


def ensure_index(force=False):
    """Incrementally (re)build the vector index if the folder changed. Reuses vectors for
    unchanged files; embeds only new/changed ones; drops removed ones. Returns the index."""
    files = _supported_files()
    cur = _sig(files)
    idx = {} if force else _load_index()
    same_model = idx.get("model") == EMBED_MODEL
    if not force and same_model and idx.get("sig") == cur and idx.get("vectors") is not None:
        return idx                                   # up to date

    old_vecs = idx.get("vectors") or []
    old_sig = idx.get("files") or {}
    by_file = {}
    if same_model:                                   # keep vectors for files whose mtime/size are unchanged
        for v in old_vecs:
            by_file.setdefault(v.get("file"), []).append(v)

    vectors, files_meta, errors = [], {}, []
    for (rel, ap, mt, sz) in files:
        unchanged = same_model and old_sig.get(rel) == [mt, sz] and by_file.get(rel)
        if unchanged:
            vectors.extend(by_file[rel])
            files_meta[rel] = {"mtime": mt, "size": sz, "chunks": len(by_file[rel])}
            continue
        text = _extract_text(ap)
        chunks = _chunk(text)
        n_ok = 0
        for ci, ch in enumerate(chunks):
            try:
                vec = _embed_one(ch)
            except Exception as e:
                errors.append(f"{rel}: {e}")
                vec = []
            if vec:
                vectors.append({"file": rel, "chunk": ci, "text": ch, "vec": vec})
                n_ok += 1
        files_meta[rel] = {"mtime": mt, "size": sz, "chunks": n_ok,
                           "note": ("no extractable text" if not chunks else None)}
    idx = {"model": EMBED_MODEL, "built_at": int(time.time()), "sig": cur,
           "files": files_meta, "vectors": vectors, "errors": errors[:20]}
    _save_index(idx)
    return idx


def status():
    """Folder + index status for the UI (no vectors, no full chunk text)."""
    files = _supported_files()
    idx = _load_index()
    stale = idx.get("sig") != _sig(files) or idx.get("model") != EMBED_MODEL
    meta = idx.get("files") or {}
    return {
        "dir": KNOWLEDGE_DIR, "model": EMBED_MODEL,
        "built_at": idx.get("built_at"), "stale": bool(stale),
        "doc_count": len(files), "chunk_count": len(idx.get("vectors") or []),
        "errors": idx.get("errors") or [],
        "docs": [{"file": rel, "size": sz, "chunks": (meta.get(rel) or {}).get("chunks")}
                 for (rel, _ap, _mt, sz) in files],
        "note": "Drop .md/.txt/.pdf files into the knowledge folder; they're indexed for the "
                "Fiduciary's search_knowledge tool. Remove a file and it drops out on reindex.",
    }


def search(query, k=4):
    """Embed the query and return the top-k most similar chunks (with source file + score).
    Auto-(re)indexes if the folder changed. Read-only."""
    query = (query or "").strip()
    if not query:
        return {"error": "empty query", "results": []}
    idx = ensure_index()
    vecs = idx.get("vectors") or []
    if not vecs:
        return {"results": [], "note": "knowledge base is empty or could not be embedded "
                "(is bge-m3 pulled and Ollama reachable?)", "errors": idx.get("errors") or []}
    try:
        qv = _embed_one(query)
    except Exception as e:
        return {"error": f"query embedding failed: {e}", "results": []}
    scored = sorted(((round(_cos(qv, v["vec"]), 4), v) for v in vecs), key=lambda x: -x[0])
    results = [{"source": v["file"], "score": sc, "text": v["text"]} for (sc, v) in scored[:max(1, int(k))]]
    return {"query": query, "results": results,
            "note": "Educational reference material — cite the source doc; verify against current IRS/SEC guidance."}
