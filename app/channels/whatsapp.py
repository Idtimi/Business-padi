"""WhatsApp Cloud API channel adapter.

Provides:
- verify_signature() – validate X-Hub-Signature-256 from Meta webhooks
- parse_webhook()    – extract IncomingMessage list + delivery statuses
- WhatsAppChannel    – send text, buttons, documents, images, and templates via Graph API
"""

import hashlib
import hmac as _hmac
import logging
from datetime import datetime, timezone

import httpx

from app.channels.base import IncomingMessage

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
MAX_BUTTONS = 3       # Meta hard limit
MAX_TITLE_LEN = 20    # Meta hard limit for button titles


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_signature(raw: bytes, header: str | None, secret: str) -> bool:
    """Return True if the HMAC-SHA256 signature matches.

    Returns False (rather than raising) when the header is missing, empty,
    malformed, or the secret itself is empty – callers treat all of those as
    "reject this request".
    """
    if not header or not secret:
        return False
    try:
        prefix, hex_digest = header.split("=", 1)
    except ValueError:
        return False
    expected = _hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, hex_digest)


# ---------------------------------------------------------------------------
# Webhook payload parsing
# ---------------------------------------------------------------------------

def _parse_message(raw: dict) -> IncomingMessage:
    """Convert a single message dict from the webhook into an IncomingMessage."""
    kind = raw.get("type", "")
    text = None
    media_ref = None
    button_id = None

    if kind == "text":
        text = raw.get("text", {}).get("body")
    elif kind == "audio":
        kind = "voice"
        media_ref = raw.get("audio", {}).get("id")
    elif kind == "image":
        kind = "image"
        media_ref = raw.get("image", {}).get("id")
    elif kind == "interactive":
        kind = "button"
        interactive = raw.get("interactive", {})
        if interactive.get("type") == "button_reply":
            button_id = interactive.get("button_reply", {}).get("id")

    ts_epoch = int(raw.get("timestamp", "0"))
    timestamp = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)

    return IncomingMessage(
        channel="whatsapp",
        user_ref=raw.get("from", ""),
        message_id=raw.get("id", ""),
        kind=kind,
        text=text,
        media_ref=media_ref,
        button_id=button_id,
        timestamp=timestamp,
    )


def parse_webhook(payload: dict) -> tuple[list[IncomingMessage], list[dict]]:
    """Extract messages and delivery-status dicts from a Meta webhook payload.

    Returns (messages, statuses) where either list may be empty.
    """
    messages: list[IncomingMessage] = []
    statuses: list[dict] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for raw in value.get("messages", []):
                messages.append(_parse_message(raw))
            statuses.extend(value.get("statuses", []))

    return messages, statuses


# ---------------------------------------------------------------------------
# WhatsApp Cloud API channel
# ---------------------------------------------------------------------------

class WhatsAppChannel:
    """Send messages through the WhatsApp Cloud API (Graph API)."""

    def __init__(self, token: str, phone_number_id: str, api_version: str = "v20.0",
                 client: httpx.AsyncClient | None = None):
        self._token = token
        self._phone_number_id = phone_number_id
        self._api_version = api_version
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=f"{GRAPH_BASE}/{api_version}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    @property
    def _url(self) -> str:
        return f"{GRAPH_BASE}/{self._api_version}/{self._phone_number_id}/messages"

    async def _post(self, body: dict) -> dict:
        """POST JSON to the messages endpoint and return the response body."""
        resp = await self._client.post(
            self._url,
            json=body,
            headers={"Authorization": f"Bearer {self._token}",
                      "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    async def send_text(self, user_ref: str, text: str) -> dict:
        return await self._post({
            "messaging_product": "whatsapp",
            "to": user_ref,
            "type": "text",
            "text": {"body": text},
        })

    async def send_buttons(self, user_ref: str, text: str,
                           buttons: list[tuple[str, str]]) -> dict:
        """Send an interactive button message.

        Meta allows at most 3 buttons with titles <= 20 chars each.
        Extra buttons are silently dropped; long titles are truncated.
        """
        rows = []
        for btn_id, title in buttons[:MAX_BUTTONS]:
            rows.append({
                "type": "reply",
                "reply": {"id": btn_id, "title": title[:MAX_TITLE_LEN]},
            })
        return await self._post({
            "messaging_product": "whatsapp",
            "to": user_ref,
            "recipient_type": "individual",
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {"buttons": rows},
            },
        })

    async def send_document(self, user_ref: str, media_id: str, filename: str,
                            caption: str = "") -> dict:
        body: dict = {
            "messaging_product": "whatsapp",
            "to": user_ref,
            "type": "document",
            "document": {"id": media_id, "filename": filename},
        }
        if caption:
            body["document"]["caption"] = caption
        return await self._post(body)

    async def send_image(self, user_ref: str, media_id: str,
                         caption: str = "") -> dict:
        body: dict = {
            "messaging_product": "whatsapp",
            "to": user_ref,
            "type": "image",
            "image": {"id": media_id},
        }
        if caption:
            body["image"]["caption"] = caption
        return await self._post(body)

    async def send_template(self, user_ref: str, template_name: str,
                            language_code: str = "en",
                            components: list[dict] | None = None) -> dict:
        body: dict = {
            "messaging_product": "whatsapp",
            "to": user_ref,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        }
        if components:
            body["template"]["components"] = components
        return await self._post(body)

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Download a media file by its ID.

        Returns (content_bytes, content_type).
        """
        # Step 1: get the download URL
        meta_resp = await self._client.get(
            f"{GRAPH_BASE}/{self._api_version}/{media_id}",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        meta_resp.raise_for_status()
        url = meta_resp.json()["url"]
        # Step 2: download the actual bytes
        dl_resp = await self._client.get(
            url, headers={"Authorization": f"Bearer {self._token}"},
        )
        dl_resp.raise_for_status()
        content_type = dl_resp.headers.get("content-type", "application/octet-stream")
        return dl_resp.content, content_type

    async def aclose(self) -> None:
        """Close the underlying httpx client if we created it."""
        if self._owns_client:
            await self._client.aclose()
