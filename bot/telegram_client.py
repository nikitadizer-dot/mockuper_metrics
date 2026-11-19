"""Минимальный клиент Telegram Bot API.

Важно: бот только шлёт сообщения и (опционально) читает команды через
long polling. Он никогда не трогает вебхуки (setWebhook/deleteWebhook),
чтобы не сломать существующую отправку генераций из приложения.
"""

import logging

import requests

log = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self, token: str, timeout: int = 65):
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout
        self.session = requests.Session()

    def send_message(
        self, chat_id: str | int, text: str, parse_mode: str | None = None
    ) -> None:
        # Telegram ограничивает сообщение 4096 символами — режем на части.
        for chunk in _chunks(text, 4000):
            resp = self.session.post(
                f"{self.base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                    **({"parse_mode": parse_mode} if parse_mode else {}),
                },
                timeout=self.timeout,
            )
            if resp.status_code >= 400:
                log.error("sendMessage failed %s: %s", resp.status_code, resp.text[:300])
                resp.raise_for_status()

    def get_updates(self, offset: int | None, poll_timeout: int = 50) -> list[dict]:
        params: dict = {"timeout": poll_timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        resp = self.session.get(
            f"{self.base}/getUpdates", params=params, timeout=poll_timeout + 15
        )
        if resp.status_code == 409:
            # Кто-то ещё читает апдейты (вебхук или второй инстанс).
            raise ConflictError(resp.text[:300])
        resp.raise_for_status()
        return resp.json().get("result", [])


class ConflictError(Exception):
    pass


def _chunks(text: str, size: int):
    for i in range(0, len(text), size):
        yield text[i : i + size]
