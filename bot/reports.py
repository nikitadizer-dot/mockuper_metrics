"""Сбор метрик из PostHog и форматирование отчётов."""

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import rc_events
from .config import Config
from .posthog_client import PostHogClient
from .revenuecat_client import RevenueCatClient, RevenueCatError

log = logging.getLogger(__name__)

MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_date(d: date) -> str:
    return f"{d.day:02d} {MONTHS_EN[d.month - 1]} {d.year}"


def _fmt_range(start: date, end_inclusive: date) -> str:
    if start == end_inclusive:
        return _fmt_date(start)
    if start.year == end_inclusive.year:
        return f"{start.day:02d} {MONTHS_EN[start.month - 1]} – {_fmt_date(end_inclusive)}"
    return f"{_fmt_date(start)} – {_fmt_date(end_inclusive)}"


def period_bounds(kind: str, tz: ZoneInfo, now: datetime | None = None):
    """Возвращает (start_date, end_date_exclusive, заголовок, подпись даты)."""
    now = now or datetime.now(tz)
    today = now.date()
    if kind == "daily":
        start, end = today - timedelta(days=1), today
        return start, end, "Daily Report", _fmt_date(start)
    if kind == "today":
        start, end = today, today + timedelta(days=1)
        return start, end, "Daily Report (сегодня, неполный день)", _fmt_date(start)
    if kind == "weekly":
        end = today - timedelta(days=today.weekday())  # понедельник этой недели
        start = end - timedelta(days=7)
        return start, end, "Weekly Report", _fmt_range(start, end - timedelta(days=1))
    if kind == "monthly":
        first_this = today.replace(day=1)
        start = (first_this - timedelta(days=1)).replace(day=1)
        return start, first_this, "Monthly Report", _fmt_range(start, first_this - timedelta(days=1))
    raise ValueError(f"unknown period kind: {kind}")


