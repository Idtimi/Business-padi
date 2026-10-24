class MockChannel:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, user_ref: str, text: str) -> dict:
        mid = f"wamid.mock.{len(self.sent) + 1}"
        record = {"to": user_ref, "type": "text", "text": text, "buttons": [], "message_id": mid}
        self.sent.append(record)
        return {"messages": [{"id": mid}]}

    async def send_buttons(self, user_ref: str, text: str, buttons: list[tuple[str, str]]) -> dict:
        mid = f"wamid.mock.{len(self.sent) + 1}"
        record = {"to": user_ref, "type": "buttons", "text": text, "buttons": list(buttons), "message_id": mid}
        self.sent.append(record)
        return {"messages": [{"id": mid}]}

    async def send_template(self, user_ref: str, template_name: str, language_code: str = "en",
                            components: list[dict] | None = None) -> dict:
        mid = f"wamid.mock.{len(self.sent) + 1}"
        record = {
            "to": user_ref,
            "type": "template",
            "template_name": template_name,
            "language": language_code,
            "components": components or [],
            "message_id": mid,
        }
        self.sent.append(record)
        return {"messages": [{"id": mid}]}

    async def send_document(self, user_ref: str, media_id: str, filename: str, caption: str = "") -> dict:
        mid = f"wamid.mock.{len(self.sent) + 1}"
        record = {
            "to": user_ref,
            "type": "document",
            "media_id": media_id,
            "filename": filename,
            "caption": caption,
            "message_id": mid,
        }
        self.sent.append(record)
        return {"messages": [{"id": mid}]}

    async def send_image(self, user_ref: str, media_id: str, caption: str = "") -> dict:
        mid = f"wamid.mock.{len(self.sent) + 1}"
        record = {
            "to": user_ref,
            "type": "image",
            "media_id": media_id,
            "caption": caption,
            "message_id": mid,
        }
        self.sent.append(record)
        return {"messages": [{"id": mid}]}
