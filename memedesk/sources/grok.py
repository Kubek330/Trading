"""Grok (xAI) as the desk's voice: second opinion on 70+ setups and the
morning report. Grok never sizes, never sets stops, never forces a buy."""
from __future__ import annotations

import json
import logging

from ..http import DataError, HttpClient

log = logging.getLogger("memedesk.grok")

ANALYST_PROMPT = """You are the second-opinion analyst on a Solana memecoin desk.
A deterministic model already scored this token >= 70 using on-chain data.
Look only at the JSON you are given. Flag anything that looks like wash trading,
a coordinated pump, or a dying chart. Reply with JSON only:
{"veto": true|false, "note": "<one sentence, max 25 words>"}"""

REPORT_PROMPT = """You are the desk's overnight reporter. Write a tight morning brief
(max 200 words) from this JSON: trades, PnL, the top kill reasons and what they say
about the market tonight, and anything the operator should change. No hype."""


class Grok:
    def __init__(self, http: HttpClient, base: str, api_key: str, model: str):
        self.http, self.base, self.key, self.model = http, base.rstrip("/"), api_key, model

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _chat(self, system: str, user: str, max_tokens: int = 400) -> str:
        res = self.http.post(
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            retries=2,
        )
        return res["choices"][0]["message"]["content"].strip()

    def second_opinion(self, payload: dict) -> tuple[bool, str]:
        if not self.enabled:
            return False, ""
        try:
            text = self._chat(ANALYST_PROMPT, json.dumps(payload, default=str))
            start, end = text.find("{"), text.rfind("}")
            obj = json.loads(text[start:end + 1])
            return bool(obj.get("veto")), str(obj.get("note", ""))[:300]
        except (DataError, KeyError, ValueError) as e:
            log.warning("grok second opinion failed: %s", e)
            return False, ""

    def morning_report(self, summary: dict) -> str:
        if not self.enabled:
            return ""
        try:
            return self._chat(REPORT_PROMPT, json.dumps(summary, default=str), 600)
        except (DataError, KeyError) as e:
            return f"(grok report failed: {e})"


class Telegram:
    def __init__(self, http: HttpClient, token: str, chat_id: str):
        self.http, self.token, self.chat_id = http, token, chat_id

    def send(self, text: str) -> None:
        if not (self.token and self.chat_id):
            return
        try:
            self.http.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                           json={"chat_id": self.chat_id, "text": text[:4000],
                                 "disable_web_page_preview": True}, retries=1)
        except DataError as e:
            log.warning("telegram send failed: %s", e)
