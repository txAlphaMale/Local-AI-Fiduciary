"""
test_llm_settings_boot.py -- regression tests for "UI-saved LLM settings never take effect".

Bug: app.py imports llm.py BEFORE _boot_merge() copies SETTINGS-tab values (LLM_API_DIALECT,
LLM_BASE_URL, EMBED_BASE_URL, ... from the settings row; LLM_API_KEY/EMBED_API_KEY from the
secrets store) into os.environ. llm.py used to compute DIALECT/BASE_URL/EMBED_BASE_URL once at
import, so the merged values were never seen and the process kept talking to the default
host.docker.internal:11434. llm.py now resolves them lazily on every call.

The boot tests run a FRESH interpreter per case (importing app.py has heavy, process-global side
effects), with every /data/* path app code defaults to redirected into a temp dir. They put the
value ONLY in the saved settings/secrets rows -- no env var -- import app (which runs the real
_boot_merge), then capture the URL/headers llm.py actually uses on a real _llm_chat/_llm_embed
call. Zero network: requests.post and urllib.request.urlopen are stubbed in the child.

Run:  python3 -m unittest test_llm_settings_boot -v     (needs requirements.txt installed)
"""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# Child process: seed the store, boot the real app module, then make real llm calls against stubs.
_CHILD = r'''
import json, os, sys, urllib.request
sys.path.insert(0, %(here)r)
import store
store.init_db()
seed = json.loads(os.environ["_TEST_SETTINGS"])
if seed:
    store.write_json(os.environ["SETTINGS_FILE"], seed)
secrets_seed = json.loads(os.environ["_TEST_SECRETS"])
if secrets_seed:
    import secrets_crypto
    store.write_json(os.environ["SECRETS_FILE"],
                     {k: secrets_crypto.encrypt(v) for k, v in secrets_seed.items()})
import app          # noqa: F401  -- import runs the REAL _boot_merge()
import llm, requests

seen = {"post": [], "urlopen": []}

class _Resp:
    status_code = 200
    text = "{}"
    def raise_for_status(self): pass
    def json(self):
        return {"message": {"content": "ok"}, "choices": [{"message": {"content": "ok"}}],
                "content": [{"type": "text", "text": "ok"}], "data": [{"embedding": [0.0]}]}

def _post(url, **kw):
    seen["post"].append({"url": url, "headers": dict(kw.get("headers") or {})})
    return _Resp()

class _U:
    def read(self): return b'{"embedding": [0.0]}'
    def __enter__(self): return self
    def __exit__(self, *a): return False

def _urlopen(req, *a, **kw):
    seen["urlopen"].append(req.full_url)
    return _U()

requests.post = _post
urllib.request.urlopen = _urlopen
for fn in (lambda: llm._llm_chat([{"role": "user", "content": "hi"}], "m"),
           lambda: llm._llm_embed("hi", "e")):
    try:
        fn()
    except Exception as e:      # response-shape parsing on the stub is irrelevant; URL capture is the test
        seen.setdefault("errors", []).append(f"{type(e).__name__}: {e}")
seen["dialect"] = llm._dialect()
seen["embed_dialect"] = llm._embed_dialect()
print("RESULT::" + json.dumps(seen))
'''


def _data_path_env(tmp):
    """Redirect every `os.environ.get("X", "/data/...")` default in the app's sources into tmp."""
    env = {}
    pat = re.compile(r'os\.(?:environ\.get|getenv)\(\s*"([A-Z0-9_]+)"\s*,\s*"(/data[^"]*)"')
    for path in glob.glob(os.path.join(HERE, "*.py")):
        with open(path, encoding="utf-8") as fh:
            for name, default in pat.findall(fh.read()):
                env[name] = os.path.join(tmp, default.lstrip("/"))
    return env


