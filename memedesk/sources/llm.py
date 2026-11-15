"""LLM client for the AI book. Gemini (free key from aistudio.google.com) today; the interface is one
method, so another provider (e.g. a paid Claude model) can be dropped in later without touching the desk.
"""
from __future__ import annotations

import json
import logging

from ..http import DataError, HttpClient

log = logging.getLogger("memedesk.llm")

GEMINI = "https://generativelanguage.googleapis.com/v1beta"


class Gemini:
    def __init__(self, http: HttpClient, api_key: str, model: str = ""):
        self.http, self.key = http, api_key
        self.model = model or "gemini-2.5-flash"
        self._resolved = False

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.key}

    def _pick_model(self) -> None:
        """If the configured model isn't available on this key, use the newest 'flash' model that is."""
        data = self.http.get(f"{GEMINI}/models", headers=self._headers(), retries=2, cache_ttl=3600)
        names = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        flash = sorted((n for n in names if "flash" in n and "lite" not in n and "image" not in n
                        and "tts" not in n and "live" not in n), reverse=True)
        if flash:
            log.warning("gemini model %s unavailable, using %s", self.model, flash[0])
            self.model = flash[0]

    def json(self, system: str, user: str, max_tokens: int = 2048) -> object:
        """One call, JSON out. Raises DataError on transport or parse failure."""
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens,
                                 "responseMimeType": "application/json"},
        }
        url = f"{GEMINI}/models/{self.model}:generateContent"
        try:
            res = self.http.post(url, json=body, headers=self._headers(), retries=2)
        except DataError as e:
            if "HTTP 404" in str(e) and not self._resolved:
                self._resolved = True
                self._pick_model()
                res = self.http.post(f"{GEMINI}/models/{self.model}:generateContent", json=body,
                                     headers=self._headers(), retries=2)
            else:
                raise
        try:
            text = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
            start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [0])
            return json.JSONDecoder().raw_decode(text[start:])[0]     # ignores trailing ``` fences
        except (KeyError, IndexError, ValueError) as e:
            raise DataError(f"gemini reply not JSON: {e}") from None
