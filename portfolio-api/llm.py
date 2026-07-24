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
  LLM_API_DIALECT=anthropic (2026-07-24, Claude integration) -- talks to Anthropic's Messages API
                                       (/v1/messages). CHAT/ANALYSIS ONLY -- Anthropic has no
                                       embeddings endpoint at all, see EMBED_API_DIALECT below.
  LLM_BASE_URL   -- base URL for any dialect. Falls back to OLLAMA_BASE_URL for ollama/openai
                     (so existing deployments need ZERO env changes), or to
                     https://api.anthropic.com for the anthropic dialect.
  LLM_API_KEY    -- Bearer token (openai) or x-api-key (anthropic). Ollama needs none locally.

Embeddings are a SEPARATE dialect axis (EMBED_API_DIALECT/EMBED_BASE_URL/EMBED_API_KEY),
independent of the chat dialect above -- because Anthropic sells no embeddings API, "anthropic"
is not a legal value for EMBED_API_DIALECT. By default EMBED_API_DIALECT inherits LLM_API_DIALECT
(so ollama/openai deployments keep sharing one dialect/host/key for both chat and embeddings,
EXACTLY as before -- zero behavior change), except when LLM_API_DIALECT=anthropic, where it falls
back to "ollama" instead (embeddings stay local even when chat is hosted on Claude). Override
EMBED_API_DIALECT explicitly to point embeddings at a third backend independent of chat.

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


def _default_base_url(dialect):
    """Sensible default host per dialect when LLM_BASE_URL/EMBED_BASE_URL is unset."""
    if dialect == "anthropic":
        return "https://api.anthropic.com"
    return os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")


DIALECT = os.environ.get("LLM_API_DIALECT", "ollama").strip().lower()
BASE_URL = os.environ.get("LLM_BASE_URL", _default_base_url(DIALECT)).rstrip("/")
API_KEY = os.environ.get("LLM_API_KEY", "")
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
# Output-token ceiling for the anthropic dialect only (Ollama/OpenAI-compatible servers are
# bounded by LLM_TIMEOUT + the model's own context, not a separate max-tokens knob this app sets).
ANTHROPIC_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "8192"))

# ---- Embeddings dialect (independent axis -- see module docstring) --------------------------
EMBED_DIALECT = os.environ.get(
    "EMBED_API_DIALECT", DIALECT if DIALECT != "anthropic" else "ollama"
).strip().lower()
if EMBED_DIALECT == "anthropic":
    raise RuntimeError(
        "EMBED_API_DIALECT=anthropic is invalid -- Anthropic has no embeddings API. Set "
        "EMBED_API_DIALECT to 'ollama' or 'openai' (the latter can point at any OpenAI-compatible "
        "embeddings host, e.g. a Voyage AI or local vLLM/llama.cpp embeddings endpoint)."
    )
EMBED_BASE_URL = (
    os.environ.get("EMBED_BASE_URL")
    or (BASE_URL if EMBED_DIALECT == DIALECT else _default_base_url(EMBED_DIALECT))
).rstrip("/")
EMBED_API_KEY = os.environ.get("EMBED_API_KEY") or (API_KEY if EMBED_DIALECT == DIALECT else "")


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
    elif DIALECT == "anthropic":
        h["x-api-key"] = API_KEY
        h["anthropic-version"] = ANTHROPIC_VERSION
    return h


def _embed_headers():
    h = {"Content-Type": "application/json"}
    if EMBED_DIALECT == "openai" and EMBED_API_KEY:
        h["Authorization"] = f"Bearer {EMBED_API_KEY}"
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
    if DIALECT == "anthropic":
        if stream:
            return _anthropic_chat_stream(msgs, model, tools, temperature, format_json, timeout, cancel_check)
        return _anthropic_chat(msgs, model, tools, temperature, format_json, think, timeout)
    if DIALECT == "openai":
        if stream:
            return _openai_chat_stream(msgs, model, tools, temperature, format_json, timeout, cancel_check)
        return _openai_chat(msgs, model, tools, temperature, format_json, timeout)
    if stream:
        return _ollama_chat_stream(msgs, model, tools, temperature, num_ctx, keep_alive, timeout, cancel_check)
    return _ollama_chat(msgs, model, tools, temperature, num_ctx, keep_alive, format_json, think, timeout)


