"""Explicit Telegram deployment checks. This module never calls Polza."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from urllib.parse import urlsplit

import httpx

from app.analysis_version import ANALYZER_VERSION
from app.config import Settings
from app.db import JobRepository
from app.scoring import ScoringEngine
from app.telegram import TelegramClient


class TelegramAdminClient:
    def __init__(self, token: str):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self._token = token

    async def request(self, method: str, payload: dict | None = None) -> dict:
        endpoint = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(endpoint, json=payload or {})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Telegram transport failed ({exc.__class__.__name__})") from None
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError(f"Telegram returned HTTP {response.status_code} with invalid JSON") from None
        if response.status_code >= 400 or not body.get("ok"):
            description = str(body.get("description") or "API error")[:200]
            raise RuntimeError(f"Telegram API failed: HTTP {response.status_code}; {description}")
        return body


def _settings_and_admin() -> tuple[Settings, TelegramAdminClient]:
    settings = Settings.from_env()
    return settings, TelegramAdminClient(settings.telegram_bot_token or "")


async def get_me() -> None:
    _, admin = _settings_and_admin()
    result = (await admin.request("getMe"))["result"]
    print(json.dumps({
        "ok": True,
        "bot_id": result.get("id"),
        "username": result.get("username"),
        "can_join_groups": result.get("can_join_groups"),
    }, ensure_ascii=False))


async def discover_chat() -> None:
    _, admin = _settings_and_admin()
    updates = (await admin.request("getUpdates", {"limit": 20, "timeout": 0}))["result"]
    chats: dict[str, dict[str, object]] = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            chats[str(chat["id"])] = {"chat_id": str(chat["id"]), "type": chat.get("type")}
    print(json.dumps({"ok": True, "candidate_chats": list(chats.values())}, ensure_ascii=False))


async def set_webhook(url: str) -> None:
    settings, admin = _settings_and_admin()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path != "/telegram/webhook":
        raise ValueError("Webhook URL must be https://<host>/telegram/webhook")
    if not settings.telegram_webhook_secret:
        raise ValueError("TELEGRAM_WEBHOOK_SECRET is required")
    await admin.request("setWebhook", {
        "url": url,
        "secret_token": settings.telegram_webhook_secret,
        "allowed_updates": ["callback_query"],
        "drop_pending_updates": False,
        "max_connections": 4,
    })
    print(json.dumps({"ok": True, "webhook_url": url}, ensure_ascii=False))


async def webhook_info() -> None:
    _, admin = _settings_and_admin()
    result = (await admin.request("getWebhookInfo"))["result"]
    print(json.dumps({
        "ok": True,
        "url": result.get("url"),
        "pending_update_count": result.get("pending_update_count"),
        "last_error_date": result.get("last_error_date"),
        "last_error_message": str(result.get("last_error_message") or "")[:200] or None,
        "allowed_updates": result.get("allowed_updates"),
    }, ensure_ascii=False))


async def smoke_card(job_id: str | None) -> None:
    settings = Settings.from_env()
    if not settings.telegram_bot_token or not settings.telegram_allowed_chat_id:
        raise ValueError("Telegram token and allowed chat ID are required")
    repository = JobRepository(settings.database_url)
    repository.initialize()
    jobs = [job for job in repository.list_all() if job.analysis_analyzer_version == ANALYZER_VERSION]
    if len(jobs) != 19:
        raise ValueError(f"Expected 19 analyzer_v1 jobs, found {len(jobs)}")
    if job_id:
        candidates = [job for job in jobs if job.id == job_id]
        if len(candidates) != 1:
            raise ValueError("Requested analyzer_v1 job was not found")
    else:
        candidates = sorted(jobs, key=lambda job: -(job.final_score or 0))
    selected = next(
        (job for job in candidates if repository.notification_status(job.id, ANALYZER_VERSION) is None),
        None,
    )
    if selected is None:
        raise ValueError("No unnotified analyzer_v1 job is available for an idempotent smoke card")
    if not repository.reserve_notification(selected.id, ANALYZER_VERSION):
        raise RuntimeError("Notification was already reserved")
    telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_allowed_chat_id)
    sent = await telegram.notify(selected, ScoringEngine())
    repository.mark_notification(
        selected.id, ANALYZER_VERSION, "sent" if sent else "failed",
        None if sent else "Telegram smoke card returned false",
    )
    if not sent:
        raise RuntimeError("Telegram smoke card failed; no retry was attempted")
    print(json.dumps({
        "ok": True, "job_id": selected.id, "title": selected.title,
        "buttons": ["interested", "rejected", "draft_requested"],
        "polza_calls": 0,
    }, ensure_ascii=False))


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Safe Telegram deployment checks; never invokes Polza.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("get-me")
    subparsers.add_parser("discover-chat")
    webhook_parser = subparsers.add_parser("set-webhook")
    webhook_parser.add_argument("--url", required=True)
    subparsers.add_parser("webhook-info")
    smoke_parser = subparsers.add_parser("smoke-card")
    smoke_parser.add_argument("--job-id")
    args = parser.parse_args()
    if args.command == "get-me":
        await get_me()
    elif args.command == "discover-chat":
        await discover_chat()
    elif args.command == "set-webhook":
        await set_webhook(args.url)
    elif args.command == "webhook-info":
        await webhook_info()
    elif args.command == "smoke-card":
        await smoke_card(args.job_id)


if __name__ == "__main__":
    asyncio.run(main())
