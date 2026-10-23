from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class IncomingMessage:
    channel: str          # "whatsapp" | "mock"
    user_ref: str         # phone number (E.164 digits)
    message_id: str
    kind: str             # "text" | "voice" | "image" | "button"
    text: str | None
    media_ref: str | None
    button_id: str | None
    timestamp: datetime


class Channel(Protocol):
    async def send_text(self, user_ref: str, text: str) -> None: ...
    async def send_buttons(self, user_ref: str, text: str, buttons: list[tuple[str, str]]) -> None: ...
    