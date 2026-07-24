"""
llm.py -- single chokepoint for every outbound LLM network call in this app.

RESUME-TRACKER 2026-07-24: before this module existed, 7 call sites (6 in app.py --
_ollama_generate, _do_narrative, _do_strategist_one, _do_advice, _vision_identify_tickers,
_chat_stream -- plus 1 in knowledge.py -- _embed_one) each built their own raw
requests.post/urllib.request call directly against an Ollama base URL. That made this app
permanently Ollama-only and meant the same request-building/response-parsing logic was
duplicated 7 times. This module collapses all of it into two functions -- _llm_chat and
_llm_embed -- dialect-switchable via an env var, so the rest of the app never touches a raw
HTTP call to the model backend again.

Dialect switch (env vars, read once at import):
  LLM_API_DIALECT=ollama (default) -- talks to Ollama's native /api/chat + /api/embeddings.
  LLM_API_DIALECT=openai           -- talks to any OpenAI-compatible endpoint's
                                       /v1/chat/completions + /v1/embeddings (vLLM, llama.cpp
                                       server, LM Studio, text-generation-webui, or real OpenAI).
  LLM_BASE_URL   -- base URL for either dialect. Falls back to OLLAMA_BASE_URL when unset, so
                     existing "ollama" deployments need ZERO env changes to keep working exactly
                     as before.
  LLM_API_KEY    -- Bearer token sent only for the openai dialect (Ollama needs none locally).

Two entry points, both dialect-agnostic from the caller's point of view:

  _llm_chat(messages, model, tools=None, stream=False, temperature=0.3, num_ctx=None,
            keep_alive=None, format_json=False, think=None, timeout=60, images=None,
            cancel_check=None)
      Non-streaming (stream=False): returns an LLMResult -- .ok, .status_code, .content,
      .thinking, .tool_calls (already normalized to [{"function": {"name", "arguments"(dict)}}]
      regardless of dialect), .error, .raw.
      Streaming (stream=True): returns a GENERATOR yielding dicts shaped
        {"content": str|None, "thinking": str|None, "tool_calls": list|None, "done": bool,
         "error": str|None, "cancelled": bool}
      -- tool_calls, when present, is the COMPLETE list for the turn, emitted exactly once
      (matching Ollama's own native stream behavior; the openai dialect accumulates per-token
      tool-call deltas internally and only surfaces the assembled list once, so callers never
      have to know the difference). `cancel_check`, if given, is a zero-arg callable polled
      between chunks -- return True to abort and close the underlying connection early.

  _llm_embed(text, model, timeout=120) -> list[float]
      Returns the embedding vector, or [] on a soft failure from the dialect-specific call
      (network/HTTP errors otherwise propagate, matching the previous _embed_one contract).

Network exceptions (timeout, connection error) are NOT swallowed by _llm_chat/_llm_embed --
they propagate exactly as a raw requests.post()/urlopen() would, so every existing call site's
own except-block (which the migration to this module deliberately left in place) keeps working
unchanged. Only the transport + response-shape-normalization moved here; retry/error-message/
telemetry logic stays at each call site.
"""
import json
import os
import urllib.request

import requests

DIALECT = os.environ.get("LLM_API_DIALECT", "ollama").strip().lower()
BASE_URL = os.environ.get(
    "LLM_BASE_URL",
    os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
).rstrip("/")
API_KEY = os.environ.get("LLM_API_KEY", "")


class LLMResult:
    """Normalized non-streaming chat result."""
    __slots__ = ("ok", "status_code", "content", "thinking", "tool_calls", "error", "raw")

    def __init__(self, ok, status_code=None, content="", thinking="", tool_calls=None, error=None, raw=None):
        self.ok = ok
        self.status_code = status_code
        self.content = content or ""
        self.thinking = thinking or ""
        self.tool_calls = tool_calls or []
        self.error = error
        self.raw = raw


