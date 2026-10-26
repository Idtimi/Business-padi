import json
from pathlib import Path
from typing import Protocol

from app.config import settings

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "extraction_cases.jsonl"


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, schema_name: str) -> dict: ...


class MockLLM:
    """Returns canned JSON by exact transcript match. No internet, no keys."""

    def __init__(self, path: Path = FIXTURES):
        self.cases: dict[str, dict] = {}
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

        # Rule-based fallback parsing for natural trade sentences
        import re

        # Debtor queries
        if any(w in user_norm for w in ("who dey owe", "who owe me", "who owe", "debtor", "person wey owe")):
            return {
                "language_detected": "pcm",
                "transactions": [{
                    "intent": "query",
                    "query": {"type": "top_debtor", "params": {}},
                }],
            }

        # 1. Sales (sell / sold)
        if "sell" in user_norm or "sold" in user_norm:
            m_qty = re.search(r'\b(\d+)\s*(bag|carton|sachet|piece|unit|kg)?s?\b', user_norm)
            qty = int(m_qty.group(1)) if m_qty else 1
            unit = m_qty.group(2) if (m_qty and m_qty.group(2)) else "unit"

            m_item = re.search(r'\b(rice|beans|indomie|garri|oil|sugar|fuel|cement|milo|yam|provisions|bread|eggs)\b', user_norm)
            item = m_item.group(1) if m_item else "goods"

            # Check for debt / owe amount
            m_owe = re.search(r'(?:owe|balance|remain)\s*(\d[\d,]*k?)', user_norm)
            owe_val = 0
            if m_owe:
                raw_owe = m_owe.group(1).replace(",", "")
                owe_val = int(float(raw_owe[:-1]) * 1000) if raw_owe.endswith("k") else (int(raw_owe) if raw_owe.isdigit() else 0)

            # Parse total amount
            all_numbers = re.findall(r'(\b\d[\d,]*k?\b)', user_norm)
            amt_candidates = [n for n in all_numbers if n != str(qty)]
            total_amt = 0
            if amt_candidates:
                raw_tot = amt_candidates[0].replace(",", "")
                total_amt = int(float(raw_tot[:-1]) * 1000) if raw_tot.endswith("k") else (int(raw_tot) if raw_tot.isdigit() else 0)

            # Determine payment & amount_paid
            if "credit" in user_norm or "never pay" in user_norm or "no pay" in user_norm:
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

            m_cust = re.search(r'(?:to|give)\s+([a-zA-Z]+)', user_norm)
            cust = m_cust.group(1).title() if m_cust else "Customer"

            return {
                "language_detected": "english",
                "transactions": [{
                    "intent": "sale",
                    "customer": cust,
                    "items": [{
                        "name": item,
                        "qty": qty,
                        "unit": unit,
                        "unit_price": int(total_amt / qty) if qty else total_amt,
                    }],
                    "payment": payment,
                    "amount_paid": amount_paid,
                }],
            }

        # 2. Customer Repayment (paid / don pay)
        if "paid" in user_norm or "don pay" in user_norm:
            m_amt = re.search(r'\b(\d[\d,]*k?)\b', user_norm)
            amt = 0
            if m_amt:
                raw_amt = m_amt.group(1).replace(",", "")
                amt = int(float(raw_amt[:-1]) * 1000) if raw_amt.endswith("k") else (int(raw_amt) if raw_amt.isdigit() else 0)

            m_cust = re.search(r'([a-zA-Z\s]+?)\s+(?:paid|don pay)', user_norm)
            cust = m_cust.group(1).strip().title() if m_cust else "Customer"

            return {
                "language_detected": "english",
                "transactions": [{
                    "intent": "payment_received",
                    "customer": cust,
                    "amount": amt,
                }],
            }

        # 3. Expenses (buy fuel, expense, paid rent)
        if "fuel" in user_norm or "expense" in user_norm or "rent" in user_norm:
            m_amt = re.search(r'\b(\d[\d,]*k?)\b', user_norm)
            amt = 0
            if m_amt:
                raw_amt = m_amt.group(1).replace(",", "")
                amt = int(float(raw_amt[:-1]) * 1000) if raw_amt.endswith("k") else (int(raw_amt) if raw_amt.isdigit() else 0)
            category = "fuel" if "fuel" in user_norm else ("rent" if "rent" in user_norm else "general")
            return {
                "language_detected": "english",
                "transactions": [{
                    "intent": "expense",
                    "category": category,
                    "amount": amt,
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
