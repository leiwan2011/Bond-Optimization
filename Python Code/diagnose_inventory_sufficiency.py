#!/usr/bin/env python3
"""
Diagnose whether Dealer Inventory is sufficient for ETF create/redemption baskets.

This script does not optimize a trade basket. It is a pre-trade inventory
diagnostic that answers:
- Is there enough tradable capacity by sector-duration combo?
- Are there enough eligible names per combo?
- Is the benchmark duration target reachable from available inventory?
- If not, what should a human look at first?

The script uses the same major assumptions as optimize_etf_trade.py:
- Benchmark targets are based on Bmk Weight.
- Create capacity is capped by Dealer Inventory and Issued Amount.
- Redemption capacity is capped by Dealer Inventory, current Shares, and Issued Amount.
- Round lot is 1,000 shares.
"""

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path


# =============================================================================
# CONFIGURATION SECTION - EDIT THIS BLOCK WHEN MOVING TO A NEW ENVIRONMENT
# =============================================================================
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "Output" / "XBB_holdings_with_dealer_inventory.csv"
DEFAULT_OUTPUT_DIR = ROOT / "Output"

COLUMN_MAPPING = {
    "shares": "Shares",
    "market_value": "Market Value",
    "duration": "Duration",
    "dealer_inventory": "Dealer Inventory",
    "bmk_weight": "Bmk Weight",
    "issued_amount": "Issued Amount",
    "sector": "Sector",
    "ticker": "Ticker",
    "name": "Name",
}

FEDERAL_SECTORS = {"federal"}
GOV_SECTORS = {"provincial", "municipal"}
DEFAULT_SECTOR_GROUP = "corporate"

DEFAULT_UNITS_PER_PNU = 50_000.0
DEFAULT_NAV_CAD = 28.22
DEFAULT_MIN_SECURITIES_PER_COMBO = 2
ISSUED_AMOUNT_MULTIPLIER = 1000
MAX_TRADE_FRACTION_OF_ISSUED_AMOUNT = 0.5
ROUND_LOT = 1000

DURATION_BUCKETS = (
    ("0-5", 0.0, 5.0, False),
    ("5-10", 5.0, 10.0, False),
    ("10-14", 10.0, 14.0, False),
    ("14-30", 14.0, 30.0, True),
)
# =============================================================================
# END CONFIGURATION SECTION
# =============================================================================


def parse_number(value):
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    return float(text)


def fmt_money(value):
    return f"{value:,.2f}"


def input_value(row, field_name, default=""):
    return row.get(COLUMN_MAPPING[field_name], default)


def validate_required_columns(fieldnames):
    required = [
        "shares",
        "market_value",
        "duration",
        "dealer_inventory",
        "bmk_weight",
        "issued_amount",
        "sector",
    ]
    missing = [COLUMN_MAPPING[field] for field in required if COLUMN_MAPPING[field] not in fieldnames]
    if missing:
        raise ValueError(
            "Input file is missing required column(s): "
            + ", ".join(missing)
            + ". Update COLUMN_MAPPING at the top of this script."
        )


def sector_group(sector):
    normalized = str(sector).strip().lower()
    if normalized in FEDERAL_SECTORS:
        return "federal"
    if normalized in GOV_SECTORS:
        return "gov"
    return DEFAULT_SECTOR_GROUP


def duration_bucket(duration):
    for name, low, high, include_high in DURATION_BUCKETS:
        if duration >= low and (duration < high or (include_high and duration <= high)):
            return name
    return "out-of-range"


