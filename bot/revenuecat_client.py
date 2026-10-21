"""Минимальный клиент RevenueCat REST API v2.

Отдаёт то, что реально доступно через API дёшево (1-2 запроса):
  - выручку за произвольный период (gross и proceeds — «в карман» после Apple);
  - снапшот проекта (MRR, активные подписки, триалы, новые пользователи).

Поток «новые/продления/отписки» метрик-API не отдаёт — он собирается из
вебхуков (см. bot/rc_events.py).
"""

import logging

import requests

log = logging.getLogger(__name__)


class RevenueCatError(Exception):
    pass


class RevenueCatClient:
    def __init__(self, api_key: str, project_id: str, timeout: int = 30):
        self.base = f"https://api.revenuecat.com/v2/projects/{project_id}"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            resp = self.session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise RevenueCatError(f"RevenueCat недоступен: {type(e).__name__}: {e}") from e
        if resp.status_code >= 400:
            raise RevenueCatError(f"RevenueCat API {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def overview(self) -> dict:
        """Текущий снапшот: {metric_id: value}.

        Ключи: active_trials, active_subscriptions, mrr, revenue (28д),
        new_customers (28д), active_users (28д).
        """
        data = self._get("/metrics/overview")
        return {m["id"]: m["value"] for m in data.get("metrics", [])}

    def revenue(self, start_date: str, end_date: str, revenue_type: str = "revenue") -> float:
        """Выручка за период [start_date, end_date) в USD.

        revenue_type: 'revenue' (gross), 'proceeds' (после комиссии магазина),
        'revenue_net_of_taxes'. Даты в формате YYYY-MM-DD.
        """
        data = self._get(
            "/metrics/revenue",
            params={
                "start_date": start_date,
                "end_date": end_date,
                "revenue_type": revenue_type,
            },
        )
        return float(data.get("value", 0) or 0)
