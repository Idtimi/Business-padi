"""Export pilot metrics per Part H of VoiceLedger specification.

Calculates:
- Active traders per day & week-one retention
- Voice entries per trader
- Share of entries saved without edit (accuracy figure)
- Documents issued & reminders sent
- Debts paid after reminders
- Cost per voice entry from usage_log

Outputs a CSV file and prints a formatted summary.
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.db import SessionLocal
from app import models as m


def parse_dt(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def calculate_metrics():
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        # 1. Users / Traders
        all_users = db.query(m.User).all()
        total_traders = len(all_users)

        # 2. Events breakdown
        all_events = db.query(m.Event).all()
        all_events.sort(key=lambda ev: parse_dt(ev.created_at) or now)

        events_by_name = defaultdict(int)
        events_by_day = defaultdict(lambda: defaultdict(int))
        user_active_days = defaultdict(set)

        for ev in all_events:
            events_by_name[ev.name] += 1
            ev_dt = parse_dt(ev.created_at)
            day_str = ev_dt.strftime("%Y-%m-%d") if ev_dt else "unknown"
            events_by_day[day_str][ev.name] += 1
            if ev.user_id:
                user_active_days[ev.user_id].add(day_str)

        # 3. Active traders per day
        daily_active_traders = {
            day: len({ev.user_id for ev in all_events if ev.user_id and parse_dt(ev.created_at) and parse_dt(ev.created_at).strftime("%Y-%m-%d") == day})
            for day in events_by_day
        }

        # 4. Week-one retention (cohort analysis)
        # Users who onboarded >= 14 days ago: did they return in days 8-14?
        eligible_cohort = 0
        retained_w1 = 0
        for u in all_users:
            u_created = parse_dt(u.created_at)
            if u_created and (now - u_created).days >= 14:
                eligible_cohort += 1
                w1_start = u_created + timedelta(days=7)
                w1_end = u_created + timedelta(days=14)
                # Check if user had any event between w1_start and w1_end
                active_in_w1 = any(
                    ev.user_id == u.id and parse_dt(ev.created_at) and w1_start <= parse_dt(ev.created_at) <= w1_end
                    for ev in all_events
                )
                if active_in_w1:
                    retained_w1 += 1

        w1_retention_pct = (retained_w1 / eligible_cohort * 100.0) if eligible_cohort > 0 else 100.0

        # 5. Voice entries & Accuracy
        total_voice_entries = events_by_name.get("voice_entry", 0)
        avg_voice_entries_per_trader = (total_voice_entries / total_traders) if total_traders > 0 else 0

        confirm_saves = events_by_name.get("confirm_save", 0)
        confirm_edits = events_by_name.get("confirm_edit", 0)
        confirm_cancels = events_by_name.get("confirm_cancel", 0)

        # Real accuracy figure = confirm_save without edit
        # If total interactions = saves + edits:
        total_decisions = confirm_saves + confirm_edits
        save_without_edit_pct = (
            (confirm_saves / (confirm_saves + confirm_edits) * 100.0)
            if (confirm_saves + confirm_edits) > 0
            else 0.0
        )

        # 6. Documents & Reminders
        docs_issued = events_by_name.get("document_issued", 0) or db.query(m.Document).count()
        reminders_sent = events_by_name.get("reminder_sent", 0) or db.query(m.Reminder).filter_by(status="sent").count()

        # 7. Debts paid after reminders
        # Find payments where customer had a reminder sent in prior 7 days
        reminder_events = [ev for ev in all_events if ev.name == "reminder_sent"]
        payments = db.query(m.Payment).all()
        debts_paid_after_reminders_count = 0
        debts_paid_after_reminders_kobo = 0

        for p in payments:
            p_dt = parse_dt(p.created_at)
            if not p_dt:
                continue
            matched = any(
                ev.props and ev.props.get("customer_id") == p.customer_id
                and parse_dt(ev.created_at)
                and timedelta(days=0) <= (p_dt - parse_dt(ev.created_at)) <= timedelta(days=7)
                for ev in reminder_events
            )
            if matched:
                debts_paid_after_reminders_count += 1
                debts_paid_after_reminders_kobo += p.amount_kobo

        # 8. Usage & Cost per Voice Entry
        usage_records = db.query(m.UsageLog).all()
        total_usage_cost_kobo = sum(u.cost_estimate_kobo for u in usage_records)
        cost_per_entry_kobo = (
            total_usage_cost_kobo / total_voice_entries if total_voice_entries > 0 else 0
        )
        cost_per_entry_ngn = cost_per_entry_kobo / 100.0

        return {
            "total_traders": total_traders,
            "daily_active_traders": daily_active_traders,
            "w1_retention_pct": w1_retention_pct,
            "total_voice_entries": total_voice_entries,
            "avg_voice_entries_per_trader": avg_voice_entries_per_trader,
            "confirm_saves": confirm_saves,
            "confirm_edits": confirm_edits,
            "confirm_cancels": confirm_cancels,
            "save_without_edit_pct": save_without_edit_pct,
            "documents_issued": docs_issued,
            "reminders_sent": reminders_sent,
            "debts_paid_after_reminders_count": debts_paid_after_reminders_count,
            "debts_paid_after_reminders_kobo": debts_paid_after_reminders_kobo,
            "total_usage_cost_kobo": total_usage_cost_kobo,
            "cost_per_entry_kobo": cost_per_entry_kobo,
            "cost_per_entry_ngn": cost_per_entry_ngn,
            "events_by_day": events_by_day,
        }
    finally:
        db.close()


def export_pilot_metrics(output_csv: str = "pilot_metrics.csv"):
    metrics = calculate_metrics()

    # Output CSV
    out_path = Path(output_csv)
    with open(out_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Metric", "Value", "Notes"])
        writer.writerow(["Total Traders", metrics["total_traders"], "Onboarded merchants"])
        writer.writerow(["Week-1 Retention", f"{metrics['w1_retention_pct']:.1f}%", "Active in week 2 after signup"])
        writer.writerow(["Total Voice Entries", metrics["total_voice_entries"], "Total audio entries received"])
        writer.writerow(["Voice Entries per Trader", f"{metrics['avg_voice_entries_per_trader']:.1f}", "Average per merchant"])
        writer.writerow(["Accuracy (Saved without Edit)", f"{metrics['save_without_edit_pct']:.1f}%", "Confirmations accepted without edit"])
        writer.writerow(["Documents Issued", metrics["documents_issued"], "Invoices & receipts generated"])
        writer.writerow(["Reminders Sent", metrics["reminders_sent"], "Debt payment reminders delivered"])
        writer.writerow(["Debts Paid After Reminder", metrics["debts_paid_after_reminders_count"], f"Value: ₦{metrics['debts_paid_after_reminders_kobo']/100:,.2f}"])
        writer.writerow(["Total AI/Log Cost (Kobo)", metrics["total_usage_cost_kobo"], f"₦{metrics['total_usage_cost_kobo']/100:,.2f}"])
        writer.writerow(["Cost per Voice Entry", f"₦{metrics['cost_per_entry_ngn']:.2f}", f"{metrics['cost_per_entry_kobo']:.1f} kobo per entry"])

        writer.writerow([])
        writer.writerow(["Date", "Active Traders", "Voice Entries", "Saves", "Edits"])
        for day in sorted(metrics["events_by_day"].keys()):
            counts = metrics["events_by_day"][day]
            active = metrics["daily_active_traders"].get(day, 0)
            writer.writerow([day, active, counts.get("voice_entry", 0), counts.get("confirm_save", 0), counts.get("confirm_edit", 0)])

    print(f"\n=======================================================")
    print(f"       VOICELEDGER PILOT METRICS (PART H)             ")
    print(f"=======================================================")
    print(f"Total Traders Onboarded:       {metrics['total_traders']}")
    print(f"Week-1 Retention:              {metrics['w1_retention_pct']:.1f}%")
    print(f"Total Voice Entries:           {metrics['total_voice_entries']}")
    print(f"Voice Entries per Trader:      {metrics['avg_voice_entries_per_trader']:.1f}")
    print(f"Saved without Edit (Accuracy): {metrics['save_without_edit_pct']:.1f}%")
    print(f"Documents Issued:              {metrics['documents_issued']}")
    print(f"Reminders Sent:                {metrics['reminders_sent']}")
    print(f"Debts Paid After Reminder:     {metrics['debts_paid_after_reminders_count']} (₦{metrics['debts_paid_after_reminders_kobo']/100:,.2f})")
    print(f"Cost per Voice Entry:          ₦{metrics['cost_per_entry_ngn']:.2f} ({metrics['cost_per_entry_kobo']:.1f} kobo)")
    print(f"=======================================================")
    print(f"Report exported to: {out_path.resolve()}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export VoiceLedger Part H pilot metrics")
    parser.add_argument("--output", type=str, default="pilot_metrics.csv", help="Output CSV path")
    args = parser.parse_args()

    export_pilot_metrics(output_csv=args.output)
