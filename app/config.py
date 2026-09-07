from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

OFFICIAL_FL_RSS_URL = "https://www.fl.ru/rss/?category=41"
OFFICIAL_FL_RSS_FEEDS: tuple[tuple[str, str], ...] = (
    ("Автоматизация бизнеса", OFFICIAL_FL_RSS_URL),
    ("Python", "https://www.fl.ru/rss/?subcategory=798&category=5"),
    ("Разработка чат-ботов", "https://www.fl.ru/rss/?subcategory=279&category=5"),
    ("Интеграция по API", "https://www.fl.ru/rss/?subcategory=585&category=5"),
    ("n8n", "https://www.fl.ru/rss/?subcategory=703&category=5"),
    ("Парсинг данных", "https://www.fl.ru/rss/?subcategory=280&category=5"),
    ("AI — искусственный интеллект", "https://www.fl.ru/rss/?category=31"),
)
OFFICIAL_FL_RSS_URLS = tuple(url for _, url in OFFICIAL_FL_RSS_FEEDS)
OFFICIAL_FL_RSS_NAMES = {url: name for name, url in OFFICIAL_FL_RSS_FEEDS}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./freelance_hunter.db"
    log_level: str = "INFO"
    poll_interval: int = 300
    min_notification_score: float = 70
    min_budget_rub: float = 3000
    fl_rss_urls: tuple[str, ...] = ()
    ai_provider: str = "mock"
    ai_api_key: str | None = None
    polza_api_key: str | None = None
    polza_model: str = "qwen/qwen3.8-flash"
    polza_base_url: str = "https://polza.ai/api/v1"
    telegram_bot_token: str | None = None
    telegram_allowed_chat_id: str | None = None
    telegram_webhook_secret: str | None = None
    run_scheduler: bool = False
    hunter_mode: str = "manual"
    poll_interval_minutes: int = 10
    telegram_score_threshold: float = 50

    @classmethod
    def from_env(cls) -> "Settings":
        # Lightweight .env support without adding a runtime dependency. Environment wins.
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        def boolean(name: str, default: bool) -> bool:
            return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes"}

        urls = tuple(item.strip() for item in os.getenv("FL_RSS_URLS", "").split(",") if item.strip())
        settings = cls(
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            log_level=os.getenv("LOG_LEVEL", cls.log_level).upper(),
            poll_interval=int(os.getenv("POLL_INTERVAL", str(cls.poll_interval))),
            min_notification_score=float(os.getenv("MIN_NOTIFICATION_SCORE", str(cls.min_notification_score))),
            min_budget_rub=float(os.getenv("MIN_BUDGET_RUB", str(cls.min_budget_rub))),
            fl_rss_urls=urls,
            ai_provider=os.getenv("AI_PROVIDER", cls.ai_provider).strip().lower(),
            ai_api_key=os.getenv("AI_API_KEY") or None,
            polza_api_key=os.getenv("POLZA_API_KEY") or None,
            polza_model=os.getenv("POLZA_MODEL", cls.polza_model).strip(),
            polza_base_url=os.getenv("POLZA_BASE_URL", cls.polza_base_url).rstrip("/"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_allowed_chat_id=os.getenv("TELEGRAM_ALLOWED_CHAT_ID") or None,
            telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET") or None,
            run_scheduler=boolean("RUN_SCHEDULER", False),
            hunter_mode=os.getenv("HUNTER_MODE", cls.hunter_mode).strip().lower(),
            poll_interval_minutes=int(os.getenv("POLL_INTERVAL_MINUTES", str(cls.poll_interval_minutes))),
            telegram_score_threshold=float(os.getenv("TELEGRAM_SCORE_THRESHOLD", str(cls.telegram_score_threshold))),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        errors: list[str] = []
        if self.poll_interval < 30:
            errors.append("POLL_INTERVAL must be at least 30 seconds")
        if not 0 <= self.min_notification_score <= 100:
            errors.append("MIN_NOTIFICATION_SCORE must be between 0 and 100")
        if self.min_budget_rub < 0:
            errors.append("MIN_BUDGET_RUB must not be negative")
        if self.ai_provider == "polza" and not self.polza_api_key:
            errors.append("POLZA_API_KEY is required when AI_PROVIDER=polza")
        if self.ai_provider not in {"mock", "polza"} and not self.ai_api_key:
            errors.append("AI_API_KEY is required when AI_PROVIDER is not mock")
        if bool(self.telegram_bot_token) != bool(self.telegram_allowed_chat_id):
            errors.append("TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID must be set together")
        if self.run_scheduler and not self.fl_rss_urls:
            errors.append("FL_RSS_URLS is required when RUN_SCHEDULER=true")
        if self.hunter_mode not in {"manual", "live"}:
            errors.append("HUNTER_MODE must be manual or live")
        if self.poll_interval_minutes < 1:
            errors.append("POLL_INTERVAL_MINUTES must be at least 1")
        if not 0 <= self.telegram_score_threshold <= 100:
            errors.append("TELEGRAM_SCORE_THRESHOLD must be between 0 and 100")
        if len(set(self.fl_rss_urls)) != len(self.fl_rss_urls):
            errors.append("FL_RSS_URLS must not contain duplicate feeds")
        if any(url not in OFFICIAL_FL_RSS_URLS for url in self.fl_rss_urls):
            errors.append("FL_RSS_URLS contains a source outside the approved official FL.ru RSS feeds")
        if self.hunter_mode == "live":
            if self.ai_provider != "polza":
                errors.append("AI_PROVIDER=polza is required when HUNTER_MODE=live")
            if len(self.fl_rss_urls) != len(OFFICIAL_FL_RSS_URLS) or set(self.fl_rss_urls) != set(OFFICIAL_FL_RSS_URLS):
                errors.append("All approved official FL.ru RSS feeds are required when HUNTER_MODE=live")
            if not self.telegram_bot_token or not self.telegram_allowed_chat_id:
                errors.append("Telegram credentials are required when HUNTER_MODE=live")
        if self.telegram_webhook_secret:
            allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
            if not 1 <= len(self.telegram_webhook_secret) <= 256 or any(char not in allowed for char in self.telegram_webhook_secret):
                errors.append("TELEGRAM_WEBHOOK_SECRET must be 1-256 characters using only A-Z, a-z, 0-9, _ and -")
        if errors:
            raise ValueError("Invalid configuration: " + "; ".join(errors))
