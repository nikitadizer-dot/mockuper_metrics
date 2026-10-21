"""Минимальный клиент PostHog Query API (HogQL)."""

import logging
import time

import requests

log = logging.getLogger(__name__)


class PostHogError(Exception):
    pass


class PostHogClient:
    def __init__(self, host: str, api_key: str, project_id: str, timeout: int = 60):
        self.url = f"{host}/api/projects/{project_id}/query/"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def query(self, hogql: str, attempts: int = 4) -> list[list]:
        """Выполняет HogQL-запрос и возвращает строки результата.

        PostHog периодически отвечает 503 clickhouse_at_capacity или
        не укладывается в таймаут — такие сбои ретраим с паузой. Любая
        итоговая ошибка (в том числе сетевая) поднимается как PostHogError,
        чтобы вызывающий код мог её обработать, а не ронял процесс.
        """
        last = ""
        for i in range(attempts):
            try:
                resp = self.session.post(
                    self.url,
                    json={"query": {"kind": "HogQLQuery", "query": hogql}},
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code < 400:
                    return resp.json().get("results", [])
                last = f"PostHog API {resp.status_code}: {resp.text[:500]}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break
            if i < attempts - 1:
                log.warning("PostHog: попытка %d не удалась (%s), повтор", i + 1, last[:200])
                time.sleep(5 * (i + 1))
        raise PostHogError(last)
