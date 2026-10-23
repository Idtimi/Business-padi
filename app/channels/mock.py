class MockChannel:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, user_ref, text):
        self.sent.append({"to": user_ref, "type": "text", "text": text, "buttons": []})

    async def send_buttons(self, user_ref, text, buttons):
        self.sent.append({"to": user_ref, "type": "buttons", "text": text, "buttons": list(buttons)})