def read_holdings(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        validate_required_columns(reader.fieldnames or [])
        rows = list(reader)

    holdings = []
    for row_number, row in enumerate(rows, start=2):
        shares = parse_number(input_value(row, "shares"))
        market_value = parse_number(input_value(row, "market_value"))
        duration = parse_number(input_value(row, "duration"))
        dealer_inventory = parse_number(input_value(row, "dealer_inventory"))
        bmk_weight = parse_number(input_value(row, "bmk_weight"))
        issued_amount = parse_number(input_value(row, "issued_amount"))
        if shares <= 0 or market_value <= 0:
            continue

        group = sector_group(input_value(row, "sector"))
        bucket = duration_bucket(duration)
        row["_row_number"] = row_number
        row["_shares"] = shares
        row["_market_value"] = market_value
        row["_price_per_share"] = market_value / shares
        row["_duration"] = duration
        row["_dealer_inventory"] = dealer_inventory
        row["_bmk_weight"] = bmk_weight
        row["_issued_amount"] = issued_amount
        row["_sector_group"] = group
        row["_duration_bucket"] = bucket
        row["_bucket_key"] = f"{group}|{bucket}"
        holdings.append(row)

    return holdings


def benchmark_targets(holdings, total_trade_value):
    combos = defaultdict(lambda: {"bmk_weight": 0.0, "duration_weight": 0.0})
    buckets = defaultdict(lambda: {"bmk_weight": 0.0, "duration_weight": 0.0})
    total_weight = 0.0

    for row in holdings:
        if row["_duration_bucket"] == "out-of-range":
            continue
        weight = row["_bmk_weight"]
        total_weight += weight
        combo = row["_bucket_key"]
        bucket = row["_duration_bucket"]
        combos[combo]["bmk_weight"] += weight
        combos[combo]["duration_weight"] += weight * row["_duration"]
        buckets[bucket]["bmk_weight"] += weight
        buckets[bucket]["duration_weight"] += weight * row["_duration"]

    combo_targets = {}
    for combo, stats in combos.items():
        target_weight = stats["bmk_weight"] / total_weight if total_weight else 0.0
        combo_targets[combo] = {
            "target_value": total_trade_value * target_weight,
            "target_duration": stats["duration_weight"] / stats["bmk_weight"],
            "target_weight": target_weight,
        }

    bucket_targets = {}
    for bucket, stats in buckets.items():
        target_weight = stats["bmk_weight"] / total_weight if total_weight else 0.0
        bucket_targets[bucket] = {
            "target_value": total_trade_value * target_weight,
            "target_duration": stats["duration_weight"] / stats["bmk_weight"],
            "target_weight": target_weight,
        }

    bmk_duration = sum(row["_bmk_weight"] * row["_duration"] for row in holdings) / total_weight
    return combo_targets, bucket_targets, bmk_duration


def max_tradable_shares(row, side):
    dealer_cap = math.floor(row["_dealer_inventory"] / ROUND_LOT) * ROUND_LOT
    issued_cap = (
        row["_issued_amount"]
        * ISSUED_AMOUNT_MULTIPLIER
        * MAX_TRADE_FRACTION_OF_ISSUED_AMOUNT
    )
    issued_cap = math.floor(issued_cap / ROUND_LOT) * ROUND_LOT

    if side == "create":
        max_shares = min(dealer_cap, issued_cap)
    else:
        position_cap = math.floor(row["_shares"] / ROUND_LOT) * ROUND_LOT
        max_shares = min(dealer_cap, issued_cap, position_cap)

    return max(0, max_shares)


def capacity_weighted_duration(rows):
    total_capacity_value = sum(row["_capacity_value"] for row in rows)
    if total_capacity_value <= 0:
        return 0.0
    return sum(row["_capacity_value"] * row["_duration"] for row in rows) / total_capacity_value


def diagnose_combo(combo, rows, target, min_securities):
    eligible = [row for row in rows if row["_max_tradable_shares"] >= ROUND_LOT]
    available_capacity = sum(row["_capacity_value"] for row in eligible)
    target_value = target["target_value"]
    target_duration = target["target_duration"]
    shortfall = max(0.0, target_value - available_capacity)

    if eligible:
        min_duration = min(row["_duration"] for row in eligible)
        max_duration = max(row["_duration"] for row in eligible)
        cap_weighted_duration = capacity_weighted_duration(eligible)
    else:
        min_duration = None
        max_duration = None
        cap_weighted_duration = None

    amount_pass = available_capacity + 1e-9 >= target_value
    name_count_pass = len(eligible) >= min_securities
    duration_reachable = (
        bool(eligible)
        and min_duration - 1e-9 <= target_duration <= max_duration + 1e-9
    )

    bottlenecks = []
    messages = []
    if not name_count_pass:
        bottlenecks.append("name_count")
        messages.append(
            f"{combo}: not enough eligible securities. "
            f"Eligible securities = {len(eligible)}, minimum required = {min_securities}."
        )
    if not amount_pass:
        bottlenecks.append("amount_capacity")
        messages.append(
            f"{combo}: amount capacity shortfall. "
            f"Target value = {fmt_money(target_value)}, available capacity = {fmt_money(available_capacity)}, "
            f"shortfall = {fmt_money(shortfall)}."
        )
    if eligible and target_duration > max_duration + 1e-9:
        bottlenecks.append("duration_too_short")
        messages.append(
            f"{combo}: duration target unreachable. "
            f"Target duration = {target_duration:.4f}, max available duration = {max_duration:.4f}. "
            "Even if all eligible inventory is used, this combo remains too short."
        )
    elif eligible and target_duration < min_duration - 1e-9:
        bottlenecks.append("duration_too_long")
        messages.append(
            f"{combo}: duration target unreachable. "
            f"Target duration = {target_duration:.4f}, min available duration = {min_duration:.4f}. "
            "Even if all eligible inventory is used, this combo remains too long."
        )

    if eligible and amount_pass and duration_reachable:
        if cap_weighted_duration is not None and abs(cap_weighted_duration - target_duration) > 0.25:
            bottlenecks.append("duration_capacity_skew")
            direction = "above" if cap_weighted_duration > target_duration else "below"
            messages.append(
                f"{combo}: duration is technically reachable, but capacity is skewed. "
                f"Target duration = {target_duration:.4f}, capacity-weighted available duration = "
                f"{cap_weighted_duration:.4f} ({direction} target)."
            )

    return {
        "Combo": combo,
        "Target Value": fmt_money(target_value),
        "Available Capacity Value": fmt_money(available_capacity),
        "Capacity Shortfall": fmt_money(shortfall),
        "Eligible Securities": len(eligible),
        "Min Required Securities": min_securities,
        "Target Duration": f"{target_duration:.4f}",
        "Available Min Duration": "" if min_duration is None else f"{min_duration:.4f}",
        "Available Max Duration": "" if max_duration is None else f"{max_duration:.4f}",
        "Capacity Weighted Duration": "" if cap_weighted_duration is None else f"{cap_weighted_duration:.4f}",
        "Amount Pass": amount_pass,
        "Name Count Pass": name_count_pass,
        "Duration Reachable": duration_reachable,
        "Bottlenecks": ";".join(bottlenecks) if bottlenecks else "none",
        "_messages": messages,
    }


def diagnose_bucket(bucket, rows, target):
    eligible = [row for row in rows if row["_max_tradable_shares"] >= ROUND_LOT]
    available_capacity = sum(row["_capacity_value"] for row in eligible)
    target_value = target["target_value"]
    shortfall = max(0.0, target_value - available_capacity)

    if eligible:
        min_duration = min(row["_duration"] for row in eligible)
        max_duration = max(row["_duration"] for row in eligible)
        cap_weighted_duration = capacity_weighted_duration(eligible)
    else:
        min_duration = None
        max_duration = None
        cap_weighted_duration = None

    return {
        "Duration Bucket": bucket,
        "Target Value": fmt_money(target_value),
        "Available Capacity Value": fmt_money(available_capacity),
        "Capacity Shortfall": fmt_money(shortfall),
        "Eligible Securities": len(eligible),
        "Target Duration": f"{target['target_duration']:.4f}",
        "Available Min Duration": "" if min_duration is None else f"{min_duration:.4f}",
        "Available Max Duration": "" if max_duration is None else f"{max_duration:.4f}",
        "Capacity Weighted Duration": "" if cap_weighted_duration is None else f"{cap_weighted_duration:.4f}",
        "Amount Pass": available_capacity + 1e-9 >= target_value,
    }


def write_csv(path, rows, fieldnames):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_pnu_list(text):
    return [float(item.strip()) for item in text.split(",") if item.strip()]


def run_diagnostic(args):
    holdings = read_holdings(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    all_summary_rows = []
    written_files = []

    for side in args.sides:
        for pnu in args.pnu_list:
            target_value = pnu * args.units_per_pnu * args.nav
            combo_targets, bucket_targets, bmk_duration = benchmark_targets(holdings, target_value)

            rows_by_combo = defaultdict(list)
            rows_by_bucket = defaultdict(list)
            total_capacity = 0.0
            for row in holdings:
                max_shares = max_tradable_shares(row, side)
                capacity_value = max_shares * row["_price_per_share"]
                enriched = dict(row)
                enriched["_max_tradable_shares"] = max_shares
                enriched["_capacity_value"] = capacity_value
                if row["_duration_bucket"] == "out-of-range":
                    continue
                rows_by_combo[row["_bucket_key"]].append(enriched)
                rows_by_bucket[row["_duration_bucket"]].append(enriched)
                total_capacity += capacity_value

            combo_rows = []
            messages = []
            for combo in sorted(combo_targets):
                result = diagnose_combo(
                    combo,
                    rows_by_combo.get(combo, []),
                    combo_targets[combo],
                    args.min_securities_per_combo,
                )
                messages.extend(result.pop("_messages"))
                combo_rows.append(result)

            bucket_rows = []
            for bucket in sorted(bucket_targets):
                bucket_rows.append(
                    diagnose_bucket(bucket, rows_by_bucket.get(bucket, []), bucket_targets[bucket])
                )

            value_capacity_pass = total_capacity + 1e-9 >= target_value
            name_count_pass = all(row["Name Count Pass"] for row in combo_rows)
            duration_reachability_pass = all(row["Duration Reachable"] for row in combo_rows)
            amount_pass = all(row["Amount Pass"] for row in combo_rows)
            worst_shortfall = max(
                combo_rows,
                key=lambda row: parse_number(row["Capacity Shortfall"]),
            )

            if not messages:
                messages.append(
                    f"{side} {pnu:g} PNU: no obvious hard inventory bottleneck found. "
                    "Optimization can still fail because of round lots, minimum-name constraints, or cross-bucket trade-offs."
                )

            prefix = f"inventory_sufficiency_{side}_{pnu:g}pnu".replace(".", "p")
            combo_path = args.output_dir / f"{prefix}_by_combo.csv"
            bucket_path = args.output_dir / f"{prefix}_by_bucket.csv"
            message_path = args.output_dir / f"{prefix}_messages.txt"

            write_csv(combo_path, combo_rows, [key for key in combo_rows[0].keys()])
            write_csv(bucket_path, bucket_rows, [key for key in bucket_rows[0].keys()])
            message_path.write_text("\n".join(messages) + "\n", encoding="utf-8")

            written_files.extend([combo_path, bucket_path, message_path])
            all_summary_rows.append({
                "Side": side,
                "PNU": f"{pnu:g}",
                "Target Value": fmt_money(target_value),
                "Total Capacity Value": fmt_money(total_capacity),
                "Benchmark Duration": f"{bmk_duration:.4f}",
                "Overall Capacity Pass": value_capacity_pass,
                "All Combo Amount Pass": amount_pass,
                "All Combo Name Count Pass": name_count_pass,
                "All Combo Duration Reachable": duration_reachability_pass,
                "Worst Capacity Combo": worst_shortfall["Combo"],
                "Worst Capacity Shortfall": worst_shortfall["Capacity Shortfall"],
                "Message File": str(message_path),
            })

    summary_path = args.output_dir / "inventory_sufficiency_summary.csv"
    write_csv(summary_path, all_summary_rows, [key for key in all_summary_rows[0].keys()])
    written_files.insert(0, summary_path)
    return written_files


def main():
    parser = argparse.ArgumentParser(description="Diagnose Dealer Inventory sufficiency by PNU.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--pnu-list", default="1,3,5,6,10,20")
    parser.add_argument("--side", choices=["create", "redemption", "both"], default="both")
    parser.add_argument("--units-per-pnu", type=float, default=DEFAULT_UNITS_PER_PNU)
    parser.add_argument("--nav", type=float, default=DEFAULT_NAV_CAD)
    parser.add_argument("--min-securities-per-combo", type=int, default=DEFAULT_MIN_SECURITIES_PER_COMBO)
    args = parser.parse_args()

    args.pnu_list = parse_pnu_list(args.pnu_list)
    args.sides = ["create", "redemption"] if args.side == "both" else [args.side]

    written_files = run_diagnostic(args)
    print("Wrote inventory sufficiency diagnostics:")
    for path in written_files:
        print(path)


if __name__ == "__main__":
    main()
