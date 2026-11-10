"""AI client. Main AI: Google Gemini (hosted, free tier). Fallbacks: Qwen through Ollama (local) or OpenRouter.

provider = "auto"       → Gemini if a key is set, then local Ollama, then OpenRouter
provider = "gemini"     → Google Gemini only (needs GEMINI_API_KEY)
provider = "ollama"     → local only (free, private)
provider = "openrouter" → OpenRouter only (needs an API key)

Every call returns plain text; ask_json() also extracts the JSON object from it.
"""
from __future__ import annotations

import json
import re
import time

import requests

from ..config import settings
from ..dsa.ratelimit import SlidingWindowLimiter

# Shared by every request in this process: protects the server's own OpenRouter key on a public site.
limiter = SlidingWindowLimiter(settings.ai_per_hour, settings.ai_per_day) if settings.public else None


class AIError(RuntimeError):
    pass


class AIUnavailable(AIError):
    """No provider is configured or reachable — callers fall back to rule-based logic."""


_THINK = re.compile(r"<think>.*?</think>", re.S)


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (handles ```json fences and chatter)."""
    t = _THINK.sub("", str(text)).replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise AIError("No JSON in the model reply")
    s = min(starts)
    for e in range(len(t), s, -1):
        if t[e - 1] in "}]":
            try:
                return json.loads(t[s:e])
            except json.JSONDecodeError:
                continue
    raise AIError("Could not parse JSON from the model reply")


class AIClient:
    _ollama_cache: dict = {}

    def __init__(self, provider=None, openrouter_key=None, ollama_model=None, openrouter_model=None, ollama_url=None, visitor=None,
                 gemini_key=None, gemini_model=None):
        self.provider = (provider or settings.ai_provider or "auto").lower()
        # Only calls paid with the server's key are metered; a visitor's own key is theirs to spend.
        self.quota_key = visitor if (limiter and not (openrouter_key or gemini_key)) else None
        self.gemini_key = gemini_key or settings.gemini_key
        self.gemini_model = gemini_model or settings.gemini_model
        self.openrouter_key = openrouter_key or settings.openrouter_key
        self.ollama_model = ollama_model or settings.ollama_model
        self.openrouter_model = openrouter_model or settings.openrouter_model
        self.ollama_url = (ollama_url or settings.ollama_url).rstrip("/")
        self.used = None  # "ollama:<model>" / "openrouter:<model>" after a call

    # ---------- discovery ----------
    def ollama_models(self):
        """List local models (cached for 20 s so every request doesn't ping Ollama)."""
        hit = self._ollama_cache.get(self.ollama_url)
        if hit and time.time() - hit[0] < 20:
            return hit[1]
        try:
            r = requests.get(self.ollama_url + "/api/tags", timeout=1.5)
            models = [m["name"] for m in r.json().get("models", [])] if r.ok else None
        except (requests.RequestException, ValueError):
            models = None
        self._ollama_cache[self.ollama_url] = (time.time(), models)
        return models

    def ollama_ready(self) -> bool:
        models = self.ollama_models()
        if not models:
            return False
        want = self.ollama_model if ":" in self.ollama_model else self.ollama_model + ":latest"
        return any(m == want or m.split(":")[0] == self.ollama_model for m in models)

    def order(self):
        if self.provider == "ollama":
            return ["ollama"]
        if self.provider == "openrouter":
            return ["openrouter"] if self._quota_ok(consume=False) else []
        if self.provider == "gemini":
            return ["gemini"] if self._quota_ok(consume=False) else []
        out = []
        if self.gemini_key and self._quota_ok(consume=False):
            out.append("gemini")
        if self.ollama_ready():
            out.append("ollama")
        if self.openrouter_key and self._quota_ok(consume=False):
            out.append("openrouter")
        return out

    def _quota_ok(self, consume: bool) -> bool:
        return self.quota_key is None or limiter.check(self.quota_key, consume=consume)

    def status(self):
        models = self.ollama_models()
        return {
            "provider": self.provider,
            "ollama": {"url": self.ollama_url, "running": models is not None, "model": self.ollama_model, "model_installed": self.ollama_ready(), "models": models or []},
            "gemini": {"configured": bool(self.gemini_key), "model": self.gemini_model},
            "render": {"available": bool(self.gemini_key), "model": settings.gemini_image_model, "hd_model": settings.gemini_image_model_hd},
            "openrouter": {"configured": bool(self.openrouter_key), "model": self.openrouter_model,
                           **({"remaining_this_hour": limiter.remaining(self.quota_key)} if self.quota_key else {})},
            "public": settings.public,
            "active": (self.order() or [None])[0],
        }

    def label(self):
        p = (self.order() or [None])[0]
        return {"gemini": f"Google Gemini · {self.gemini_model}", "ollama": f"Qwen · {self.ollama_model} (local)", "openrouter": f"OpenRouter · {self.openrouter_model}"}.get(p, "rule-based engine")

    def model_of(self, p):
        return {"gemini": self.gemini_model, "ollama": self.ollama_model, "openrouter": self.openrouter_model}[p]

    # ---------- calls ----------
    def chat(self, messages, temperature: float = 0.3, max_tokens: int = 2048, want_json: bool = True, timeout: float | None = None) -> str:
        """messages: [{'role': 'user'|'assistant'|'system', 'text': str, 'images': [base64 jpeg]}]"""
        providers = self.order()
        if not providers:
            if (self.gemini_key or self.openrouter_key) and not self._quota_ok(consume=False):
                raise AIUnavailable("The free AI allowance for this hour is used up — DIMORA continues with its rule-based engine. "
                                    "Add your own Gemini API key in Settings to keep using the AI.")
            raise AIUnavailable("No AI provider: set a Google Gemini API key (GEMINI_API_KEY), or start Ollama, or set an OpenRouter key.")
        errors = []
        for p in providers:
            if p in ("openrouter", "gemini") and not self._quota_ok(consume=True):
                errors.append(f"{p}: AI allowance used up")
                continue
            try:
                fn = {"ollama": self._ollama, "openrouter": self._openrouter, "gemini": self._gemini}[p]
                text = _THINK.sub("", fn(messages, temperature, max_tokens, want_json, timeout or settings.ai_timeout)).strip()
                if not text:
                    raise AIError("Empty reply")
                self.used = f"{p}:{self.model_of(p)}"
                return text
            except AIError as e:
                errors.append(f"{p}: {e}")
        raise AIError("; ".join(errors))

    def ask_json(self, prompt: str, images=None, **kw):
        return extract_json(self.chat([{"role": "user", "text": prompt, "images": images or []}], **kw))

    def _ollama(self, messages, temperature, max_tokens, want_json, timeout):
        body = {
            "model": self.ollama_model, "stream": False, "think": False,
            "messages": [{"role": m["role"], "content": m.get("text", ""), **({"images": m["images"]} if m.get("images") else {})} for m in messages],
            "options": {"temperature": temperature, "num_predict": max_tokens, "top_p": 0.8, "top_k": 20},
        }
        if want_json:
            body["format"] = "json"
        for attempt in range(2):
            try:
                r = requests.post(self.ollama_url + "/api/chat", json=body, timeout=timeout)
            except requests.RequestException as e:
                raise AIError(f"Ollama not reachable ({e.__class__.__name__})") from e
            if r.status_code == 400 and "think" in r.text and attempt == 0:
                body.pop("think", None)  # older Ollama / non-thinking model
                continue
            if not r.ok:
                raise AIError(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
            return (r.json().get("message") or {}).get("content", "")
        raise AIError("Ollama rejected the request")

    @staticmethod
    def _content(m):
        if not m.get("images"):
            return m.get("text", "")
        return [{"type": "text", "text": m.get("text", "")}] + [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for b in m["images"]]

    def _openai_style(self, name, url, headers, body, hints, timeout, optional):
        """POST an OpenAI-format chat request; on HTTP 400 drop optional params one at a time and retry."""
        for _ in range(len(optional) + 1):
            try:
                r = requests.post(url, json=body, headers=headers, timeout=timeout)
            except requests.RequestException as e:
                raise AIError(f"{name} not reachable ({e.__class__.__name__})") from e
            if r.status_code == 400:
                drop = next((k for k in optional if k in body), None)
                if drop:
                    body.pop(drop)
                    continue
            if not r.ok:
                try:
                    j = r.json()
                    err = j[0].get("error", {}) if isinstance(j, list) else j.get("error", {})
                    msg = err.get("message", r.text[:200]) if isinstance(err, dict) else str(err)[:200]
                except ValueError:
                    msg = r.text[:200]
                raise AIError(f"{name} HTTP {r.status_code}: {hints.get(r.status_code) or msg}")
            j = r.json()
            return ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        raise AIError(f"{name} rejected the request")

    def _gemini(self, messages, temperature, max_tokens, want_json, timeout):
        body = {"model": self.gemini_model, "max_tokens": max_tokens, "temperature": temperature, "reasoning_effort": "low",
                "messages": [{"role": m["role"], "content": self._content(m)} for m in messages]}
        if want_json:
            body["response_format"] = {"type": "json_object"}
        hints = {400: "Gemini rejected the request", 401: "the Gemini API key was rejected — check GEMINI_API_KEY",
                 403: "this Gemini key isn't allowed to use the API — check it in Google AI Studio",
                 404: f"Gemini model {self.gemini_model} not found — set GEMINI_MODEL to a current model",
                 429: "Gemini free-tier limit reached — wait a minute, or enable billing in Google AI Studio"}
        return self._openai_style("Gemini", settings.gemini_url.rstrip("/") + "/chat/completions",
                                  {"Authorization": f"Bearer {self.gemini_key}"}, body, hints, timeout,
                                  ["temperature", "reasoning_effort", "response_format"])

    # ---------- images (AI photo renders) ----------
    def image_models(self, hd: bool = False):
        first = settings.gemini_image_model_hd if hd else settings.gemini_image_model
        chain = [first] + ([settings.gemini_image_model] if hd else []) + list(settings.gemini_image_fallbacks)
        return list(dict.fromkeys(m for m in chain if m))

    def render_image(self, prompt: str, image_b64: str, mime: str = "image/jpeg", hd: bool = False,
                     aspect: str = "4:3", timeout: float | None = None) -> dict:
        """Repaint an image from a text instruction with a Gemini image model ("Nano Banana").

        Uses Gemini's own generateContent endpoint (the OpenAI-compatible one only returns text).
        Returns {"image": base64, "mime": str, "model": str, "note": str}."""
        if not self.gemini_key:
            raise AIUnavailable("AI photo renders use Google Gemini. Set GEMINI_API_KEY on the server, "
                                "or add your own Gemini key in ⚙ Settings.")
        base = settings.gemini_url.rstrip("/")
        base = base[: -len("/openai")] if base.endswith("/openai") else base
        tried = []
        for model in self.image_models(hd):
            gen = {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": aspect, "imageSize": "2K" if hd else "1K"}}
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}, {"inlineData": {"mimeType": mime, "data": image_b64}}]}],
                    "generationConfig": gen}
            r = None
            for _ in range(3):  # older image models reject imageSize / imageConfig: drop them and retry
                try:
                    r = requests.post(f"{base}/models/{model}:generateContent", json=body,
                                      headers={"x-goog-api-key": self.gemini_key}, timeout=timeout or settings.render_timeout)
                except requests.RequestException as e:
                    raise AIError(f"Gemini not reachable ({e.__class__.__name__})") from e
                msg = self._google_error(r)
                if r.status_code == 400 and "imageSize" in gen.get("imageConfig", {}) and re.search(r"image_?size", msg, re.I):
                    gen["imageConfig"].pop("imageSize")
                    continue
                if r.status_code == 400 and "imageConfig" in gen and re.search(r"image_?config|aspect", msg, re.I):
                    gen.pop("imageConfig")
                    continue
                break
            if r.status_code == 404:
                tried.append(model)
                continue
            if not r.ok:
                msg = self._google_error(r)
                hints = {401: "the Gemini API key was rejected — check GEMINI_API_KEY",
                         403: "this Gemini key can't use image models — check it in Google AI Studio",
                         429: "the Gemini image quota is used up — wait a minute, or enable billing in Google AI Studio "
                              "(image models have little or no free allowance)"}
                if r.status_code == 400 and re.search(r"billing|free.?tier|paid", msg, re.I):
                    msg = "image models need billing enabled in Google AI Studio"
                raise AIError(f"Gemini image HTTP {r.status_code}: {hints.get(r.status_code) or msg}")
            j = r.json()
            cand = (j.get("candidates") or [{}])[0] or {}
            image, mime_out, notes = None, "image/png", []
            for p in (cand.get("content") or {}).get("parts") or []:
                blob = p.get("inlineData") or p.get("inline_data")
                if blob and blob.get("data") and not p.get("thought"):
                    image, mime_out = blob["data"], blob.get("mimeType") or blob.get("mime_type") or "image/png"
                elif p.get("text") and not p.get("thought"):
                    notes.append(p["text"].strip())
            if not image:
                why = (j.get("promptFeedback") or {}).get("blockReason") or cand.get("finishReason") or "no image in the reply"
                extra = f" — {notes[0][:200]}" if notes else ""
                raise AIError(f"Gemini didn't return an image ({why}){extra}. Try again, or change the notes.")
            self.used = f"gemini:{model}"
            return {"image": image, "mime": mime_out, "model": model, "note": " ".join(notes)[:400]}
        raise AIError("Gemini image model not found (tried " + ", ".join(tried) + ") — set GEMINI_IMAGE_MODEL to a current model")

    @staticmethod
    def _google_error(r) -> str:
        if r.ok:
            return ""
        try:
            j = r.json()
            err = j[0].get("error", {}) if isinstance(j, list) else j.get("error", {})
            return (err.get("message") if isinstance(err, dict) else str(err)) or r.text[:200]
        except ValueError:
            return r.text[:200]

    def _openrouter(self, messages, temperature, max_tokens, want_json, timeout):
        body = {"model": self.openrouter_model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": m["role"], "content": self._content(m)} for m in messages],
                "reasoning": {"enabled": False}}
        if want_json:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.openrouter_key}", "HTTP-Referer": "https://github.com/dimora", "X-Title": "DIMORA"}
        hints = {401: "the API key was rejected — check OPENROUTER_API_KEY", 402: "the OpenRouter account is out of credits",
                 403: "this key isn't allowed to use that model", 404: f"model {self.openrouter_model} not found",
                 429: "rate-limited by OpenRouter — try again in a moment"}
        return self._openai_style("OpenRouter", settings.openrouter_url + "/chat/completions", headers, body, hints, timeout,
                                  ["response_format", "reasoning"])


def client_from_request(headers, body=None, visitor=None) -> AIClient:
    """Per-request overrides from the frontend's AI settings (headers win over .env).

    On a public site a visitor must not be able to make the server call an arbitrary URL, so the
    Ollama URL override is ignored there (the server's own OLLAMA_URL still applies)."""
    body = body or {}
    return AIClient(
        provider=headers.get("X-AI-Provider") or body.get("ai_provider"),
        openrouter_key=headers.get("X-OpenRouter-Key") or None,
        ollama_model=headers.get("X-Ollama-Model") or None,
        openrouter_model=headers.get("X-OpenRouter-Model") or None,
        gemini_key=headers.get("X-Gemini-Key") or None,
        gemini_model=headers.get("X-Gemini-Model") or None,
        ollama_url=None if settings.public else (headers.get("X-Ollama-Url") or None),
        visitor=visitor,
    )