def _headers():
    h = {"Content-Type": "application/json"}
    if DIALECT == "openai" and API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def _with_images(messages, images):
    """Attach `images` (list of base64 strings, no data-URL prefix) to the LAST message --
    matches how every current caller only ever sends images on a single-turn vision call."""
    if not images:
        return list(messages)
    msgs = list(messages)
    if not msgs:
        return [{"role": "user", "content": "", "images": images}]
    msgs[-1] = dict(msgs[-1], images=images)
    return msgs


def _to_openai_messages(messages):
    """Ollama-shape messages (role/content[/images]) -> OpenAI chat messages. A message carrying
    `images` becomes an OpenAI vision content-parts array (image_url data: URLs)."""
    out = []
    for m in messages:
        imgs = m.get("images")
        if imgs:
            parts = [{"type": "text", "text": m.get("content") or ""}]
            for b64 in imgs:
                parts.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}})
            out.append({"role": m["role"], "content": parts})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def _normalize_tool_calls(tcs):
    """Both dialects' tool_calls end up in the same shape: arguments always a dict (parsed from
    a JSON string if the backend sent one -- Ollama does this inconsistently across versions)."""
    out = []
    for tc in (tcs or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except Exception:
                args = {}
        out.append({"function": {"name": fn.get("name"), "arguments": args or {}}})
    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def _llm_chat(messages, model, tools=None, stream=False, temperature=0.3, num_ctx=None,
              keep_alive=None, format_json=False, think=None, timeout=60, images=None,
              cancel_check=None):
    msgs = _with_images(messages, images)
    if DIALECT == "openai":
        if stream:
            return _openai_chat_stream(msgs, model, tools, temperature, format_json, timeout, cancel_check)
        return _openai_chat(msgs, model, tools, temperature, format_json, timeout)
    if stream:
        return _ollama_chat_stream(msgs, model, tools, temperature, num_ctx, keep_alive, timeout, cancel_check)
    return _ollama_chat(msgs, model, tools, temperature, num_ctx, keep_alive, format_json, think, timeout)


def _llm_embed(text, model, timeout=120):
    if DIALECT == "openai":
        r = requests.post(f"{BASE_URL}/v1/embeddings", json={"model": model, "input": text},
                          headers=_headers(), timeout=timeout)
        r.raise_for_status()
        items = (r.json() or {}).get("data") or []
        return (items[0].get("embedding") if items else None) or []
    body = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(BASE_URL + "/api/embeddings", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()).get("embedding") or []


# ---------------------------------------------------------------------------
# ollama dialect
# ---------------------------------------------------------------------------

def _ollama_chat(messages, model, tools, temperature, num_ctx, keep_alive, format_json, think, timeout):
    options = {"temperature": temperature}
    if num_ctx:
        options["num_ctx"] = num_ctx
    base = {"model": model, "messages": messages, "stream": False, "options": options}
    if tools:
        base["tools"] = tools
    if keep_alive is not None:
        base["keep_alive"] = keep_alive
    if format_json:
        base["format"] = "json"
    # think/no_think retry: some Ollama builds reject the `think` field outright -- fall back to
    # omitting it entirely rather than failing the whole call (preserves _ollama_generate's
    # original two-attempt behavior exactly).
    attempts = [dict(base, think=think), base] if think is not None else [base]
    last_err = "no response"
    for body in attempts:
        r = requests.post(f"{BASE_URL}/api/chat", json=body, timeout=timeout)
        if r.status_code >= 400:
            if "think" in r.text.lower() and body.get("think") is not None:
                last_err = f"ollama {r.status_code}: {r.text[:300]}"
                continue
            return LLMResult(False, status_code=r.status_code, error=f"ollama {r.status_code}: {r.text[:300]}")
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            if "think" in str(data["error"]).lower() and body.get("think") is not None:
                last_err = str(data["error"])
                continue
            return LLMResult(False, status_code=r.status_code, error=str(data["error"]), raw=data)
        msg = data.get("message") or {}
        return LLMResult(True, status_code=r.status_code, content=msg.get("content") or "",
                         thinking=msg.get("thinking") or "",
                         tool_calls=_normalize_tool_calls(msg.get("tool_calls")), raw=data)
    return LLMResult(False, error=last_err)


def _ollama_chat_stream(messages, model, tools, temperature, num_ctx, keep_alive, timeout, cancel_check):
    options = {"temperature": temperature}
    if num_ctx:
        options["num_ctx"] = num_ctx
    body = {"model": model, "messages": messages, "stream": True, "options": options}
    if tools:
        body["tools"] = tools
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    with requests.post(f"{BASE_URL}/api/chat", json=body, timeout=timeout, stream=True) as resp:
        if resp.status_code >= 400:
            yield {"content": None, "thinking": None, "tool_calls": None, "done": True,
                   "error": f"ollama {resp.status_code}: {resp.text[:200]}", "cancelled": False}
            return
        for line in resp.iter_lines():
            if cancel_check and cancel_check():
                try:
                    resp.close()
                except Exception:
                    pass
                yield {"content": None, "thinking": None, "tool_calls": None, "done": True,
                       "error": None, "cancelled": True}
                return
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            m = chunk.get("message") or {}
            tcs = m.get("tool_calls")
            yield {"content": m.get("content") or None, "thinking": m.get("thinking") or None,
                   "tool_calls": _normalize_tool_calls(tcs) if tcs else None,
                   "done": bool(chunk.get("done")), "error": None, "cancelled": False}
            if chunk.get("done"):
                return


# ---------------------------------------------------------------------------
# openai dialect
# ---------------------------------------------------------------------------

def _openai_chat(messages, model, tools, temperature, format_json, timeout):
    body = {"model": model, "messages": _to_openai_messages(messages), "temperature": temperature,
             "stream": False}
    if tools:
        body["tools"] = tools
    if format_json:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{BASE_URL}/v1/chat/completions", json=body, headers=_headers(), timeout=timeout)
    if r.status_code >= 400:
        return LLMResult(False, status_code=r.status_code, error=f"openai {r.status_code}: {r.text[:300]}")
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return LLMResult(True, status_code=r.status_code, content=msg.get("content") or "",
                     tool_calls=_normalize_tool_calls(msg.get("tool_calls")), raw=data)


def _finalize_openai_tool_acc(tool_acc):
    out = []
    for _, slot in sorted(tool_acc.items()):
        args = slot["arguments"]
        try:
            args = json.loads(args) if args else {}
        except Exception:
            args = {}
        out.append({"function": {"name": slot["name"], "arguments": args}})
    return out


def _openai_chat_stream(messages, model, tools, temperature, format_json, timeout, cancel_check):
    body = {"model": model, "messages": _to_openai_messages(messages), "temperature": temperature,
             "stream": True}
    if tools:
        body["tools"] = tools
    if format_json:
        body["response_format"] = {"type": "json_object"}
    tool_acc = {}  # delta index -> {"name": str|None, "arguments": accumulated str}
    with requests.post(f"{BASE_URL}/v1/chat/completions", json=body, headers=_headers(),
                       timeout=timeout, stream=True) as resp:
        if resp.status_code >= 400:
            yield {"content": None, "thinking": None, "tool_calls": None, "done": True,
                   "error": f"openai {resp.status_code}: {resp.text[:200]}", "cancelled": False}
            return
        for raw_line in resp.iter_lines():
            if cancel_check and cancel_check():
                try:
                    resp.close()
                except Exception:
                    pass
                yield {"content": None, "thinking": None, "tool_calls": None, "done": True,
                       "error": None, "cancelled": True}
                return
            if not raw_line:
                continue
            line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                tcs = _finalize_openai_tool_acc(tool_acc) if tool_acc else None
                yield {"content": None, "thinking": None, "tool_calls": tcs, "done": True,
                       "error": None, "cancelled": False}
                return
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            finish = choice.get("finish_reason")
            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index", 0)
                slot = tool_acc.setdefault(idx, {"name": None, "arguments": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            content = delta.get("content")
            if content:
                yield {"content": content, "thinking": None, "tool_calls": None, "done": False,
                       "error": None, "cancelled": False}
            if finish:
                tcs = _finalize_openai_tool_acc(tool_acc) if tool_acc else None
                yield {"content": None, "thinking": None, "tool_calls": tcs, "done": True,
                       "error": None, "cancelled": False}
                return
