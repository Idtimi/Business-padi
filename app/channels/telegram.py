"""Telegram Bot channel adapter for Business Padi."""

from dataclasses import dataclass
import logging
from typing import Any
import httpx

from app.channels.base import Channel
from app.config import settings

log = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramChannel:
    """Channel adapter for Telegram Bot API."""

    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None):
        self.token = token or getattr(settings, "telegram_bot_token", "")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)

    @property
    def _base_url(self) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.token}"

    async def send_text(self, user_ref: str, text: str) -> dict[str, Any]:
        """Send a plain text message to user_ref (chat_id)."""
        url = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id": user_ref,
            "text": text,
        }
        resp = await self._client.post(url, json=payload)
        if resp.status_code >= 400:
            log.error("Telegram send_text error [%s]: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    async def send_buttons(
        self,
        user_ref: str,
        text: str,
        buttons: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """Send message with inline keyboard buttons."""
        url = f"{self._base_url}/sendMessage"
        inline_keyboard = [
            [{"text": title, "callback_data": action_id}]
            for action_id, title in buttons
        ]
        payload = {
            "chat_id": user_ref,
            "text": text,
            "reply_markup": {"inline_keyboard": inline_keyboard},
        }
        resp = await self._client.post(url, json=payload)
        if resp.status_code >= 400:
            log.error("Telegram send_buttons error [%s]: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    async def send_document(
        self,
        user_ref: str,
        media_id: str,
        filename: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """Send a document or download link to chat."""
        import os
        if os.path.exists(media_id):
            url = f"{self._base_url}/sendDocument"
            try:
                with open(media_id, "rb") as f:
                    content = f.read()
                files = {"document": (filename, content, "application/pdf")}
                data = {"chat_id": user_ref, "caption": caption or filename}
                resp = await self._client.post(url, data=data, files=files)
                if resp.status_code == 200:
                    return resp.json()
                log.warning("Telegram sendDocument file upload error [%s]: %s", resp.status_code, resp.text)
            except Exception as e:
                log.warning("Telegram sendDocument local file upload failed: %s", e)

        if media_id.startswith("http://") or media_id.startswith("https://"):
            url = f"{self._base_url}/sendDocument"
            payload = {
                "chat_id": user_ref,
                "document": media_id,
                "caption": caption or filename,
            }
            try:
                resp = await self._client.post(url, json=payload)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                log.warning("Telegram sendDocument failed, falling back to link: %s", e)
            return await self.send_text(user_ref, f"{caption}\nDownload link: {media_id}".strip())
        else:
            return await self.send_text(user_ref, f"{caption}\nDocument: {filename}".strip())

    async def send_image(
        self,
        user_ref: str,
        media_id: str,
        caption: str = "",
    ) -> dict[str, Any]:
        return await self.send_document(user_ref, media_id, "image.jpg", caption)

    async def send_template(
        self,
        user_ref: str,
        template_name: str,
        language_code: str = "en",
        components: list[dict] | None = None,
    ) -> dict[str, Any]:
        return await self.send_text(user_ref, f"Template: {template_name}")

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge a button tap so the client removes the loading spinner."""
        try:
            url = f"{self._base_url}/answerCallbackQuery"
            await self._client.post(url, json={"callback_query_id": callback_query_id, "text": text})
        except Exception as e:
            log.debug("Failed to answer callback query: %s", e)

    async def download_media(self, media_ref: str) -> tuple[bytes, str]:
        """Download voice note or media using Telegram file_id or direct URL."""
        if not media_ref:
            raise ValueError("No media_ref provided for Telegram download")
        if media_ref.startswith("http://") or media_ref.startswith("https://"):
            resp = await self._client.get(media_ref, follow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "audio/ogg")
            return resp.content, content_type

        # media_ref is Telegram file_id
        get_file_url = f"{self._base_url}/getFile?file_id={media_ref}"
        res = await self._client.get(get_file_url)
        res.raise_for_status()
        file_data = res.json()
        if not file_data.get("ok"):
            desc = file_data.get("description", "Unknown Telegram getFile error")
            raise ValueError(f"Telegram getFile error: {desc}")
        file_path = file_data.get("result", {}).get("file_path")
        if not file_path:
            raise ValueError(f"Could not retrieve file_path for Telegram file_id {media_ref}")

        dl_url = f"{TELEGRAM_API_BASE}/file/bot{self.token}/{file_path}"
        dl_resp = await self._client.get(dl_url)
        dl_resp.raise_for_status()
        content_type = "audio/ogg" if file_path.endswith((".oga", ".ogg", ".opus")) else "audio/mpeg"
        return dl_resp.content, content_type

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