def boot(settings=None, secrets=None, real_env=None):
    """Boot app.py in a fresh interpreter; return what llm.py actually called."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("LLM_", "EMBED_", "OLLAMA_", "ANTHROPIC_"))}
        env.update(_data_path_env(tmp))
        env.update({"_TEST_SETTINGS": json.dumps(settings or {}),
                    "_TEST_SECRETS": json.dumps(secrets or {}),
                    "INTERNAL_SCHEDULER": "0", "PYTHONDONTWRITEBYTECODE": "1"})
        env.update(real_env or {})
        p = subprocess.run([sys.executable, "-c", _CHILD % {"here": HERE}], env=env, cwd=HERE,
                           capture_output=True, text=True, timeout=180)
        for line in p.stdout.splitlines():
            if line.startswith("RESULT::"):
                return json.loads(line[len("RESULT::"):])
        raise AssertionError(f"child produced no result (rc={p.returncode})\n{p.stderr[-3000:]}")


class TestUISavedLLMSettings(unittest.TestCase):
    def test_saved_llm_base_url_is_the_url_actually_called(self):
        # The exact case from the bug report: value ONLY in the saved settings row.
        r = boot(settings={"LLM_BASE_URL": "http://x:1"})
        self.assertEqual([c["url"] for c in r["post"]][:1], ["http://x:1/api/chat"], r)
        self.assertFalse(any("11434" in c["url"] for c in r["post"]), r)

    def test_saved_embed_base_url_is_the_url_actually_called(self):
        r = boot(settings={"LLM_BASE_URL": "http://x:1", "EMBED_BASE_URL": "http://y:2"})
        self.assertEqual(r["urlopen"], ["http://y:2/api/embeddings"], r)

    def test_embed_url_inherits_saved_llm_url_when_same_dialect(self):
        r = boot(settings={"LLM_BASE_URL": "http://x:1"})
        self.assertEqual(r["urlopen"], ["http://x:1/api/embeddings"], r)

    def test_saved_dialect_takes_effect(self):
        r = boot(settings={"LLM_API_DIALECT": "openai", "LLM_BASE_URL": "http://x:1"})
        self.assertEqual(r["dialect"], "openai")
        self.assertEqual(r["post"][0]["url"], "http://x:1/v1/chat/completions", r)

    def test_saved_secret_api_key_takes_effect(self):
        # Same import-order bug class for the secrets store (LLM_API_KEY is merged from it).
        r = boot(settings={"LLM_API_DIALECT": "openai", "LLM_BASE_URL": "http://x:1"},
                 secrets={"LLM_API_KEY": "sk-test-123"})
        self.assertEqual(r["post"][0]["headers"].get("Authorization"), "Bearer sk-test-123", r)

    def test_real_env_still_wins_over_saved_setting(self):
        r = boot(settings={"LLM_BASE_URL": "http://saved:1"}, real_env={"LLM_BASE_URL": "http://env:2"})
        self.assertEqual(r["post"][0]["url"], "http://env:2/api/chat", r)

    def test_blank_env_var_does_not_beat_saved_setting_or_default(self):
        # compose passthrough of an empty .env var arrives as "" -- must count as unset
        r = boot(settings={"LLM_BASE_URL": "http://x:1"}, real_env={"LLM_BASE_URL": ""})
        self.assertEqual(r["post"][0]["url"], "http://x:1/api/chat", r)
        r = boot(real_env={"LLM_BASE_URL": ""})
        self.assertEqual(r["post"][0]["url"], "http://host.docker.internal:11434/api/chat", r)


class TestLazyResolvers(unittest.TestCase):
    """llm.py helpers read os.environ at CALL time (no app import needed)."""

    def setUp(self):
        sys.path.insert(0, HERE)
        import llm
        self.llm = llm
        self._saved = {k: os.environ.get(k) for k in
                       ("LLM_API_DIALECT", "LLM_BASE_URL", "EMBED_API_DIALECT", "EMBED_BASE_URL",
                        "LLM_API_KEY", "EMBED_API_KEY", "OLLAMA_BASE_URL")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_changes_after_import_are_seen(self):
        self.assertEqual(self.llm._base_url(), "http://host.docker.internal:11434")
        os.environ["LLM_BASE_URL"] = "http://a:1/"
        self.assertEqual(self.llm._base_url(), "http://a:1")
        os.environ["LLM_API_DIALECT"] = " OpenAI "
        self.assertEqual(self.llm._dialect(), "openai")

    def test_anthropic_defaults_and_embed_fallback(self):
        os.environ["LLM_API_DIALECT"] = "anthropic"
        self.assertEqual(self.llm._base_url(), "https://api.anthropic.com")
        self.assertEqual(self.llm._embed_dialect(), "ollama")           # Anthropic has no embeddings
        self.assertEqual(self.llm._embed_base_url(), "http://host.docker.internal:11434")
        os.environ["EMBED_BASE_URL"] = "http://e:3"
        self.assertEqual(self.llm._embed_base_url(), "http://e:3")

    def test_embed_anthropic_raises_at_call_time_not_import(self):
        os.environ["EMBED_API_DIALECT"] = "anthropic"
        with self.assertRaises(RuntimeError):
            self.llm._llm_embed("x", "m")


if __name__ == "__main__":
    unittest.main()
