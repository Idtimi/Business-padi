"""Stock tracking service: inventory, unit conversions, moves, and supplier pricing.

Per Phase 8 of the Voice-to-Ledger specification:
- Item matching with aliases and fuzzy matching.
- Unit conversions (e.g. 1 carton = 24 sachets).
- Restocks update weighted average cost and supplier price memory.
- Sales reduce stock via stock_moves.
- Negative stock allowed but flagged.
- Low-stock thresholds and alerts.
- Unknown items trigger "add item?" prompt.
- Gated by plan (allowed(business, 'stock_tracking')).
"""

from datetime import datetime, timezone
import difflib
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Business, ItemAlias, StockItem, StockMove, UnitConversion, now_iso
from app.money import fmt_naira, to_kobo
from app.services import plans

log = logging.getLogger(__name__)


def clean_item_name(name: str | None) -> str:
    """Trim and format item names."""
    if not name:
        return ""
    return " ".join(name.strip().split())


def get_or_create_item(
    session: Session,
    business_id: int,
    name: str,
    base_unit: str = "unit",
    low_at: float | None = None,
) -> StockItem:
    """Fetch or insert a StockItem record."""
    norm_name = clean_item_name(name)
    item = session.scalar(
        select(StockItem).where(
            StockItem.business_id == business_id,
            func.lower(StockItem.name) == norm_name.lower(),
        )
    )
    if item is None:
        item = StockItem(
            business_id=business_id,
            name=norm_name,
            base_unit=base_unit.lower() if base_unit else "unit",
            qty=0.0,
            avg_cost_kobo=0,
            low_at=low_at,
        )
        session.add(item)
        session.flush()
    return item


def add_alias(session: Session, business_id: int, item_id: int, alias: str) -> ItemAlias:
    """Register an alternate name/alias for an item."""
    norm_alias = clean_item_name(alias).lower()
    existing = session.scalar(
        select(ItemAlias).where(
            ItemAlias.business_id == business_id,
            ItemAlias.alias == norm_alias,
        )
    )
    if existing is not None:
        existing.item_id = item_id
        session.flush()
        return existing

    ia = ItemAlias(business_id=business_id, item_id=item_id, alias=norm_alias)
    session.add(ia)
    session.flush()
    return ia


def set_unit_conversion(session: Session, item_id: int, unit: str, to_base: float) -> UnitConversion:
    """Register a conversion ratio from a unit to the item's base unit.

    e.g. item has base_unit 'sachet'. unit 'carton' with to_base 24.0
    means 1 carton = 24 sachets.
    """
    unit_norm = unit.strip().lower()
    conv = session.scalar(
        select(UnitConversion).where(
            UnitConversion.item_id == item_id,
            func.lower(UnitConversion.unit) == unit_norm,
        )
    )
    if conv is None:
        conv = UnitConversion(item_id=item_id, unit=unit_norm, to_base=to_base)
        session.add(conv)
    else:
        conv.to_base = to_base
    session.flush()
    return conv


def convert_to_base_qty(session: Session, item: StockItem, qty: float, unit: str | None) -> float:
    """Convert a given quantity in 'unit' into base units."""
    if not unit or not item.base_unit:
        return qty
    if unit.strip().lower() == item.base_unit.strip().lower():
        return qty

    conv = session.scalar(
        select(UnitConversion).where(
            UnitConversion.item_id == item.id,
            func.lower(UnitConversion.unit) == unit.strip().lower(),
        )
    )
    if conv is not None:
        return qty * conv.to_base

    return qty


def match_item(session: Session, business_id: int, query: str) -> tuple[StockItem | None, list[StockItem]]:
    """Match an item name query using exact match, aliases, and fuzzy search.

    Returns:
        (matched_item, ambiguous_matches)
        - If exactly one item matches: (StockItem, [])
        - If multiple close matches: (None, [StockItem, ...])
        - If no match found: (None, [])
    """
    q_clean = clean_item_name(query).lower()
    if not q_clean:
        return None, []

    # 1. Exact match on item name
    item = session.scalar(
        select(StockItem).where(
            StockItem.business_id == business_id,
            func.lower(StockItem.name) == q_clean,
        )
    )
    if item is not None:
        return item, []

    # 2. Exact match on item alias
    alias = session.scalar(
        select(ItemAlias).where(
            ItemAlias.business_id == business_id,
            func.lower(ItemAlias.alias) == q_clean,
        )
    )
    if alias is not None:
        item = session.get(StockItem, alias.item_id)
        if item is not None:
            return item, []

    # 3. Substring & fuzzy matches
    all_items = list(session.scalars(select(StockItem).where(StockItem.business_id == business_id)))
    if not all_items:
        return None, []

    # Check substring matches
    substring_matches = [
        it for it in all_items if q_clean in it.name.lower() or it.name.lower() in q_clean
    ]
    if len(substring_matches) == 1:
        return substring_matches[0], []
    if len(substring_matches) > 1:
        return None, substring_matches

    # Check fuzzy matches via difflib
    names_map = {it.name.lower(): it for it in all_items}
    close_names = difflib.get_close_matches(q_clean, names_map.keys(), n=3, cutoff=0.6)
    if len(close_names) == 1:
        return names_map[close_names[0]], []
    if len(close_names) > 1:
        return None, [names_map[name] for name in close_names]

    return None, []


