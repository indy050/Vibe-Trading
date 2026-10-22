"""Telegram messages for the events that matter: orders, fills, exits and council decisions.

Create a bot with @BotFather, send it one message, and put the bot token and your
chat id in the server's environment (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). Never
in code or in the repository.
"""

from __future__ import annotations

import json
import os
import urllib.request

from .models import Event

_ICON = {"order_placed": "📝", "market_entry": "⚡", "filled": "✅", "exit": "🏁", "order_cancelled": "🗑️",
         "council": "🗣️", "news": "🗞️"}


def format_event(ev: Event) -> str | None:
    """The Telegram text for an engine event, or None when it is not worth a message."""
    if ev.kind == "council":
        if ev.data.get("approved") and not ev.data.get("objections"):
            return None  # everyone agreed: the order message follows anyway
        body = "\n".join(ev.data.get("transcript", []))
        return f"{_ICON['council']} Overleg US30 {ev.time:%H:%M}\n{body}"
    if ev.kind == "exit":
        sign = "+" if ev.data.get("pnl", 0) >= 0 else ""
        return f"{_ICON['exit']} {ev.message} Resultaat {sign}{ev.data.get('pnl', 0):.2f}."
    if ev.kind in _ICON:
        return f"{_ICON[ev.kind]} US30 {ev.time:%H:%M} · {ev.message}"
    return None


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, opener=urllib.request.urlopen) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        if not self.token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN en TELEGRAM_CHAT_ID zijn nodig")
        self.opener = opener

    def send(self, text: str) -> None:
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body,
                                     headers={"content-type": "application/json"}, method="POST")
        with self.opener(req, timeout=15) as resp:  # noqa: S310 - fixed Telegram API host
            resp.read()

    def on_event(self, ev: Event) -> None:
        """Plug into ``Us30Strategy(on_event=...)``. A failed send never stops trading."""
        text = format_event(ev)
        if not text:
            return
        try:
            self.send(text)
        except Exception:  # noqa: BLE001 - notification is best effort
            pass
