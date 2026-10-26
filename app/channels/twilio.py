"""Twilio WhatsApp Sandbox channel adapter.

Provides TwilioChannel conforming to app.channels.base.Channel.
"""

import logging
from typing import Any
import httpx

from app.config import settings

log = logging.getLogger(__name__)

TWILIO_BASE = "https://api.twilio.com/2010-04-01"


class TwilioChannel:
    """Send messages and download media via the Twilio WhatsApp API."""

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.account_sid = account_sid or settings.twilio_account_sid
        self.auth_token = auth_token or settings.twilio_auth_token
        self.from_number = from_number or settings.twilio_whatsapp_number or "whatsapp:+14155238886"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30)

    @property
    def _messages_url(self) -> str:
        return f"{TWILIO_BASE}/Accounts/{self.account_sid}/Messages.json"

    def _format_recipient(self, user_ref: str) -> str:
        clean = user_ref.strip().replace(" ", "").replace("-", "")
        if clean.startswith("whatsapp:"):
            return clean
        if clean.startswith("+"):
            return f"whatsapp:{clean}"
        return f"whatsapp:+{clean}"

    async def _post(self, data: dict[str, Any]) -> dict[str, Any]:
        """Send message via Twilio REST API."""
        if settings.demo_mode or not self.account_sid or not self.auth_token:
            log.info("TwilioChannel mock/demo send: %s", data)
            return {"sid": "SM_demo", "status": "queued"}

        resp = await self._client.post(
            self._messages_url,
            auth=(self.account_sid, self.auth_token),
            data=data,
        )
        if resp.status_code >= 400:
            log.error("Twilio send failed (%s): %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    async def send_text(self, user_ref: str, text: str) -> dict[str, Any]:
        data = {
            "From": self.from_number,
            "To": self._format_recipient(user_ref),
            "Body": text,
        }
        return await self._post(data)

    async def send_buttons(
        self,
        user_ref: str,
        text: str,
        buttons: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """Twilio Sandbox renders buttons as numbered options."""
        options = "\n".join(f"[{idx + 1}] {title}" for idx, (_, title) in enumerate(buttons))
        body = f"{text}\n\n{options}\n(Reply with the button name or number)"
        return await self.send_text(user_ref, body)

    async def send_document(
        self,
        user_ref: str,
        media_id: str,
        filename: str,
        caption: str = "",
    ) -> dict[str, Any]:
        body_text = caption or f"Document: {filename}"
        data: dict[str, Any] = {
            "From": self.from_number,
            "To": self._format_recipient(user_ref),
            "Body": body_text,
        }
        # If media_id is an HTTP URL, attach it as MediaUrl
        if media_id.startswith("http://") or media_id.startswith("https://"):
            data["MediaUrl"] = media_id
        return await self._post(data)

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

    async def download_media(self, media_url: str) -> tuple[bytes, str]:
        """Download media file directly from Twilio MediaUrl."""
        auth = (self.account_sid, self.auth_token) if self.account_sid and self.auth_token else None
        resp = await self._client.get(media_url, auth=auth, follow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "audio/ogg")
        return resp.content, content_type

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