def _llm_embed(text, model, timeout=120):
    """Dialect-switchable on EMBED_API_DIALECT (NOT the chat DIALECT -- see module docstring)."""
    if EMBED_DIALECT == "openai":
        r = requests.post(f"{EMBED_BASE_URL}/v1/embeddings", json={"model": model, "input": text},
                          headers=_embed_headers(), timeout=timeout)
        r.raise_for_status()
        items = (r.json() or {}).get("data") or []
        return (items[0].get("embedding") if items else None) or []
    body = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(EMBED_BASE_URL + "/api/embeddings", data=body,
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


# ---------------------------------------------------------------------------
# anthropic dialect (2026-07-24, Claude integration)
#
# Anthropic's Messages API differs from the Ollama/OpenAI shape in three ways every caller in
# this app is shielded from:
#   1. The system prompt is a top-level `system` string, not a message with role="system".
#      This app occasionally injects a SECOND system message mid-conversation (the Strategist's
#      "you're almost out of steps" nudge) -- every system-role entry, wherever it appears, is
#      pulled out and concatenated into one string.
#   2. Tool calls/results are matched by id (`tool_use_id`), and a tool_result must be a
#      "user"-role message. This app never tracked per-call ids (Ollama/local OpenAI-compatible
#      servers don't require them) -- ids are synthesized fresh on every conversion, which is
#      safe because the message list is rebuilt from scratch on every _llm_chat call in this
#      app's ReAct loops (app.py always appends tool-result messages in the SAME order the
#      assistant emitted the matching tool_calls -- verified across every call site).
#   3. Messages must strictly alternate user/assistant (no two consecutive same-role entries) --
#      _to_anthropic_messages merges consecutive same-resolved-role entries into one.
#
# format_json=True (the ONE call site that uses it, _ollama_generate, never combines it with
# `tools`) is implemented via Anthropic's own recommended pattern for guaranteed-shape JSON: a
# single synthetic tool is forced via tool_choice, and its parsed `input` becomes `.content` --
# more reliable than Ollama's format="json" (a decoding-time grammar constraint with no shape
# guarantee), not just a same-effort substitute.
# ---------------------------------------------------------------------------

_JSON_TOOL_NAME = "emit_result"
_JSON_TOOL = {"name": _JSON_TOOL_NAME,
              "description": "Return your complete answer as a single JSON object matching what was asked for.",
              "input_schema": {"type": "object"}}


def _to_anthropic_messages(messages):
    """See the dialect-level docstring above. Returns (system_text, anthropic_messages)."""
    system_parts = []
    out = []
    pending_tool_ids = []

    def _push(role, blocks):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
            continue
        if role == "tool":
            tid = pending_tool_ids.pop(0) if pending_tool_ids else f"toolu_unmatched_{i}"
            _push("user", [{"type": "tool_result", "tool_use_id": tid,
                            "content": str(m.get("content") or "")}])
            continue
        blocks = []
        content = m.get("content")
        if content:
            blocks.append({"type": "text", "text": content})
        for b64 in (m.get("images") or []):
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": "image/png", "data": b64}})
        tcs = m.get("tool_calls")
        if tcs:
            pending_tool_ids = []
            for j, tc in enumerate(tcs):
                fn = (tc or {}).get("function") or {}
                tid = f"toolu_{i}_{j}"
                pending_tool_ids.append(tid)
                blocks.append({"type": "tool_use", "id": tid, "name": fn.get("name"),
                               "input": fn.get("arguments") or {}})
        _push(role if role in ("user", "assistant") else "user", blocks)
    return "\n\n".join(system_parts), out


def _to_anthropic_tools(tools):
    out = []
    for t in (tools or []):
        fn = (t or {}).get("function") or {}
        out.append({"name": fn.get("name"), "description": fn.get("description") or "",
                   "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _anthropic_body(messages, model, tools, temperature, format_json, stream):
    system_text, amsgs = _to_anthropic_messages(messages)
    body = {"model": model, "max_tokens": ANTHROPIC_MAX_TOKENS, "messages": amsgs,
            "temperature": temperature}
    if stream:
        body["stream"] = True
    if system_text:
        body["system"] = system_text
    atools = _to_anthropic_tools(tools) if tools else []
    forcing_json = False
    if format_json and not atools:
        atools = [_JSON_TOOL]
        body["tool_choice"] = {"type": "tool", "name": _JSON_TOOL_NAME}
        forcing_json = True
    if atools:
        body["tools"] = atools
    return body, forcing_json


def _anthropic_chat(messages, model, tools, temperature, format_json, think, timeout):
    body, forcing_json = _anthropic_body(messages, model, tools, temperature, format_json, stream=False)
    # Extended thinking: only offered on the single-turn, no-tools, non-JSON-forcing path (the
    # only think=True caller in this app, _ollama_generate, never combines it with tools) --
    # thinking requires temperature==1 and Anthropic rejects a forced tool_choice alongside it,
    # so JSON-forcing takes priority if both were somehow requested together.
    if think and not tools and not forcing_json:
        body["thinking"] = {"type": "enabled", "budget_tokens": min(4096, ANTHROPIC_MAX_TOKENS - 1024)}
        body["temperature"] = 1
    r = requests.post(f"{BASE_URL}/v1/messages", json=body, headers=_headers(), timeout=timeout)
    if r.status_code >= 400:
        return LLMResult(False, status_code=r.status_code, error=f"anthropic {r.status_code}: {r.text[:300]}")
    data = r.json()
    content_text, thinking_text, tcs = "", "", []
    for block in (data.get("content") or []):
        bt = block.get("type")
        if bt == "text":
            content_text += block.get("text") or ""
        elif bt == "thinking":
            thinking_text += block.get("thinking") or ""
        elif bt == "tool_use":
            if forcing_json and block.get("name") == _JSON_TOOL_NAME:
                content_text = json.dumps(block.get("input") or {})
            else:
                tcs.append({"function": {"name": block.get("name"), "arguments": block.get("input") or {}}})
    return LLMResult(True, status_code=r.status_code, content=content_text, thinking=thinking_text,
                     tool_calls=tcs, raw=data)


def _anthropic_chat_stream(messages, model, tools, temperature, format_json, timeout, cancel_check):
    body, forcing_json = _anthropic_body(messages, model, tools, temperature, format_json, stream=True)
    block_types, tool_meta, tool_json, tool_order = {}, {}, {}, []
    with requests.post(f"{BASE_URL}/v1/messages", json=body, headers=_headers(),
                       timeout=timeout, stream=True) as resp:
        if resp.status_code >= 400:
            yield {"content": None, "thinking": None, "tool_calls": None, "done": True,
                   "error": f"anthropic {resp.status_code}: {resp.text[:200]}", "cancelled": False}
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
            if not payload:
                continue
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            et = ev.get("type")
            if et == "content_block_start":
                idx = ev.get("index")
                cb = ev.get("content_block") or {}
                block_types[idx] = cb.get("type")
                if cb.get("type") == "tool_use":
                    tool_meta[idx] = {"id": cb.get("id"), "name": cb.get("name")}
                    tool_json[idx] = ""
                    tool_order.append(idx)
            elif et == "content_block_delta":
                idx = ev.get("index")
                delta = ev.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    yield {"content": delta.get("text") or None, "thinking": None, "tool_calls": None,
                           "done": False, "error": None, "cancelled": False}
                elif dt == "thinking_delta":
                    yield {"content": None, "thinking": delta.get("thinking") or None, "tool_calls": None,
                           "done": False, "error": None, "cancelled": False}
                elif dt == "input_json_delta":
                    tool_json[idx] = tool_json.get(idx, "") + (delta.get("partial_json") or "")
            elif et == "message_stop":
                if forcing_json:
                    idx = tool_order[0] if tool_order else None
                    raw_args = tool_json.get(idx, "") if idx is not None else ""
                    yield {"content": raw_args or "{}", "thinking": None, "tool_calls": None,
                           "done": True, "error": None, "cancelled": False}
                    return
                tcs = None
                if tool_order:
                    tcs = []
                    for idx in tool_order:
                        meta = tool_meta.get(idx) or {}
                        raw_args = tool_json.get(idx, "")
                        try:
                            args = json.loads(raw_args) if raw_args else {}
                        except Exception:
                            args = {}
                        tcs.append({"function": {"name": meta.get("name"), "arguments": args}})
                yield {"content": None, "thinking": None, "tool_calls": tcs, "done": True,
                       "error": None, "cancelled": False}
                return
            elif et == "error":
                yield {"content": None, "thinking": None, "tool_calls": None, "done": True,
                       "error": str((ev.get("error") or {}).get("message") or ev), "cancelled": False}
                return
