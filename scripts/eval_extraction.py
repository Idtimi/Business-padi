"""Evaluate LLM transaction extraction per-field accuracy on fixture transcripts."""

import argparse
import json
import sys
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.pipeline.extract import extract
from app.pipeline.llm import MockLLM


def run_extraction_eval():
    cases_path = Path("tests/fixtures/extraction_cases.jsonl")
    if not cases_path.exists():
        print(f"Error: Fixtures file {cases_path} not found.")
        return

    cases = []
    with open(cases_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))

    fields = ["intent", "customer", "item", "qty", "unit_price", "payment", "due_date"]
    field_counts = {f: {"correct": 0, "total": 0} for f in fields}
    fully_correct = 0

    llm = MockLLM()

    for case in cases:
        transcript = case["transcript"]
        expected_txs = case["expected"].get("transactions", [])
        try:
            result = extract(llm, transcript)
            actual_doc = result.model_dump()
        except Exception:
            actual_doc = {}
        actual_txs = actual_doc.get("transactions", [])

        if not expected_txs and not actual_txs:
            fully_correct += 1
            continue

        case_is_fully_correct = True

        for exp in expected_txs:
            # Match actual by intent or order
            act = next((a for a in actual_txs if a.get("intent") == exp.get("intent")), actual_txs[0] if actual_txs else {})

            # Intent
            field_counts["intent"]["total"] += 1
            if act.get("intent") == exp.get("intent"):
                field_counts["intent"]["correct"] += 1
            else:
                case_is_fully_correct = False

            # Customer
            if "customer" in exp:
                field_counts["customer"]["total"] += 1
                if act.get("customer") == exp.get("customer"):
                    field_counts["customer"]["correct"] += 1
                else:
                    case_is_fully_correct = False

            # Item, Qty, Unit Price
            if exp.get("items"):
                exp_item = exp["items"][0]
                act_items = act.get("items", [{}])
                act_item = act_items[0] if act_items else {}

                field_counts["item"]["total"] += 1
                if act_item.get("name") == exp_item.get("name"):
                    field_counts["item"]["correct"] += 1
                else:
                    case_is_fully_correct = False

                if "qty" in exp_item:
                    field_counts["qty"]["total"] += 1
                    if act_item.get("qty") == exp_item.get("qty"):
                        field_counts["qty"]["correct"] += 1
                    else:
                        case_is_fully_correct = False

                if "unit_price" in exp_item:
                    field_counts["unit_price"]["total"] += 1
                    if act_item.get("unit_price") == exp_item.get("unit_price"):
                        field_counts["unit_price"]["correct"] += 1
                    else:
                        case_is_fully_correct = False

            # Payment
            if "payment" in exp:
                field_counts["payment"]["total"] += 1
                if act.get("payment") == exp.get("payment"):
                    field_counts["payment"]["correct"] += 1
                else:
                    case_is_fully_correct = False

            # Due Date
            if "due_date" in exp:
                field_counts["due_date"]["total"] += 1
                if act.get("due_date") == exp.get("due_date"):
                    field_counts["due_date"]["correct"] += 1
                else:
                    case_is_fully_correct = False

        if case_is_fully_correct:
            fully_correct += 1

    total_cases = len(cases)
    fully_correct_pct = (fully_correct / total_cases * 100.0) if total_cases > 0 else 0.0

    print("\n=======================================================")
    print("      LLM EXTRACTION PER-FIELD ACCURACY EVALUATION     ")
    print("=======================================================")
    print(f"Total Test Cases: {total_cases}")
    print(f"Transcripts Fully Correct: {fully_correct}/{total_cases} ({fully_correct_pct:.1f}%)\n")
    print(f"{'Field':<15} | {'Correct':<8} | {'Total':<8} | Accuracy")
    print("-" * 48)

    md_lines = [
        "\n# LLM Extraction Evaluation Results\n",
        f"- **Fully Correct Transcripts:** {fully_correct}/{total_cases} ({fully_correct_pct:.1f}%)\n",
        "| Field | Correct | Total | Accuracy |",
        "| --- | --- | --- | --- |",
    ]

    for f in fields:
        corr = field_counts[f]["correct"]
        tot = field_counts[f]["total"]
        pct = (corr / tot * 100.0) if tot > 0 else 100.0
        print(f"{f:<15} | {corr:<8} | {tot:<8} | {pct:>6.1f}%")
        md_lines.append(f"| {f} | {corr} | {tot} | {pct:.1f}% |")

    print("=======================================================\n")
    return "\n".join(md_lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate LLM extraction per-field accuracy")
    parser.add_argument("--save", action="store_true", help="Append markdown results to docs/eval_results.md")
    args = parser.parse_args()

    report = run_extraction_eval()
    if args.save:
        doc_path = Path("docs/eval_results.md")
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        with open(doc_path, "a", encoding="utf-8") as f:
            f.write("\n" + report)
        print(f"Results appended to {doc_path}")
