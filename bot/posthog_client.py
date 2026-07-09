"""Минимальный клиент PostHog Query API (HogQL)."""

import logging

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

    def query(self, hogql: str) -> list[list]:
        """Выполняет HogQL-запрос и возвращает строки результата."""
        resp = self.session.post(
            self.url,
            json={"query": {"kind": "HogQLQuery", "query": hogql}},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise PostHogError(f"PostHog API {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        return data.get("results", [])
