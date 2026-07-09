"""PostHog → Telegram бот с ежедневными/еженедельными/ежемесячными отчётами.

Режимы:
  python main.py                 — воркер: планировщик + команды в Telegram
  python main.py --once daily    — отправить один отчёт и выйти (для cron)
                 --once weekly | monthly | today
"""

import argparse
import json
import logging
import os
import threading
import time as time_mod
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from bot.config import Config, load_config
from bot.posthog_client import PostHogClient, PostHogError
from bot.reports import ReportBuilder
from bot.telegram_client import ConflictError, TelegramClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("main")

HELP_TEXT = (
    "Команды:\n"
    "/report — отчёт за вчера\n"
    "/today — отчёт за сегодня (неполный день)\n"
    "/week — отчёт за прошлую неделю\n"
    "/month — отчёт за прошлый месяц\n"
    "/events — какие события реально приходят в PostHog (за 14 дней)\n"
    "/props <event> — свойства события (для настройки отчёта)\n"
    "/chatid — показать ID этого чата\n"
    "/help — эта справка"
)


# ---------------------------------------------------------------- state

def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except OSError:
        log.exception("не удалось сохранить состояние в %s", path)


# ---------------------------------------------------------------- reports

def send_report(cfg: Config, tg: TelegramClient, builder: ReportBuilder, kind: str) -> None:
    if not cfg.chat_id:
        log.warning("TELEGRAM_CHAT_ID не задан — отчёт %s не отправлен", kind)
        return
    try:
        text = builder.build(kind)
    except PostHogError as e:
        log.error("ошибка PostHog: %s", e)
        text = f"⚠️ Не удалось получить данные из PostHog для отчёта ({kind}):\n{e}"
    tg.send_message(cfg.chat_id, text)
    log.info("отчёт %s отправлен в чат %s", kind, cfg.chat_id)


def scheduler_loop(cfg: Config, tg: TelegramClient, builder: ReportBuilder) -> None:
    tz = ZoneInfo(cfg.tz_name)
    state = load_state(cfg.state_file)
    log.info(
        "планировщик запущен: ежедневно в %02d:00 (%s), weekly по понедельникам, monthly 1-го числа",
        cfg.report_hour,
        cfg.tz_name,
    )
    while True:
        now = datetime.now(tz)
        today = now.date()
        if now.hour >= cfg.report_hour:
            day_key = today.isoformat()
            if state.get("daily") != day_key:
                send_report(cfg, tg, builder, "daily")
                state["daily"] = day_key
                save_state(cfg.state_file, state)

            iso = today.isocalendar()
            week_key = f"{iso.year}-W{iso.week:02d}"
            if today.weekday() == 0 and state.get("weekly") != week_key:
                send_report(cfg, tg, builder, "weekly")
                state["weekly"] = week_key
                save_state(cfg.state_file, state)

            month_key = f"{today.year}-{today.month:02d}"
            if today.day == 1 and state.get("monthly") != month_key:
                send_report(cfg, tg, builder, "monthly")
                state["monthly"] = month_key
                save_state(cfg.state_file, state)
        time_mod.sleep(30)


# ---------------------------------------------------------------- telegram commands

def handle_command(
    cfg: Config, tg: TelegramClient, builder: ReportBuilder, chat_id: int, text: str
) -> None:
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower().strip()
    arg = arg.strip()

    if cmd == "/chatid":
        tg.send_message(chat_id, f"ID этого чата: {chat_id}")
        return

    # Если чат для отчётов задан — остальные команды работают только в нём.
    if cfg.chat_id and str(chat_id) != str(cfg.chat_id):
        return

    try:
        if cmd in ("/start", "/help"):
            tg.send_message(chat_id, HELP_TEXT)
        elif cmd in ("/report", "/day", "/daily"):
            tg.send_message(chat_id, builder.build("daily"))
        elif cmd == "/today":
            tg.send_message(chat_id, builder.build("today"))
        elif cmd in ("/week", "/weekly"):
            tg.send_message(chat_id, builder.build("weekly"))
        elif cmd in ("/month", "/monthly"):
            tg.send_message(chat_id, builder.build("monthly"))
        elif cmd == "/events":
            tg.send_message(chat_id, builder.list_events())
        elif cmd == "/props":
            if not arg:
                tg.send_message(chat_id, "Использование: /props <имя события>")
            else:
                tg.send_message(chat_id, builder.list_props(arg))
    except PostHogError as e:
        tg.send_message(chat_id, f"⚠️ Ошибка PostHog:\n{e}")
    except Exception:
        log.exception("ошибка обработки команды %s", cmd)
        tg.send_message(chat_id, "⚠️ Внутренняя ошибка, подробности в логах.")


def polling_loop(cfg: Config, tg: TelegramClient, builder: ReportBuilder) -> None:
    offset: int | None = None
    log.info("long polling запущен (команды /report, /week, /month, /events...)")
    while True:
        try:
            updates = tg.get_updates(offset)
        except ConflictError as e:
            # Вебхук или второй инстанс уже читает апдейты — не мешаем,
            # рассылка по расписанию продолжает работать.
            log.warning("getUpdates conflict (409), пауза 5 минут: %s", e)
            time_mod.sleep(300)
            continue
        except Exception as e:
            log.warning("getUpdates error: %s", e)
            time_mod.sleep(10)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            text = msg.get("text") or ""
            chat = (msg.get("chat") or {}).get("id")
            if chat is not None and text.startswith("/"):
                try:
                    handle_command(cfg, tg, builder, chat, text)
                except Exception:
                    log.exception("сбой обработчика команд")


# ---------------------------------------------------------------- health server

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("health-сервер слушает порт %d", port)


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        choices=["daily", "weekly", "monthly", "today"],
        help="отправить один отчёт и выйти (для запуска из cron)",
    )
    args = parser.parse_args()

    cfg = load_config()
    tg = TelegramClient(cfg.telegram_token)
    ph = PostHogClient(cfg.posthog_host, cfg.posthog_api_key, cfg.posthog_project_id)
    builder = ReportBuilder(cfg, ph)

    if args.once:
        send_report(cfg, tg, builder, args.once)
        return

    port = os.environ.get("PORT")
    if port:
        start_health_server(int(port))

    if cfg.enable_polling:
        threading.Thread(
            target=polling_loop, args=(cfg, tg, builder), daemon=True
        ).start()

    if not cfg.chat_id:
        log.warning(
            "TELEGRAM_CHAT_ID не задан: отчёты по расписанию отправляться не будут. "
            "Напишите боту /chatid, чтобы узнать ID чата."
        )

    scheduler_loop(cfg, tg, builder)


if __name__ == "__main__":
    main()
