#!/usr/bin/env python3
"""Calculates average monthly cost per tier and actual margins from wa_message_log and usage_log."""

import os
import sys
from datetime import datetime, timedelta, timezone

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select
from app.db import SessionLocal
from app.models import Business, UsageLog, WaMessageLog, get_tz
from config.plans import PLANS
from app.money import fmt_naira


def run_price_check(session=None):
    close_session = False
    if session is None:
        session = SessionLocal()
        close_session = True

    try:
        now = datetime.now(get_tz())
        thirty_days_ago = (now - timedelta(days=30)).isoformat()

        # Query businesses
        businesses = session.scalars(select(Business)).all()
        biz_by_id = {b.id: b for b in businesses}

        # Query message logs (last 30 days)
        all_wa_logs = session.scalars(
            select(WaMessageLog).where(WaMessageLog.created_at >= thirty_days_ago)
        ).all()

        # Query usage logs (last 30 days)
        all_usage_logs = session.scalars(
            select(UsageLog).where(UsageLog.created_at >= thirty_days_ago)
        ).all()

        # Group costs by business
        biz_costs = {b.id: 0 for b in businesses}
        for l in all_wa_logs:
            if l.business_id in biz_costs:
                biz_costs[l.business_id] += (l.est_cost_kobo or 0)

        for u in all_usage_logs:
            if u.business_id in biz_costs:
                biz_costs[u.business_id] += (u.cost_estimate_kobo or 0)

        # Aggregate by tier
        tier_stats = {}
        for plan_name, plan_info in PLANS.items():
            price = plan_info.price_kobo if hasattr(plan_info, "price_kobo") else plan_info.get("price_kobo", 0)
            tier_stats[plan_name] = {
                "businesses": 0,
                "total_cost_kobo": 0,
                "price_kobo": price,
            }

        for b in businesses:
            plan = b.plan or "starter"
            if plan not in tier_stats:
                tier_stats[plan] = {"businesses": 0, "total_cost_kobo": 0, "price_kobo": 0}
            tier_stats[plan]["businesses"] += 1
            tier_stats[plan]["total_cost_kobo"] += biz_costs.get(b.id, 0)

        print("\n=======================================================")
        print("          VOICE-TO-LEDGER 30-DAY PRICE & MARGIN CHECK   ")
        print("=======================================================")
        print(f"Report Generated: {now.strftime('%Y-%m-%d %H:%M:%S WAT')}")
        print("-------------------------------------------------------")
        print(f"{'Tier':<10} | {'Active':<6} | {'Avg Cost/Biz':<14} | {'Plan Price':<12} | {'Margin':<8}")
        print("-------------------------------------------------------")

        results = {}
        for plan_name, stats in tier_stats.items():
            count = stats["businesses"]
            avg_cost = round(stats["total_cost_kobo"] / count) if count > 0 else 0
            price = stats["price_kobo"]
            if price > 0:
                margin_pct = round(((price - avg_cost) / price) * 100, 1)
                margin_str = f"{margin_pct}%"
            else:
                margin_str = "N/A (free)"

            print(f"{plan_name.title():<10} | {count:<6} | {fmt_naira(avg_cost):<14} | {fmt_naira(price):<12} | {margin_str:<8}")
            results[plan_name] = {
                "active_businesses": count,
                "avg_cost_kobo": avg_cost,
                "price_kobo": price,
                "margin_str": margin_str,
            }

        print("=======================================================\n")
        return results
    finally:
        if close_session:
            session.close()


if __name__ == "__main__":
    run_price_check()