def _utc_str(d: date, tz: ZoneInfo) -> str:
    """Полночь локальной даты -> строка UTC для HogQL."""
    dt = datetime.combine(d, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _sql_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _in_list(names: list[str]) -> str:
    return "(" + ", ".join(_sql_str(n) for n in names) + ")"


def _prop_expr(props: list[str], fallback: str = "") -> str:
    parts = [f"nullIf(toString(properties.{p}), '')" for p in props]
    return f"coalesce({', '.join(parts)}, {_sql_str(fallback)})"


def _delta(cur: float, prev: float) -> str:
    """Компактное сравнение с предыдущим периодом: 🟢 +25% (4).

    Стрелки-эмодзи не используем: 🔺 в Telegram всегда красная,
    а рост должен быть зелёным. Цвет несут 🟢/🔴, знак — сам процент.
    """
    if prev == 0:
        return " (0)" if cur else ""
    diff = (cur - prev) / prev * 100
    prev_s = f"{prev:g}"
    if round(diff) == 0:
        return f" ⚪ 0% ({prev_s})"
    mark = "🟢" if diff > 0 else "🔴"
    return f" {mark} {diff:+.0f}% ({prev_s})"


def _money(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 100 else f"${x:,.2f}"


class ReportBuilder:
    def __init__(self, cfg: Config, ph: PostHogClient, rc: RevenueCatClient | None = None):
        self.cfg = cfg
        self.ph = ph
        self.rc = rc
        self.tz = ZoneInfo(cfg.tz_name)

    def _time_filter(self, start: date, end: date) -> str:
        return (
            f"timestamp >= toDateTime('{_utc_str(start, self.tz)}', 'UTC') "
            f"AND timestamp < toDateTime('{_utc_str(end, self.tz)}', 'UTC')"
        )

    def gather(self, start: date, end: date) -> dict:
        cfg = self.cfg
        tf = self._time_filter(start, end)

        rows = self.ph.query(
            f"SELECT event, count() FROM events WHERE {tf} GROUP BY event"
        )
        counts = {r[0]: int(r[1]) for r in rows}

        def total(names: list[str]) -> int:
            return sum(counts.get(n, 0) for n in names)

        dau_rows = self.ph.query(
            f"SELECT uniq(person_id) FROM events WHERE {tf}"
        )
        dau = int(dau_rows[0][0]) if dau_rows else 0

        gexpr = _prop_expr(cfg.garment_props)
        garments = self.ph.query(
            f"SELECT {gexpr} AS g, count() AS c FROM events "
            f"WHERE {tf} AND {gexpr} != '' "
            f"GROUP BY g ORDER BY c DESC LIMIT 5"
        )

        sexpr = _prop_expr(cfg.paywall_source_props, fallback="default")
        sources = self.ph.query(
            f"SELECT {sexpr} AS s, count() AS c FROM events "
            f"WHERE {tf} AND event IN {_in_list(cfg.events_paywall_shown)} "
            f"GROUP BY s ORDER BY c DESC LIMIT 10"
        )

        # У события покупки нет источника, поэтому привязываем каждую покупку
        # к последнему пейволлу этого пользователя (ASOF JOIN); пейволлы берём
        # с запасом в 7 дней до начала периода.
        pw_from = _utc_str(start - timedelta(days=7), self.tz)
        purchase_sources = self.ph.query(
            f"SELECT if(pw.src = '', 'не определено', pw.src) AS s, count() AS c "
            f"FROM events AS p "
            f"ASOF LEFT JOIN ("
            f"SELECT person_id, timestamp, {sexpr} AS src FROM events "
            f"WHERE event IN {_in_list(cfg.events_paywall_shown)} "
            f"AND timestamp >= toDateTime('{pw_from}', 'UTC') "
            f"AND timestamp < toDateTime('{_utc_str(end, self.tz)}', 'UTC')"
            f") AS pw ON p.person_id = pw.person_id AND pw.timestamp <= p.timestamp "
            f"WHERE p.event IN {_in_list(cfg.events_purchase)} "
            f"AND p.timestamp >= toDateTime('{_utc_str(start, self.tz)}', 'UTC') "
            f"AND p.timestamp < toDateTime('{_utc_str(end, self.tz)}', 'UTC') "
            f"GROUP BY s ORDER BY c DESC LIMIT 10"
        )

        return {
            "dau": dau,
            "installs": total(cfg.events_app_installed),
            "app_opened": total(cfg.events_app_opened),
            "editor_opened": total(cfg.events_editor_opened),
            "purchases": total(cfg.events_purchase),
            "paywall_shown": total(cfg.events_paywall_shown),
            "mockups_saved": total(cfg.events_mockup_saved),
            "ai_generations": total(cfg.events_ai_generation),
            "garments": [(r[0], int(r[1])) for r in garments],
            "paywall_sources": [(r[0], int(r[1])) for r in sources],
            "purchase_sources": [(r[0], int(r[1])) for r in purchase_sources],
        }

    def revenue_lines(self, kind: str, start: date, end: date) -> list[str]:
        """Секция «Деньги» из RevenueCat. Пустой список, если RC не настроен."""
        if not self.rc:
            return []
        lines: list[str] = []
        try:
            gross = self.rc.revenue(start.isoformat(), end.isoformat(), "revenue")
            proceeds = self.rc.revenue(start.isoformat(), end.isoformat(), "proceeds")
            lines += [
                "",
                "💵 Деньги за период",
                f"• Выручка (gross): {_money(gross)}",
                f"• В карман (после Apple): {_money(proceeds)}",
            ]
        except RevenueCatError as e:
            log.error("RevenueCat revenue: %s", e)
            return ["", "💵 Деньги: ⚠️ RevenueCat недоступен"]

        # Поток подписок из вебхуков (если настроены).
        flow = rc_events.summarize(self.cfg.rc_events_file, start, end, self.tz)
        if flow:
            new_money = flow["new_revenue"]
            ren_money = flow["renewal_revenue"]
            lines += [
                "",
                "🧾 Подписки за период",
                f"• Новых: {flow['new']} — нед {flow['new_weekly']} / год {flow['new_yearly']}",
                f"• Заработано с новых: {_money(new_money)}",
                f"• Продлений: {flow['renewal']} → {_money(ren_money)}",
                f"• Итого с подписок: {_money(new_money + ren_money)}",
                f"• Отписались: {flow['cancellation']} · закончились: {flow['expiration']}",
            ]
            if flow["refund"]:
                lines.append(f"• Возвраты: {flow['refund']}")

        # Текущий снапшот проекта (не за период, а «прямо сейчас» / за 28 дней).
        try:
            ov = self.rc.overview()
            arppu = ov["mrr"] / ov["active_subscriptions"] if ov.get("active_subscriptions") else 0
            lines += [
                "",
                "📊 Сейчас",
                f"• MRR: {_money(ov.get('mrr', 0))}",
                f"• Активных подписок: {ov.get('active_subscriptions', 0)}",
            ]
            if ov.get("active_trials"):
                lines.append(f"• Активных триалов: {ov['active_trials']}")
            lines += [
                f"• ARPPU: {_money(arppu)}/мес",
                f"• Новых юзеров (28д): {ov.get('new_customers', 0)}",
            ]
        except RevenueCatError as e:
            log.error("RevenueCat overview: %s", e)
        return lines

    def build(self, kind: str) -> str:
        start, end, title, date_label = period_bounds(kind, self.tz)
        m = self.gather(start, end)

        # Предыдущий период той же длины (для "today" — вчера целиком).
        period_len = end - start
        p = self.gather(start - period_len, start)

        def conv(metrics: dict) -> float:
            if metrics["paywall_shown"] > 0:
                return metrics["purchases"] / metrics["paywall_shown"] * 100
            return 0.0

        conversion, prev_conversion = conv(m), conv(p)
        conv_delta = ""
        if prev_conversion or conversion:
            pp = conversion - prev_conversion
            if abs(pp) < 0.05:
                conv_delta = f" ⚪ ({prev_conversion:.1f}%)"
            else:
                mark = "🟢" if pp > 0 else "🔴"
                conv_delta = f" {mark} {pp:+.1f} п.п. ({prev_conversion:.1f}%)"

        def row(label: str, key: str) -> str:
            return f"• {label}: {m[key]}{_delta(m[key], p[key])}"

        users_label = "DAU" if kind in ("daily", "today") else "Пользователей"

        lines = [
            f"📱 {self.cfg.app_name} — {title}",
            f"📅 {date_label}",
            "━" * 20,
            "",
            "👥 Пользователи",
            row(users_label, "dau"),
            row("Установок", "installs"),
            row("Запусков", "app_opened"),
            row("Редактор", "editor_opened"),
            "",
            "💰 Монетизация",
            row("Покупок", "purchases"),
            *(
                [f"  ↳ {' · '.join(f'{name}: {c}' for name, c in m['purchase_sources'])}"]
                if m["purchase_sources"]
                else []
            ),
            row("Пейволлов", "paywall_shown"),
            f"• Конверсия: {conversion:.1f}%{conv_delta}",
            "",
            "🎨 Контент",
            row("Мокапов", "mockups_saved"),
            row("AI-генераций", "ai_generations"),
        ]

        if m["garments"]:
            lines += ["", "👗 Топ гарментов"]
            lines += [f"• {name}: {c}" for name, c in m["garments"]]

        if m["paywall_sources"]:
            lines += ["", "🔓 Источники пейволла"]
            lines += [f"• {name}: {c}" for name, c in m["paywall_sources"]]

        lines += self.revenue_lines(kind, start, end)

        lines += ["", "🟢 рост · 🔴 падение · в скобках — пред. период"]
        return "\n".join(lines)

    def list_events(self, days: int = 14) -> str:
        """Список реальных событий за N дней — для сверки имён (/events)."""
        rows = self.ph.query(
            f"SELECT event, count() AS c, uniq(person_id) FROM events "
            f"WHERE timestamp >= now() - INTERVAL {int(days)} DAY "
            f"GROUP BY event ORDER BY c DESC LIMIT 40"
        )
        if not rows:
            return f"За последние {days} дней событий не найдено."
        lines = [f"📋 События в PostHog за {days} дней (событие: всего / уник. польз.):", ""]
        lines += [f"• {r[0]}: {int(r[1])} / {int(r[2])}" for r in rows]
        return "\n".join(lines)

    def list_props(self, event: str, days: int = 14) -> str:
        """Топ свойств события — для сверки имён свойств (/props <event>)."""
        rows = self.ph.query(
            f"SELECT arrayJoin(JSONExtractKeys(properties)) AS k, count() AS c FROM events "
            f"WHERE event = {_sql_str(event)} AND timestamp >= now() - INTERVAL {int(days)} DAY "
            f"GROUP BY k ORDER BY c DESC LIMIT 40"
        )
        if not rows:
            return f"Событие «{event}» за последние {days} дней не найдено."
        lines = [f"🔑 Свойства события «{event}» за {days} дней:", ""]
        lines += [f"• {r[0]}: {int(r[1])}" for r in rows if not str(r[0]).startswith("$")]
        return "\n".join(lines)