def record_restock(
    session: Session,
    business_id: int,
    item_name: str,
    qty: float,
    unit: str | None,
    unit_cost_kobo: int,
    supplier: str | None = None,
    transaction_id: int | None = None,
) -> tuple[StockItem, StockMove]:
    """Process incoming inventory, updating weighted average cost and recording move.

    Acceptance rule:
    Restock 10 cartons at N18,000 then 10 at N20,000 gives average N19,000.
    """
    biz = session.get(Business, business_id)
    if biz and not plans.allowed(biz, "stock_tracking"):
        log.info("Business %s plan does not allow stock tracking; skipping stock update", business_id)

    item, _ = match_item(session, business_id, item_name)
    if item is None:
        item = get_or_create_item(session, business_id, item_name, base_unit=unit or "unit")

    base_qty = convert_to_base_qty(session, item, qty, unit)
    total_batch_cost_kobo = round(qty * unit_cost_kobo)

    # Weighted average cost calculation
    if item.qty <= 0:
        new_avg = round(total_batch_cost_kobo / base_qty) if base_qty > 0 else unit_cost_kobo
    else:
        existing_total_value = round(item.qty * item.avg_cost_kobo)
        new_total_qty = item.qty + base_qty
        new_avg = round((existing_total_value + total_batch_cost_kobo) / new_total_qty)

    item.qty += base_qty
    item.avg_cost_kobo = new_avg

    # Record stock move
    move = StockMove(
        item_id=item.id,
        qty=base_qty,
        kind="restock",
        unit_cost_kobo=unit_cost_kobo,
        supplier=supplier,
        transaction_id=transaction_id,
        created_at=now_iso(),
    )
    session.add(move)
    session.flush()
    return item, move


def record_sale(
    session: Session,
    business_id: int,
    item_name: str,
    qty: float,
    unit: str | None = None,
    transaction_id: int | None = None,
) -> tuple[StockItem | None, StockMove | None, dict]:
    """Reduce stock for sold items and flag negative or low stock conditions."""
    biz = session.get(Business, business_id)
    if biz and not plans.allowed(biz, "stock_tracking"):
        return None, None, {"skipped": True}

    item, ambiguous = match_item(session, business_id, item_name)
    if item is None:
        return None, None, {"unknown_item": item_name, "ambiguous": ambiguous}

    base_qty = convert_to_base_qty(session, item, qty, unit)
    item.qty -= base_qty

    move = StockMove(
        item_id=item.id,
        qty=-base_qty,
        kind="sale",
        unit_cost_kobo=item.avg_cost_kobo,
        transaction_id=transaction_id,
        created_at=now_iso(),
    )
    session.add(move)
    session.flush()

    flags = {
        "negative_stock": item.qty < 0,
        "low_stock": item.low_at is not None and item.qty <= item.low_at,
        "current_qty": item.qty,
        "base_unit": item.base_unit,
    }
    return item, move, flags


def get_supplier_last_price(session: Session, item_id: int, supplier: str) -> int | None:
    """Supplier price memory: retrieve the last restock price paid to this supplier."""
    move = session.scalar(
        select(StockMove)
        .where(
            StockMove.item_id == item_id,
            func.lower(StockMove.supplier) == supplier.strip().lower(),
            StockMove.kind == "restock",
        )
        .order_by(StockMove.id.desc())
    )
    return move.unit_cost_kobo if move else None


def get_low_stock_items(session: Session, business_id: int) -> list[StockItem]:
    """Find all items currently at or below their low-stock threshold."""
    return list(
        session.scalars(
            select(StockItem).where(
                StockItem.business_id == business_id,
                StockItem.low_at.is_not(None),
                StockItem.qty <= StockItem.low_at,
            )
        )
    )
