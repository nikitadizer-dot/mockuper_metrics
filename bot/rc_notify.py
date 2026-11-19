"""RevenueCat webhooks → сообщения в Telegram.

RevenueCat шлёт POST с JSON {"api_version": "1.0", "event": {...}} на каждую
покупку, продление, отмену, возврат и т.д. во всех приложениях проекта.
Документация: https://www.revenuecat.com/docs/integrations/webhooks
"""

import logging
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

EVENT_TITLES = {
    "INITIAL_PURCHASE": "🟢 Новая подписка",
    "RENEWAL": "🔁 Продление",
    "NON_RENEWING_PURCHASE": "🛒 Разовая покупка",
    "PRODUCT_CHANGE": "🔀 Смена тарифа",
    "UNCANCELLATION": "↩️ Подписку вернули",
    "CANCELLATION": "🟠 Отмена автопродления",
    "EXPIRATION": "⚫️ Подписка закончилась",
    "BILLING_ISSUE": "⚠️ Проблема с оплатой",
    "SUBSCRIPTION_PAUSED": "⏸ Подписка на паузе",
    "SUBSCRIPTION_EXTENDED": "➕ Подписка продлена (бонус)",
    "REFUND_REVERSED": "↩️ Возврат отменён",
    "TEMPORARY_ENTITLEMENT_GRANT": "⏳ Временный доступ",
    "TRANSFER": "🔄 Перенос покупок",
    "INVOICE_ISSUANCE": "🧾 Выставлен счёт",
    "VIRTUAL_CURRENCY_TRANSACTION": "🪙 Виртуальная валюта",
    "TEST": "🧪 Тестовое событие RevenueCat",
}

PERIOD_TYPES = {
    "TRIAL": "пробный период",
    "INTRO": "вводная цена",
    "NORMAL": "обычный",
    "PROMOTIONAL": "промо",
    "PREPAID": "предоплата",
}

STORES = {
    "APP_STORE": "App Store",
    "MAC_APP_STORE": "Mac App Store",
    "PLAY_STORE": "Google Play",
    "STRIPE": "Stripe",
    "PROMOTIONAL": "Промо (RevenueCat)",
    "AMAZON": "Amazon",
    "RC_BILLING": "Web Billing",
    "PADDLE": "Paddle",
    "ROKU": "Roku",
    "TEST_STORE": "Test Store",
}


class AppNames:
    """Подтягивает названия приложений по app_id через RevenueCat API v2.

    Ключ необязателен: без него в сообщении будет просто app_id.
    """

    def __init__(self, api_key: str | None):
        self.api_key = api_key
        self._names: dict[str, str] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def get(self, app_id: str | None) -> str | None:
        if not app_id:
            return None
        with self._lock:
            if app_id not in self._names and self.api_key and not self._loaded:
                self._load()
            return self._names.get(app_id)

    def _load(self) -> None:
        self._loaded = True
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            projects = requests.get(
                "https://api.revenuecat.com/v2/projects", headers=headers, timeout=15
            ).json().get("items", [])
            for p in projects:
                url = f"https://api.revenuecat.com/v2/projects/{p['id']}/apps?limit=100"
                apps = requests.get(url, headers=headers, timeout=15).json().get("items", [])
                for a in apps:
                    self._names[a["id"]] = a.get("name") or a["id"]
            log.info("RevenueCat: загружено %d приложений", len(self._names))
        except Exception:
            log.exception("RevenueCat: не удалось загрузить список приложений")


class Deduper:
    """RevenueCat может повторно прислать событие — не дублируем сообщение."""

    def __init__(self, size: int = 2000):
        self.size = size
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, event_id: str | None) -> bool:
        if not event_id:
            return False
        with self._lock:
            if event_id in self._seen:
                return True
            self._seen[event_id] = None
            if len(self._seen) > self.size:
                self._seen.popitem(last=False)
            return False


def _money(amount, currency) -> str | None:
    if amount is None:
        return None
    try:
        return f"{float(amount):.2f} {currency or ''}".strip()
    except (TypeError, ValueError):
        return None


def _ts(ms, tz: ZoneInfo) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz).strftime(
        "%d.%m.%Y %H:%M"
    )


def format_event(event: dict, app_name: str | None, tz: ZoneInfo) -> str:
    etype = event.get("type", "UNKNOWN")
    period = event.get("period_type")
    price = event.get("price")  # в USD, отрицательная при возврате

    title = EVENT_TITLES.get(etype, f"ℹ️ {etype}")
    if etype == "INITIAL_PURCHASE" and period == "TRIAL":
        title = "🆓 Начат пробный период"
    elif etype == "RENEWAL" and event.get("is_trial_conversion"):
        title = "💰 Триал → платная подписка"
    elif etype == "CANCELLATION" and event.get("cancel_reason") == "CUSTOMER_SUPPORT":
        title = "🔴 Возврат денег"
    elif price is not None and price < 0:
        title = "🔴 Возврат денег"

    lines = [f"<b>{title}</b>"]
    if event.get("environment") == "SANDBOX":
        lines[0] += " <i>(sandbox)</i>"

    app = app_name or event.get("app_id")
    if app:
        lines.append(f"📱 {_esc(app)}")

    product = event.get("product_id")
    if etype == "PRODUCT_CHANGE" and event.get("new_product_id"):
        product = f"{product} → {event['new_product_id']}"
    if product:
        lines.append(f"📦 {_esc(product)}")

    local = _money(event.get("price_in_purchased_currency"), event.get("currency"))
    usd = _money(price, "USD")
    if not price and not event.get("price_in_purchased_currency"):
        pass  # триал / бесплатное событие — цену не показываем
    elif local and usd and event.get("currency") != "USD":
        lines.append(f"💵 {local} (≈ {usd})")
    elif usd or local:
        lines.append(f"💵 {usd or local}")
    if price and event.get("takehome_percentage") is not None:
        net = price * float(event["takehome_percentage"])
        lines.append(f"🏦 Чистыми ≈ {net:.2f} USD")

    meta = []
    if period and period != "NORMAL":
        meta.append(PERIOD_TYPES.get(period, period.lower()))
    if event.get("store"):
        meta.append(STORES.get(event["store"], event["store"]))
    if event.get("country_code"):
        meta.append(event["country_code"])
    if meta:
        lines.append("🏷 " + " · ".join(meta))

    if event.get("offer_code"):
        lines.append(f"🎟 Промокод: {_esc(event['offer_code'])}")
    if event.get("cancel_reason"):
        lines.append(f"❓ Причина: {event['cancel_reason']}")
    if event.get("expiration_reason"):
        lines.append(f"❓ Причина: {event['expiration_reason']}")

    expires = _ts(event.get("expiration_at_ms"), tz)
    if expires and etype not in ("EXPIRATION", "NON_RENEWING_PURCHASE"):
        lines.append(f"📅 До: {expires}")

    if event.get("app_user_id"):
        lines.append(f"👤 <code>{_esc(event['app_user_id'])}</code>")
    when = _ts(event.get("event_timestamp_ms"), tz)
    if when:
        lines.append(f"🕒 {when}")
    return "\n".join(lines)


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
