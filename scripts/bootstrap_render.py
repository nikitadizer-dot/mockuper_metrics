"""Разовая настройка: проверка событий PostHog, деплой на Render, тестовый отчёт.

Запуск (ключи только через окружение, в git их не коммитим):

    export RENDER_API_KEY=rnd_...
    export POSTHOG_API_KEY=phx_...
    export POSTHOG_PROJECT_ID=498457
    export TELEGRAM_BOT_TOKEN=123:ABC...
    python scripts/bootstrap_render.py            # все шаги
    python scripts/bootstrap_render.py --skip-render   # без создания сервиса

Шаги:
  1. Читает реальные имена событий из PostHog за 30 дней и сверяет с
     дефолтами bot/config.py; для непокрытых метрик печатает, какие
     EVENTS_* переменные надо переопределить.
  2. Создаёт на Render background worker из ветки DEPLOY_BRANCH и
     прописывает env-переменные (включая ключи).
  3. Определяет chat_id через getUpdates (нужно заранее написать боту
     любое сообщение), отправляет тестовый отчёт за сегодня и вчера и
     дописывает TELEGRAM_CHAT_ID в env сервиса на Render.
"""

import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_URL = "https://github.com/nikitadizer-dot/mockuper_metrics"
DEPLOY_BRANCH = "claude/posthog-reports-bot-deploy-ne9qyx"
SERVICE_NAME = "mockuper-metrics-bot"
RENDER_API = "https://api.render.com/v1"


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Переменная окружения {name} не задана")
    return v


# ------------------------------------------------------------------ шаг 1

