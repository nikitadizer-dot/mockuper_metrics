"""Сбор метрик из PostHog и форматирование отчётов."""

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .posthog_client import PostHogClient

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


class ReportBuilder:
    def __init__(self, cfg: Config, ph: PostHogClient):
        self.cfg = cfg
        self.ph = ph
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

        return {
            "dau": dau,
            "app_opened": total(cfg.events_app_opened),
            "editor_opened": total(cfg.events_editor_opened),
            "purchases": total(cfg.events_purchase),
            "paywall_shown": total(cfg.events_paywall_shown),
            "mockups_saved": total(cfg.events_mockup_saved),
            "ai_generations": total(cfg.events_ai_generation),
            "garments": [(r[0], int(r[1])) for r in garments],
            "paywall_sources": [(r[0], int(r[1])) for r in sources],
        }

    def build(self, kind: str) -> str:
        start, end, title, date_label = period_bounds(kind, self.tz)
        m = self.gather(start, end)

        conversion = 0.0
        if m["paywall_shown"] > 0:
            conversion = m["purchases"] / m["paywall_shown"] * 100

        users_label = "DAU" if kind in ("daily", "today") else "Уникальных пользователей"

        lines = [
            f"📱 {self.cfg.app_name} — {title}",
            f"📅 {date_label}",
            "━" * 20,
            "",
            "👥 Пользователи",
            f"  • {users_label}: {m['dau']}",
            f"  • Запусков приложения: {m['app_opened']}",
            f"  • Открыли редактор: {m['editor_opened']}",
            "",
            "💰 Монетизация",
            f"  • Покупок: {m['purchases']}",
            f"  • Пейволл показан: {m['paywall_shown']}",
            f"  • Конверсия: {conversion:.1f}%",
            "",
            "🎨 Контент",
            f"  • Мокапов сохранено: {m['mockups_saved']}",
            f"  • AI-генераций: {m['ai_generations']}",
        ]

        if m["garments"]:
            lines += ["", "👗 Топ гарментов"]
            lines += [f"  • {name}: {c}" for name, c in m["garments"]]

        if m["paywall_sources"]:
            lines += ["", "🔓 Источники пейволла"]
            lines += [f"  • {name}: {c}" for name, c in m["paywall_sources"]]

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
