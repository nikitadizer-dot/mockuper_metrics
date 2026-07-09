"""Конфигурация бота: всё берётся из переменных окружения."""

import os
from dataclasses import dataclass


def _list(env_name: str, default: str) -> list[str]:
    raw = os.environ.get(env_name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Config:
    telegram_token: str
    chat_id: str | None

    posthog_host: str
    posthog_api_key: str
    posthog_project_id: str

    app_name: str
    tz_name: str
    report_hour: int
    state_file: str
    enable_polling: bool

    # Имена событий в PostHog. Для каждой метрики можно указать несколько
    # кандидатов через запятую — считаются все, которые реально есть в данных.
    events_app_opened: list[str]
    events_editor_opened: list[str]
    events_purchase: list[str]
    events_paywall_shown: list[str]
    events_mockup_saved: list[str]
    events_ai_generation: list[str]

    # Имена свойств, где лежит гармент и источник пейволла.
    garment_props: list[str]
    paywall_source_props: list[str]


def load_config() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    api_key = os.environ.get("POSTHOG_API_KEY", "").strip()
    project_id = os.environ.get("POSTHOG_PROJECT_ID", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан")
    if not api_key:
        raise SystemExit("POSTHOG_API_KEY не задан (personal API key, phx_...)")
    if not project_id:
        raise SystemExit("POSTHOG_PROJECT_ID не задан")

    return Config(
        telegram_token=token,
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None,
        posthog_host=os.environ.get("POSTHOG_HOST", "https://us.posthog.com").rstrip("/"),
        posthog_api_key=api_key,
        posthog_project_id=project_id,
        app_name=os.environ.get("APP_NAME", "Mockup Clothing"),
        tz_name=os.environ.get("REPORT_TZ", "Europe/Moscow"),
        report_hour=int(os.environ.get("REPORT_HOUR", "9")),
        state_file=os.environ.get("STATE_FILE", "state.json"),
        enable_polling=os.environ.get("ENABLE_POLLING", "1") not in ("0", "false", "no"),
        events_app_opened=_list(
            "EVENTS_APP_OPENED",
            "Application Opened,app_opened,app_launched,app_launch,app_open",
        ),
        events_editor_opened=_list(
            "EVENTS_EDITOR_OPENED",
            "editor_opened,editor_open,open_editor,editor_shown",
        ),
        events_purchase=_list(
            "EVENTS_PURCHASE",
            "purchase_completed,purchase_success,purchase,subscription_purchased,subscription_started",
        ),
        events_paywall_shown=_list(
            "EVENTS_PAYWALL_SHOWN",
            "paywall_shown,paywall_opened,paywall_viewed,paywall_show",
        ),
        events_mockup_saved=_list(
            "EVENTS_MOCKUP_SAVED",
            "mockup_saved,mockup_save,save_mockup,mockup_exported,mockup_downloaded",
        ),
        events_ai_generation=_list(
            "EVENTS_AI_GENERATION",
            "ai_generation,ai_generation_completed,generation_completed,ai_generate,ai_generation_started",
        ),
        garment_props=_list(
            "GARMENT_PROPS",
            "garment,garment_name,garment_type,garment_title",
        ),
        paywall_source_props=_list(
            "PAYWALL_SOURCE_PROPS",
            "source,paywall_source,placement",
        ),
    )
