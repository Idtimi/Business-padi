"""Telegram Bot Webhook endpoints for Business Padi."""

from datetime import datetime, timezone
import logging
from typing import Any
import httpx

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.base import IncomingMessage
from app.channels.telegram import TelegramChannel
from app.config import settings
from app.db import SessionLocal
from app.pipeline.asr import get_asr
from app.pipeline.handler import handle_incoming

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/webhook/telegram")
def telegram_health():
    return PlainTextResponse("Telegram Webhook Ready", status_code=200)


@router.get("/telegram/set_webhook")
async def set_telegram_webhook(url: str | None = None):
    """Register or update Telegram webhook URL."""
    token = getattr(settings, "telegram_bot_token", "")
    if not token:
        return {"error": "telegram_bot_token not configured"}

    target_url = url or f"{settings.base_url.rstrip('/')}/webhook/telegram"
    api_url = f"https://api.telegram.org/bot{token}/setWebhook?url={target_url}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(api_url)
        return res.json()


@router.get("/telegram/get_webhook_info")
async def get_telegram_webhook_info():
    """Check Telegram webhook status."""
    token = getattr(settings, "telegram_bot_token", "")
    if not token:
        return {"error": "telegram_bot_token not configured"}

    api_url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(api_url)
        return res.json()


@router.post("/webhook/telegram")
async def receive_telegram_webhook(request: Request):
    """Receive incoming webhook updates from Telegram Bot API."""
    try:
        update: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid json"}, status_code=400)

    log.info("Received Telegram update: %s", str(update)[:180])

    channel = TelegramChannel()
    msg: IncomingMessage | None = None

    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb.get("id")
        user_info = cb.get("from", {})
        chat_id = str(user_info.get("id"))
        cb_data = cb.get("data", "")
        msg = IncomingMessage(
            channel="telegram",
            user_ref=chat_id,
            message_id=f"tg_cb_{cb_id}",
            kind="button",
            text=None,
            media_ref=None,
            button_id=cb_data,
            timestamp=datetime.now(timezone.utc),
        )
        if cb_id:
            await channel.answer_callback_query(cb_id)

    elif "message" in update:
        m = update["message"]
        chat = m.get("chat", {})
        chat_id = str(chat.get("id"))
        user_info = m.get("from", {})
        msg_id = str(m.get("message_id"))

        if "voice" in m:
            file_id = m["voice"].get("file_id")
            msg = IncomingMessage(
                channel="telegram",
                user_ref=chat_id,
                message_id=f"tg_{msg_id}",
                kind="voice",
                text=None,
                media_ref=file_id,
                button_id=None,
                timestamp=datetime.now(timezone.utc),
            )
        elif "audio" in m:
            file_id = m["audio"].get("file_id")
            msg = IncomingMessage(
                channel="telegram",
                user_ref=chat_id,
                message_id=f"tg_{msg_id}",
                kind="voice",
                text=None,
                media_ref=file_id,
                button_id=None,
                timestamp=datetime.now(timezone.utc),
            )
        elif "photo" in m and isinstance(m["photo"], list) and len(m["photo"]) > 0:
            best_photo = m["photo"][-1]
            file_id = best_photo.get("file_id")
            caption = m.get("caption")
            msg = IncomingMessage(
                channel="telegram",
                user_ref=chat_id,
                message_id=f"tg_{msg_id}",
                kind="image",
                text=caption,
                media_ref=file_id,
                button_id=None,
                timestamp=datetime.now(timezone.utc),
            )
        elif "document" in m and (
            (m["document"].get("mime_type") or "").startswith("image/")
            or (m["document"].get("file_name") or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ):
            file_id = m["document"].get("file_id")
            caption = m.get("caption")
            msg = IncomingMessage(
                channel="telegram",
                user_ref=chat_id,
                message_id=f"tg_{msg_id}",
                kind="image",
                text=caption,
                media_ref=file_id,
                button_id=None,
                timestamp=datetime.now(timezone.utc),
            )
        elif "text" in m:
            raw_text = m.get("text", "").strip()
            msg = IncomingMessage(
                channel="telegram",
                user_ref=chat_id,
                message_id=f"tg_{msg_id}",
                kind="text",
                text=raw_text,
                media_ref=None,
                button_id=None,
                timestamp=datetime.now(timezone.utc),
            )

    if msg is not None:
        try:
            with SessionLocal() as session:
                asr_engine = get_asr()
                await handle_incoming(channel, msg, session, asr=asr_engine)
        except Exception as e:
            log.exception("Error processing Telegram message: %s", e)

    return JSONResponse({"status": "ok"}, status_code=200)
