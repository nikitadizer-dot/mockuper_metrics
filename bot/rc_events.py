"""Хранилище событий вебхуков RevenueCat (append-only JSON Lines).

Метрик-API RevenueCat не отдаёт поток «новые/продления/отписки» по периоду,
поэтому мы принимаем вебхуки (INITIAL_PURCHASE / RENEWAL / CANCELLATION / ...)
и складываем их в файл. Отчёт потом считает события за нужный период.

Ограничение: файл лежит на диске воркера. На Render диск эфемерный —
при редеплое события до него теряются. Для продакшена этого достаточно
(отчёт за день/неделю обычно не пересекает редеплой), но знать стоит.
"""

import json
import logging
import os
import threading
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_lock = threading.Lock()

# Типы событий RevenueCat, которые нас интересуют.
NEW = "INITIAL_PURCHASE"
RENEWAL = "RENEWAL"
CANCELLATION = "CANCELLATION"      # отключили автопродление (деньги не возвращаются)
UNCANCELLATION = "UNCANCELLATION"
EXPIRATION = "EXPIRATION"          # подписка реально закончилась (отвалились)
REFUND = "CANCELLATION_REFUND"     # возврат денег (см. cancel_reason)
NON_RENEWING = "NON_RENEWING_PURCHASE"


def append_event(path: str, event: dict) -> None:
    """Сохраняет одно событие вебхука. `event` — объект `event` из payload RC."""
    rec = {
        "ts": int(event.get("event_timestamp_ms") or 0),
        "type": event.get("type", ""),
        "product_id": event.get("product_id", ""),
        "store": event.get("store", ""),
        # RevenueCat присылает цену в USD в поле `price`, в валюте покупки — отдельно.
        "price": float(event.get("price") or 0),
        "currency": event.get("currency", ""),
        "cancel_reason": event.get("cancel_reason", ""),
        "period_type": event.get("period_type", ""),
        "id": event.get("id", ""),
    }
    with _lock:
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")


def _read(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with _lock:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def summarize(path: str, start: date, end: date, tz: ZoneInfo) -> dict | None:
    """Считает поток подписок за [start, end) по локальному tz.

    Возвращает None, если событий нет вовсе (вебхук ещё не настроен).
    """
    events = _read(path)
    if not events:
        return None

    start_ms = int(datetime.combine(start, time(0, 0), tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.combine(end, time(0, 0), tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)

    seen: set[str] = set()
    agg = {"new": 0, "renewal": 0, "cancellation": 0, "expiration": 0, "refund": 0,
           "new_weekly": 0, "new_yearly": 0, "renewal_revenue": 0.0, "new_revenue": 0.0,
           "in_window": 0}
    for e in events:
        if not (start_ms <= e["ts"] < end_ms):
            continue
        # дедуп по id события (вебхуки могут дублироваться при ретраях)
        eid = e.get("id")
        if eid:
            if eid in seen:
                continue
            seen.add(eid)
        agg["in_window"] += 1
        t = e["type"]
        is_yearly = "yearly" in e.get("product_id", "").lower() or "annual" in e.get("product_id", "").lower()
        if t == NEW:
            agg["new"] += 1
            agg["new_revenue"] += e["price"]
            agg["new_yearly" if is_yearly else "new_weekly"] += 1
        elif t == RENEWAL:
            agg["renewal"] += 1
            agg["renewal_revenue"] += e["price"]
        elif t == CANCELLATION:
            if e.get("cancel_reason") in ("REFUND", "CUSTOMER_SUPPORT") or e["price"] < 0:
                agg["refund"] += 1
            else:
                agg["cancellation"] += 1
        elif t == REFUND:
            agg["refund"] += 1
        elif t == EXPIRATION:
            agg["expiration"] += 1
    return agg
