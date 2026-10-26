import json
from pathlib import Path
import re
from typing import Protocol

from app.config import settings

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "extraction_cases.jsonl"


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, schema_name: str) -> dict: ...


class MockLLM:
    """Returns canned JSON by exact transcript match, with robust fallback trade parsing."""

    def __init__(self, path: Path = FIXTURES):
        self.cases: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.cases[row["transcript"].strip().lower()] = row["expected"]

    def complete_json(self, system: str, user: str, schema_name: str) -> dict:
        found = self.cases.get(user.strip().lower())
        if found is not None:
            return found
        user_norm = user.strip().lower().rstrip("?.,!")

        if "who owe me pass" in user_norm:
            return {
                "language_detected": "pidgin",
                "transactions": [{
                    "intent": "query",
                    "query": {"type": "top_debtor", "params": {}},
                }],
            }
        if "receipt" in user_norm:
            return {
                "language_detected": "english",
                "transactions": [{
                    "intent": "receipt",
                }],
            }
        if "invoice" in user_norm:
            return {
                "language_detected": "english",
                "transactions": [{
                    "intent": "invoice",
                }],
            }
        if "profit" in user_norm:
            return {
                "language_detected": "english",
                "transactions": [{
                    "intent": "query",
                    "query": {"type": "profit_period", "params": {"period": "today"}},
                }],
            }

        # 1. Word number replacements across English, Pidgin, Yoruba, Hausa, Igbo
        word_map = {
            'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
            'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10',
            'twenty': '20', 'thirty': '30', 'forty': '40', 'fifty': '50',
            'hundred': '100', 'thousand': '000',
            'ookan': '1', 'meji': '2', 'meta': '3', 'merin': '4', 'marun': '5',
            'daya': '1', 'biyu': '2', 'uku': '3', 'hudu': '4', 'biyar': '5',
            'otu': '1', 'abuo': '2', 'ato': '3', 'ano': '4', 'ise': '5',
        }
        for w, d in word_map.items():
            user_norm = re.sub(rf'\b{w}\b', d, user_norm)

        # Normalize currency phrases like '40 k' -> '40k', '40 000' -> '40000'
        user_norm = re.sub(r'(\d+)\s+k\b', r'\1k', user_norm)
        user_norm = re.sub(r'(\d+)\s+000\b', r'\1000', user_norm)

        # Detect language
        lang_detected = "english"
        if any(w in user_norm for w in ('dey', 'don', 'fit', 'wetin', 'abeg', 'una', 'am')):
            lang_detected = "pcm"
        elif any(w in user_norm for w in ('mo ta', 'tita', 'san owo', 'e jowo', 'inawo', 'gbese', 'fun ')):
            lang_detected = "yo"
        elif any(w in user_norm for w in ('sayar', 'sayarwa', 'biya', 'bashi', 'kudi', 'ga ')):
            lang_detected = "ha"
        elif any(w in user_norm for w in ('erere', 'ahia', 'ugwo', 'ego', 'kwuru', 'nye ')):
            lang_detected = "ig"

        # Debtor queries
        if any(w in user_norm for w in ("who dey owe", "who owe me", "who owe", "debtor", "person wey owe", "tani o je", "waye ke bina", "onye ji")):
            return {
                "language_detected": lang_detected,
                "transactions": [{
                    "intent": "query",
                    "query": {"type": "top_debtor", "params": {}},
                }],
            }

        # Customer Repayments (paid / don pay / san owo / ya biya / akwula)
        if any(w in user_norm for w in ("paid", "don pay", "san owo", "ya biya", "akwula", "kwuru ugwo", "settle")):
            m_amt = re.search(r'\b(\d[\d,]*k?)\b', user_norm)
            amt = 0
            if m_amt:
                raw_amt = m_amt.group(1).replace(",", "")
                amt = int(float(raw_amt[:-1]) * 1000) if raw_amt.endswith("k") else (int(raw_amt) if raw_amt.isdigit() else 0)

            m_cust = re.search(r'([a-zA-Z\s]+?)\s+(?:paid|don pay|san|ya biya|kwuru|settle)', user_norm)
            cust = m_cust.group(1).strip().title() if m_cust else "Customer"

            return {
                "language_detected": lang_detected,
                "transactions": [{
                    "intent": "payment_received",
                    "customer": cust,
                    "amount": amt,
                }],
            }

        # Expenses (buy fuel, expense, paid rent, inawo, kudin mai, mmanu)
        if any(w in user_norm for w in ("fuel", "diesel", "petrol", "expense", "rent", "generator", "transport", "food", "inawo", "kudin mai", "mmanu")):
            m_amt = re.search(r'\b(\d[\d,]*k?)\b', user_norm)
            amt = 0
            if m_amt:
                raw_amt = m_amt.group(1).replace(",", "")
                amt = int(float(raw_amt[:-1]) * 1000) if raw_amt.endswith("k") else (int(raw_amt) if raw_amt.isdigit() else 0)
            category = "fuel" if any(w in user_norm for w in ("fuel", "diesel", "petrol", "generator", "kudin mai", "mmanu")) else ("rent" if "rent" in user_norm else "general")
            return {
                "language_detected": lang_detected,
                "transactions": [{
                    "intent": "expense",
                    "category": category,
                    "amount": amt,
                }],
            }

        # Sales (sell / sold / sale / buy / bought / ta / sayar / erere)
        is_sale = any(w in user_norm for w in ("sell", "sold", "sale", "buy", "bought", "ta ", "mo ta", "tita", "sayar", "erere", "ree", "give", "dash", "collect"))
        all_num_matches = list(re.finditer(r'\b(\d[\d,]*k?)\b', user_norm))

        if is_sale or len(all_num_matches) > 0:
            # Look for quantity explicitly tied to a unit
            m_qty = re.search(r'\b(\d+)\s*(bag|carton|sachet|piece|unit|kg|crate|pack|bundle|paint|mudu|derica|cup)s?\b', user_norm)
            qty = 1
            unit = "unit"
            qty_str = None
            if m_qty:
                qty = int(m_qty.group(1))
                unit = m_qty.group(2)
                qty_str = m_qty.group(1)

            # Look for trade commodity
            m_item = re.search(r'\b(rice|beans|indomie|garri|oil|sugar|fuel|cement|milo|yam|provisions|bread|eggs|meat|fish|soap|drinks|flour|biscuit|spaghetti|semovita|milk|groundnut)\b', user_norm)
            item = m_item.group(1) if m_item else "goods"

            # Check for debt / owe amount
            m_owe = re.search(r'(?:owe|balance|remain|gbese|bashi|ugwo)\s*(\d[\d,]*k?)', user_norm)
            owe_val = 0
            if m_owe:
                raw_owe = m_owe.group(1).replace(",", "")
                owe_val = int(float(raw_owe[:-1]) * 1000) if raw_owe.endswith("k") else (int(raw_owe) if raw_owe.isdigit() else 0)

            # Parse all numerical amounts
            nums = []
            for nm in all_num_matches:
                raw = nm.group(1).replace(",", "")
                val = int(float(raw[:-1]) * 1000) if raw.endswith("k") else (int(raw) if raw.isdigit() else 0)
                if qty_str and raw == qty_str and val < 100:
                    continue
                nums.append(val)

            total_amt = 0
            if nums:
                total_amt = max(nums)
                if owe_val == total_amt and len(nums) > 1:
                    candidates = [n for n in nums if n != owe_val]
                    if candidates:
                        total_amt = candidates[0]

            # Determine payment & amount_paid
            if any(w in user_norm for w in ("credit", "never pay", "no pay", "not pay")):
                payment = "credit"
                amount_paid = 0
            elif owe_val > 0:
                if owe_val >= total_amt:
                    payment = "credit"
                    amount_paid = 0
                else:
                    payment = "partial"
                    amount_paid = total_amt - owe_val
            else:
                payment = "cash"
                amount_paid = total_amt

            # Extract customer name
            m_cust = re.search(r'(?:to|give|fun|ga|nye)\s+([a-zA-Z]+)', user_norm)
            if not m_cust:
                m_cust = re.search(r'([a-zA-Z]+)\s+(?:buy|collect|bought)', user_norm)
            cust = m_cust.group(1).title() if m_cust else "Customer"

            unit_price = int(total_amt / qty) if qty > 0 and total_amt > 0 else total_amt

            return {
                "language_detected": lang_detected,
                "transactions": [{
                    "intent": "sale",
                    "customer": cust,
                    "items": [{
                        "name": item,
                        "qty": qty,
                        "unit": unit,
                        "unit_price": unit_price,
                    }],
                    "payment": payment,
                    "amount_paid": amount_paid,
                }],
            }

        return {
            "language_detected": "unknown",
            "transactions": [{
                "intent": "unknown",
                "needs_clarification": "Sorry, I no understand. Abeg talk am again.",
            }],
        }


def get_llm() -> LLMClient:
    if settings.llm_provider == "mock" or settings.demo_mode:
        return MockLLM()
    raise NotImplementedError("Real LLM provider comes later")