def check_posthog_events() -> dict[str, str]:
    """Сверяет события в PostHog с дефолтами конфига.

    Возвращает EVENTS_* переопределения, которые стоит прописать на Render
    (пустой dict — дефолты покрывают всё, что есть в данных).
    """
    from bot.config import load_config

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")  # load_config требует
    cfg = load_config()

    resp = requests.post(
        f"{cfg.posthog_host}/api/projects/{cfg.posthog_project_id}/query/",
        headers={"Authorization": f"Bearer {cfg.posthog_api_key}"},
        json={
            "query": {
                "kind": "HogQLQuery",
                "query": (
                    "SELECT event, count() FROM events "
                    "WHERE timestamp >= now() - INTERVAL 30 DAY "
                    "GROUP BY event ORDER BY count() DESC LIMIT 100"
                ),
            }
        },
        timeout=60,
    )
    resp.raise_for_status()
    real = {r[0]: int(r[1]) for r in resp.json().get("results", [])}

    print("События в PostHog за 30 дней:")
    for name, c in sorted(real.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {c}")

    metric_candidates = {
        "EVENTS_APP_OPENED": cfg.events_app_opened,
        "EVENTS_EDITOR_OPENED": cfg.events_editor_opened,
        "EVENTS_PURCHASE": cfg.events_purchase,
        "EVENTS_PAYWALL_SHOWN": cfg.events_paywall_shown,
        "EVENTS_MOCKUP_SAVED": cfg.events_mockup_saved,
        "EVENTS_AI_GENERATION": cfg.events_ai_generation,
    }
    overrides: dict[str, str] = {}
    print("\nПокрытие метрик дефолтами:")
    for var, candidates in metric_candidates.items():
        matched = [n for n in candidates if n in real]
        if matched:
            print(f"  ✅ {var}: {', '.join(matched)}")
        else:
            print(f"  ⚠️ {var}: ни один кандидат не найден в данных")
            # Простая эвристика: ищем события по ключевому слову из имени переменной.
            kw = var.replace("EVENTS_", "").lower().split("_")[0]
            guesses = [n for n in real if kw in n.lower()]
            if guesses:
                overrides[var] = ",".join(guesses)
                print(f"     похоже на: {overrides[var]}")
    return overrides


# ------------------------------------------------------------------ шаг 2

def render_headers() -> dict:
    return {
        "Authorization": f"Bearer {env('RENDER_API_KEY')}",
        "Content-Type": "application/json",
    }


def find_service() -> dict | None:
    resp = requests.get(
        f"{RENDER_API}/services",
        headers=render_headers(),
        params={"name": SERVICE_NAME, "limit": 20},
        timeout=30,
    )
    resp.raise_for_status()
    for item in resp.json():
        svc = item.get("service", item)
        if svc.get("name") == SERVICE_NAME:
            return svc
    return None


def create_render_worker(event_overrides: dict[str, str]) -> str:
    existing = find_service()
    if existing:
        print(f"Сервис уже существует: {existing['id']} — пропускаю создание")
        return existing["id"]

    owners = requests.get(
        f"{RENDER_API}/owners", headers=render_headers(), params={"limit": 10}, timeout=30
    )
    owners.raise_for_status()
    owner_id = owners.json()[0]["owner"]["id"]

    env_vars = [
        {"key": "PYTHON_VERSION", "value": "3.12.4"},
        {"key": "TELEGRAM_BOT_TOKEN", "value": env("TELEGRAM_BOT_TOKEN")},
        {"key": "POSTHOG_API_KEY", "value": env("POSTHOG_API_KEY")},
        {"key": "POSTHOG_PROJECT_ID", "value": env("POSTHOG_PROJECT_ID")},
        {"key": "POSTHOG_HOST", "value": os.environ.get("POSTHOG_HOST", "https://us.posthog.com")},
        {"key": "REPORT_TZ", "value": "Europe/Moscow"},
        {"key": "REPORT_HOUR", "value": "9"},
        {"key": "APP_NAME", "value": "Mockup Clothing"},
    ]
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if chat_id:
        env_vars.append({"key": "TELEGRAM_CHAT_ID", "value": chat_id})
    env_vars += [{"key": k, "value": v} for k, v in event_overrides.items()]

    payload = {
        "type": "background_worker",
        "name": SERVICE_NAME,
        "ownerId": owner_id,
        "repo": REPO_URL,
        "branch": DEPLOY_BRANCH,
        "autoDeploy": "yes",
        "envVars": env_vars,
        "serviceDetails": {
            "runtime": "python",
            "plan": "starter",
            "envSpecificDetails": {
                "buildCommand": "pip install -r requirements.txt",
                "startCommand": "python main.py",
            },
        },
    }
    resp = requests.post(
        f"{RENDER_API}/services", headers=render_headers(), json=payload, timeout=60
    )
    if resp.status_code >= 400:
        raise SystemExit(f"Render API {resp.status_code}: {resp.text[:1000]}")
    svc = resp.json()["service"]
    print(f"Создан background worker: {svc['id']} ({svc.get('dashboardUrl', '')})")
    return svc["id"]


def set_render_env_var(service_id: str, key: str, value: str) -> None:
    resp = requests.put(
        f"{RENDER_API}/services/{service_id}/env-vars/{key}",
        headers=render_headers(),
        json={"value": value},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise SystemExit(f"Render env-var {key}: {resp.status_code} {resp.text[:500]}")
    print(f"Render: {key} обновлён")


# ------------------------------------------------------------------ шаг 3

def detect_chat_id(token: str) -> str | None:
    """Берёт chat_id из последних сообщений боту (нужно написать боту заранее)."""
    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates", timeout=30
    )
    resp.raise_for_status()
    updates = resp.json().get("result", [])
    for upd in reversed(updates):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if chat is not None:
            return str(chat)
    return None


def send_test_report(chat_id: str) -> None:
    from bot.config import load_config
    from bot.posthog_client import PostHogClient
    from bot.reports import ReportBuilder
    from bot.telegram_client import TelegramClient

    cfg = load_config()
    builder = ReportBuilder(cfg, PostHogClient(cfg.posthog_host, cfg.posthog_api_key, cfg.posthog_project_id))
    tg = TelegramClient(cfg.telegram_token)
    tg.send_message(chat_id, f"✅ Тестовый отчёт. Ваш chat_id: {chat_id}\n\n" + builder.build("today"))
    tg.send_message(chat_id, builder.build("daily"))
    print(f"Тестовые отчёты отправлены в чат {chat_id}")


# ------------------------------------------------------------------ main

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-render", action="store_true", help="не трогать Render")
    parser.add_argument("--skip-telegram", action="store_true", help="не слать тестовый отчёт")
    args = parser.parse_args()

    overrides = check_posthog_events()
    if overrides:
        print("\nРекомендуемые переопределения EVENTS_* (будут прописаны на Render):")
        print(json.dumps(overrides, indent=2, ensure_ascii=False))

    service_id = None
    if not args.skip_render:
        service_id = create_render_worker(overrides)

    if not args.skip_telegram:
        token = env("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or detect_chat_id(token)
        if not chat_id:
            print(
                "\n⚠️ chat_id не найден: напишите боту любое сообщение и "
                "перезапустите скрипт (или задайте TELEGRAM_CHAT_ID)."
            )
        else:
            print(f"\nchat_id: {chat_id}")
            send_test_report(chat_id)
            if service_id:
                set_render_env_var(service_id, "TELEGRAM_CHAT_ID", chat_id)

    print("\nГотово.")


if __name__ == "__main__":
    main()
