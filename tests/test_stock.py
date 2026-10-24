"""Tests for Phase 8: Stock and suppliers.

Covers:
- Acceptance: Restock 10 cartons at N18,000 then 10 at N20,000 gives average N19,000; selling 3 cartons reduces stock to 17.
- Carton to sachet unit conversion (e.g. 1 carton = 24 sachets).
- Negative stock allowed but flagged.
- Item matching with aliases and fuzzy matching.
- Ambiguous item asks with buttons.
- Supplier price memory.
- Low-stock threshold alerts.
- Plan gating (stock tracking enabled on Trader/Business, disabled on Starter).
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models as m
from app.db import Base
from app.money import to_kobo
from app.pipeline import router
from app.services import ledger, plans, stock


@pytest.fixture()
def session(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'test_stock.db'}")
    Base.metadata.create_all(e)
    with Session(e, expire_on_commit=False) as s:
        yield s


def make_business(s, name="Kudirat Store", plan="trader"):
    u = m.User(channel="mock", channel_user_id=f"u-{name}", state="DONE", consent_at=m.now_iso())
    s.add(u)
    s.flush()
    b = m.Business(owner_user_id=u.id, name=name, plan=plan)
    s.add(b)
    s.commit()
    return u, b


def test_acceptance_restock_weighted_average_and_sale_reduction(session):
    """Acceptance test:
    Restock 10 cartons at N18,000 then 10 at N20,000 gives average N19,000;
    selling 3 cartons reduces stock to 17.
    """
    u, b = make_business(session)

    # 1. First restock: 10 cartons @ N18,000 (1,800,000 kobo)
    item, move1 = stock.record_restock(
        session=session,
        business_id=b.id,
        item_name="Milo",
        qty=10.0,
        unit="carton",
        unit_cost_kobo=to_kobo(18_000),
        supplier="Alhaji Musa",
    )
    assert item.qty == 10.0
    assert item.avg_cost_kobo == to_kobo(18_000)
    assert move1.kind == "restock"
    assert move1.qty == 10.0

    # 2. Second restock: 10 cartons @ N20,000 (2,000,000 kobo)
    item, move2 = stock.record_restock(
        session=session,
        business_id=b.id,
        item_name="Milo",
        qty=10.0,
        unit="carton",
        unit_cost_kobo=to_kobo(20_000),
        supplier="Alhaji Bello",
    )
    # Total qty: 20 cartons. Weighted average: (10*18k + 10*20k)/20 = N19,000
    assert item.qty == 20.0
    assert item.avg_cost_kobo == to_kobo(19_000)
    assert move2.qty == 10.0

    # 3. Selling 3 cartons reduces stock to 17
    item, sale_move, flags = stock.record_sale(
        session=session,
        business_id=b.id,
        item_name="Milo",
        qty=3.0,
        unit="carton",
    )
    assert item.qty == 17.0
    assert item.avg_cost_kobo == to_kobo(19_000)  # avg cost unaffected by sale
    assert sale_move.kind == "sale"
    assert sale_move.qty == -3.0
    assert flags["negative_stock"] is False


def test_carton_to_sachet_unit_conversion(session):
    """Test unit conversions: 1 carton = 24 sachets.
    Selling in cartons converts to base units (sachets).
    """
    u, b = make_business(session)

    # Base unit: sachet
    item = stock.get_or_create_item(session, b.id, "Cowbell Milk", base_unit="sachet")
    # 1 carton = 24 sachets
    stock.set_unit_conversion(session, item.id, unit="carton", to_base=24.0)

    # Restock 2 cartons -> 48 sachets added
    stock.record_restock(session, b.id, "Cowbell Milk", qty=2.0, unit="carton", unit_cost_kobo=to_kobo(3_600))
    session.refresh(item)
    assert item.qty == 48.0

    # Sell 6 sachets -> 42 sachets remaining
    stock.record_sale(session, b.id, "Cowbell Milk", qty=6.0, unit="sachet")
    session.refresh(item)
    assert item.qty == 42.0

    # Sell 1 carton -> reduces by 24 sachets -> 18 sachets remaining
    stock.record_sale(session, b.id, "Cowbell Milk", qty=1.0, unit="carton")
    session.refresh(item)
    assert item.qty == 18.0


def test_negative_stock_allowed_but_flagged(session):
    """Test that selling more than existing inventory is allowed but flagged as negative stock."""
    u, b = make_business(session)

    # Item initially has 2 bags
    stock.record_restock(session, b.id, "Mama Gold Rice", qty=2.0, unit="bag", unit_cost_kobo=to_kobo(40_000))

    # Sell 5 bags -> qty becomes -3.0, negative_stock is True
    item, move, flags = stock.record_sale(session, b.id, "Mama Gold Rice", qty=5.0, unit="bag")
    assert item.qty == -3.0
    assert flags["negative_stock"] is True
    assert flags["current_qty"] == -3.0


def test_item_alias_and_fuzzy_matching(session):
    """Test item matching with aliases and fuzzy matching."""
    u, b = make_business(session)
    item = stock.get_or_create_item(session, b.id, "Indomie Onion Chicken", base_unit="carton")

    # Add alias
    stock.add_alias(session, b.id, item.id, "Onion Indomie")

    # Match by exact alias
    matched_alias, _ = stock.match_item(session, b.id, "Onion Indomie")
    assert matched_alias is not None
    assert matched_alias.id == item.id

    # Fuzzy match with slight typo
    matched_fuzzy, _ = stock.match_item(session, b.id, "Indomi Onion Chiken")
    assert matched_fuzzy is not None
    assert matched_fuzzy.id == item.id


def test_ambiguous_item_matching(session):
    """Test that ambiguous item queries return candidate items."""
    u, b = make_business(session)
    item1 = stock.get_or_create_item(session, b.id, "Golden Morn 500g")
    item2 = stock.get_or_create_item(session, b.id, "Golden Morn 1kg")

    # Search for "Golden Morn" -> ambiguous between 500g and 1kg
    matched, candidates = stock.match_item(session, b.id, "Golden Morn")
    assert matched is None
    assert len(candidates) == 2
    candidate_ids = {it.id for it in candidates}
    assert item1.id in candidate_ids
    assert item2.id in candidate_ids


def test_supplier_price_memory(session):
    """Test supplier price memory tracking."""
    u, b = make_business(session, plan="business")

    item, _ = stock.record_restock(
        session, b.id, "Peak Milk", qty=5.0, unit="carton",
        unit_cost_kobo=to_kobo(25_000), supplier="Alhaji Bello"
    )

    # Restock from second supplier at higher price
    stock.record_restock(
        session, b.id, "Peak Milk", qty=5.0, unit="carton",
        unit_cost_kobo=to_kobo(26_500), supplier="Mama Chioma"
    )

    # Retrieve last price for Alhaji Bello
    price_bello = stock.get_supplier_last_price(session, item.id, "Alhaji Bello")
    assert price_bello == to_kobo(25_000)

    # Retrieve last price for Mama Chioma
    price_chioma = stock.get_supplier_last_price(session, item.id, "Mama Chioma")
    assert price_chioma == to_kobo(26_500)


def test_low_stock_threshold_alert(session):
    """Test low stock threshold alerts."""
    u, b = make_business(session)

    # Item with threshold low_at = 5.0
    item = stock.get_or_create_item(session, b.id, "Sugar", base_unit="bag", low_at=5.0)
    stock.record_restock(session, b.id, "Sugar", qty=10.0, unit="bag", unit_cost_kobo=to_kobo(15_000))

    # Sell 6 bags -> qty = 4.0 <= low_at (5.0) -> flagged!
    item, move, flags = stock.record_sale(session, b.id, "Sugar", qty=6.0, unit="bag")
    assert item.qty == 4.0
    assert flags["low_stock"] is True

    # Check get_low_stock_items query
    low_items = stock.get_low_stock_items(session, b.id)
    assert len(low_items) == 1
    assert low_items[0].name == "Sugar"


def test_stock_gated_by_plan(session):
    """Test plan gating: Starter cannot track stock; Trader can."""
    u, b_starter = make_business(session, "Starter Shop", plan="starter")
    u, b_trader = make_business(session, "Trader Shop", plan="trader")

    # Record sale on starter -> skipped
    _, _, flags_starter = stock.record_sale(session, b_starter.id, "Rice", qty=1.0)
    assert flags_starter.get("skipped") is True

    # Record restock on trader -> allowed
    item_trader, move = stock.record_restock(session, b_trader.id, "Rice", qty=5.0, unit="bag", unit_cost_kobo=to_kobo(40_000))
    assert item_trader.qty == 5.0
    assert move is not None
