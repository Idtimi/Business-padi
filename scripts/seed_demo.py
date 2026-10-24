"""Seed script to populate a realistic demo merchant business for VoiceLedger.

Creates:
- Demo Business: "Mama Ngozi Provisions" (Trader plan)
- Merchant User: Mama Ngozi (+2348011223344)
- 10 Realistic Nigerian customers
- 5 Stock items with weighted average costs
- 3 weeks of confirmed cash sales, credit sales, repayments, expenses, documents
- Usage logs and analytics events for Part H pilot metrics
"""

import argparse
import hashlib
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app import models as m


def seed_demo_data(reset: bool = True):
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        # Check existing
        existing_user = db.query(m.User).filter_by(channel_user_id="2348011223344").first()
        if existing_user:
            if not reset:
                print("Demo user already exists. Use --reset to re-seed.")
                return
            # Wipe demo data
            biz = db.query(m.Business).filter_by(owner_user_id=existing_user.id).first()
            if biz:
                db.query(m.Payment).filter_by(business_id=biz.id).delete()
                db.query(m.Document).filter_by(business_id=biz.id).delete()
                db.query(m.StockMove).filter(m.StockMove.item_id.in_(
                    db.query(m.StockItem.id).filter_by(business_id=biz.id)
                )).delete(synchronize_session=False)
                db.query(m.StockItem).filter_by(business_id=biz.id).delete()
                db.query(m.Transaction).filter_by(business_id=biz.id).delete()
                db.query(m.Customer).filter_by(business_id=biz.id).delete()
                db.query(m.Event).filter_by(business_id=biz.id).delete()
                db.query(m.UsageLog).filter_by(business_id=biz.id).delete()
                db.query(m.Business).filter_by(id=biz.id).delete()
            db.delete(existing_user)
            db.commit()
            print("Existing demo business data wiped.")

        # 1. Create User
        user = m.User(
            channel="whatsapp",
            channel_user_id="2348011223344",
            display_name="Mama Ngozi",
            language="pcm",
            state="idle",
            consent_at=now - timedelta(days=22),
            created_at=now - timedelta(days=22),
        )
        db.add(user)
        db.flush()

        # 2. Create Business
        biz = m.Business(
            owner_user_id=user.id,
            name="Mama Ngozi Provisions",
            address="Shop 14, Tejuosho Ultra Modern Market, Yaba, Lagos",
            phone="+2348011223344",
            bank_details="GTBank 0123456789 (Mama Ngozi Ventures)",
            footer_note="Thank you for your patronage! God bless you.",
            plan="trader",
            plan_renews_at=now + timedelta(days=90),
            created_at=now - timedelta(days=22),
        )
        db.add(biz)
        db.flush()

        # Record onboarded event
        db.add(m.Event(
            business_id=biz.id,
            user_id=user.id,
            name="onboarded",
            props={"plan": "trader", "source": "whatsapp"},
            created_at=now - timedelta(days=22),
        ))

        # 3. Create 10 Customers
        customer_defs = [
            ("Alhaji Musa", "+2348031112222"),
            ("Chika Okonkwo", "+2348032223333"),
            ("Tunde Adeleke", "+2348033334444"),
            ("Emeka Okafor", "+2348034445555"),
            ("Blessing Eze", "+2348035556666"),
            ("Fatima Bello", "+2348036667777"),
            ("Baba Segun", "+2348037778888"),
            ("Grace Johnson", "+2348038889999"),
            ("Chukwuma Obi", "+2348039990000"),
            ("Aisha Garba", "+2348021110000"),
        ]
        customers = []
        for name, phone in customer_defs:
            c = m.Customer(
                business_id=biz.id,
                name=name,
                phone=phone,
                opted_in=1,
            )
            db.add(c)
            customers.append(c)
        db.flush()

        # 4. Create 5 Stock Items
        stock_defs = [
            ("Bag of Rice 50kg", "bag", 25, 3800000, 5),
            ("Indomie Carton 40pk", "carton", 40, 800000, 8),
            ("Milo Carton 500g", "carton", 15, 1600000, 3),
            ("Vegetable Oil 25L", "keg", 12, 3200000, 2),
            ("Golden Penny Sugar", "carton", 30, 1400000, 5),
        ]
        stock_items = []
        for name, unit, qty, avg_cost, low_at in stock_defs:
            si = m.StockItem(
                business_id=biz.id,
                name=name,
                base_unit=unit,
                qty=qty,
                avg_cost_kobo=avg_cost,
                low_at=low_at,
            )
            db.add(si)
            stock_items.append(si)
        db.flush()

        # Initial Restock Moves
        for si in stock_items:
            sm = m.StockMove(
                item_id=si.id,
                qty=si.qty,
                kind="restock",
                unit_cost_kobo=si.avg_cost_kobo,
                supplier="Alhaji Musa Wholesales",
                created_at=now - timedelta(days=21),
            )
            db.add(sm)

        # 5. Seed 3 weeks of transactions (Days -21 to Day 0)
        # Sales, Credit sales, Expenses, Repayments
        tx_count = 0
        doc_count = 0

        # Expenses across weeks
        expenses = [
            (20, "fuel", 500000, "Buy fuel for generator"),
            (17, "transport", 350000, "Transport bring goods from market"),
            (14, "fuel", 500000, "Generator fuel for shop"),
            (10, "levy", 200000, "Market association stall levy"),
            (7, "fuel", 600000, "Fuel 5 litres for generator"),
            (3, "transport", 400000, "Transport delivery to customer"),
            (1, "fuel", 550000, "Fuel generator"),
        ]
        for days_ago, cat, amt, desc in expenses:
            tx = m.Transaction(
                business_id=biz.id,
                kind="expense",
                total_kobo=amt,
                paid_kobo=amt,
                category=cat,
                status="confirmed",
                transcript=desc,
                created_by_user_id=user.id,
                created_at=now - timedelta(days=days_ago),
            )
            db.add(tx)
            db.add(m.Event(business_id=biz.id, user_id=user.id, name="voice_entry", created_at=tx.created_at))
            db.add(m.Event(business_id=biz.id, user_id=user.id, name="confirm_save", created_at=tx.created_at))
            tx_count += 1

        # Sales across 21 days
        sales_data = [
            # Day, customer_idx, item_idx, qty, unit_price_kobo, paid_kobo, due_days
            (21, 1, 0, 2, 4250000, 8500000, None),  # Chika cash sale
            (20, 3, 1, 5, 950000, 4750000, None),   # Emeka cash sale
            (19, 2, 0, 1, 4300000, 2000000, 7),     # Tunde credit sale (due in 7d)
            (18, 4, 2, 2, 1850000, 3700000, None),  # Blessing cash
            (17, 5, 3, 1, 3600000, 1500000, 5),     # Fatima credit
            (16, 6, 4, 3, 1650000, 4950000, None),  # Baba Segun cash
            (15, 7, 1, 4, 950000, 3800000, None),   # Grace cash
            (14, 8, 0, 3, 4250000, 5000000, 7),     # Chukwuma credit
            (13, 9, 2, 3, 1850000, 5550000, None),  # Aisha cash
            (12, 1, 3, 2, 3600000, 7200000, None),  # Chika cash
            (11, 3, 0, 2, 4300000, 4000000, 5),     # Emeka credit
            (10, 4, 4, 5, 1650000, 8250000, None),  # Blessing cash
            (9, 2, 1, 6, 950000, 5700000, None),    # Tunde cash
            (8, 5, 2, 2, 1850000, 3700000, None),   # Fatima cash
            (7, 6, 0, 1, 4300000, 2000000, 7),      # Baba Segun credit
            (6, 7, 3, 1, 3600000, 3600000, None),   # Grace cash
            (5, 8, 1, 5, 950000, 4750000, None),    # Chukwuma cash
            (4, 9, 4, 2, 1650000, 3300000, None),   # Aisha cash
            (3, 3, 2, 4, 1850000, 7400000, None),   # Emeka cash
            (2, 1, 0, 2, 4300000, 8600000, None),   # Chika cash
            (1, 4, 3, 2, 3600000, 3600000, 7),      # Blessing credit
        ]

        for days_ago, cust_idx, item_idx, qty, unit_price, paid, due_days in sales_data:
            cust = customers[cust_idx]
            item = stock_items[item_idx]
            total = qty * unit_price
            due_date = (now - timedelta(days=days_ago) + timedelta(days=due_days)).date() if due_days else None

            line_items = [{
                "name": item.name,
                "qty": qty,
                "unit": item.base_unit,
                "unit_price_kobo": unit_price,
                "total_kobo": total,
            }]

            tx_time = now - timedelta(days=days_ago, hours=random.randint(1, 8))
            tx = m.Transaction(
                business_id=biz.id,
                customer_id=cust.id,
                kind="sale",
                items_json=line_items,
                total_kobo=total,
                paid_kobo=paid,
                due_date=due_date,
                status="confirmed",
                transcript=f"I sell {qty} {item.base_unit} {item.name} to {cust.name}",
                created_by_user_id=user.id,
                created_at=tx_time,
            )
            db.add(tx)
            db.flush()

            # Deduct stock movement
            sm = m.StockMove(
                item_id=item.id,
                qty=-qty,
                kind="sale",
                unit_cost_kobo=item.avg_cost_kobo,
                transaction_id=tx.id,
                created_at=tx_time,
            )
            db.add(sm)

            # Record event
            db.add(m.Event(business_id=biz.id, user_id=user.id, name="voice_entry", created_at=tx_time))
            # 90% saved without edit, 10% edited
            if random.random() < 0.9:
                db.add(m.Event(business_id=biz.id, user_id=user.id, name="confirm_save", created_at=tx_time))
            else:
                db.add(m.Event(business_id=biz.id, user_id=user.id, name="confirm_edit", created_at=tx_time))
                db.add(m.Event(business_id=biz.id, user_id=user.id, name="confirm_save", created_at=tx_time))

            # Usage log
            db.add(m.UsageLog(
                business_id=biz.id,
                kind="asr",
                units=8.5,
                cost_estimate_kobo=500,
                created_at=tx_time,
            ))
            db.add(m.UsageLog(
                business_id=biz.id,
                kind="llm",
                units=450,
                cost_estimate_kobo=1000,
                created_at=tx_time,
            ))

            tx_count += 1

            # Documents: receipt if paid in full, invoice if credit
            if paid == total and random.random() < 0.5:
                doc_count += 1
                doc = m.Document(
                    business_id=biz.id,
                    kind="receipt",
                    number=f"REC-{doc_count:04d}",
                    transaction_id=tx.id,
                    customer_id=cust.id,
                    total_kobo=total,
                    file_path=f"data/docs/demo_rec_{doc_count}.pdf",
                    sha256=hashlib.sha256(f"rec_{doc_count}".encode()).hexdigest(),
                    status="sent",
                    created_at=tx_time,
                )
                db.add(doc)
                db.add(m.Event(business_id=biz.id, user_id=user.id, name="document_issued",
                               props={"kind": "receipt"}, created_at=tx_time))

            elif paid < total:
                doc_count += 1
                doc = m.Document(
                    business_id=biz.id,
                    kind="invoice",
                    number=f"INV-{doc_count:04d}",
                    transaction_id=tx.id,
                    customer_id=cust.id,
                    total_kobo=total,
                    file_path=f"data/docs/demo_inv_{doc_count}.pdf",
                    sha256=hashlib.sha256(f"inv_{doc_count}".encode()).hexdigest(),
                    status="sent",
                    created_at=tx_time,
                )
                db.add(doc)
                db.add(m.Event(business_id=biz.id, user_id=user.id, name="document_issued",
                               props={"kind": "invoice"}, created_at=tx_time))

        # Repayments on credit sales
        # Tunde pays ₦15,000 on Day 12
        pm1_time = now - timedelta(days=12)
        pm1 = m.Payment(
            business_id=biz.id,
            customer_id=customers[2].id,
            amount_kobo=1500000,
            created_at=pm1_time,
        )
        db.add(pm1)
        db.add(m.Event(business_id=biz.id, user_id=user.id, name="reminder_sent",
                       props={"customer_id": customers[2].id}, created_at=now - timedelta(days=13)))
        db.add(m.Event(business_id=biz.id, user_id=user.id, name="voice_entry", created_at=pm1_time))
        db.add(m.Event(business_id=biz.id, user_id=user.id, name="confirm_save", created_at=pm1_time))

        # Emeka pays ₦20,000 on Day 6
        pm2_time = now - timedelta(days=6)
        pm2 = m.Payment(
            business_id=biz.id,
            customer_id=customers[3].id,
            amount_kobo=2000000,
            created_at=pm2_time,
        )
        db.add(pm2)
        db.add(m.Event(business_id=biz.id, user_id=user.id, name="reminder_sent",
                       props={"customer_id": customers[3].id}, created_at=now - timedelta(days=7)))
        db.add(m.Event(business_id=biz.id, user_id=user.id, name="voice_entry", created_at=pm2_time))
        db.add(m.Event(business_id=biz.id, user_id=user.id, name="confirm_save", created_at=pm2_time))

        # Add query_asked events
        for d in [18, 14, 10, 5, 2]:
            db.add(m.Event(business_id=biz.id, user_id=user.id, name="query_asked",
                           props={"type": "total_owed"}, created_at=now - timedelta(days=d)))

        db.commit()
        print(f"Successfully seeded demo business '{biz.name}' (ID: {biz.id})!")
        print(f"- User: {user.display_name} ({user.channel_user_id})")
        print(f"- Customers: {len(customers)}")
        print(f"- Stock items: {len(stock_items)}")
        print(f"- Transactions: {tx_count}")
        print(f"- Documents: {doc_count}")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed realistic demo data for VoiceLedger")
    parser.add_argument("--no-reset", action="store_true", help="Do not wipe existing demo data")
    args = parser.parse_args()

    seed_demo_data(reset=not args.no_reset)
