#!/usr/bin/env python3
"""Shared Telegram / Discord senders — used by main.py (whale/watch/consensus/
accumulation alerts) and resolve_markets.py (market-resolution alerts), so
resolve_markets.py doesn't need to import all of main.py just to send a
message."""

import logging

import requests

logger = logging.getLogger(__name__)


def send_telegram_alert(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not bot_token or not chat_id:
        logger.debug("Telegram not configured — skipping alert.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        logger.warning(f"Telegram HTTP error: {e} — response: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"Failed to send Telegram alert: {e}")
        return False


def send_discord_alert(webhook_url: str, message: str) -> bool:
    """Send a message via Discord Webhook API. Returns True on success."""
    if not webhook_url:
        logger.debug("Discord not configured — skipping alert.")
        return False

    payload = {
        "content": message,
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        logger.warning(f"Discord HTTP error: {e}")
        return False
    except Exception as e:
        logger.warning(f"Failed to send Discord alert: {e}")
        return False
