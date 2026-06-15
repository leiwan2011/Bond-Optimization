#!/usr/bin/env python3
"""
Build a create/redemption trade list for XBB using available Dealer Inventory.

This script is intentionally dependency-free: it uses only the Python standard
library so it can be copied into an office environment without installing an
optimization package. The optimizer is a deterministic greedy/search heuristic:
it builds a basket by benchmark-weighted sector-duration combinations, then
repairs the basket against value and duration constraints one 1,000-share round
lot at a time.

Default assumptions:
- Positive Cash Spend/Raise means create/buy.
- Negative Cash Spend/Raise means redemption/sell.
- Positive Trade Shares means create/buy.
- Negative Trade Shares means redemption/sell.
"""

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path


# =============================================================================
# CONFIGURATION SECTION - EDIT THIS BLOCK WHEN MOVING TO A NEW ENVIRONMENT
# =============================================================================
#
# 1) Trade assumptions
#    These are the main parameters most users change between runs.
#    Positive = create/spend cash. Negative = redemption/raise cash.
DEFAULT_CASH_SPEND_RAISE = 1_411_000.0
DEFAULT_MAX_VALUE_GAP_CAD = 300.0
DEFAULT_MAX_GLOBAL_DURATION_GAP = 0.1
DEFAULT_MAX_BUCKET_DURATION_GAP = 0.2
DEFAULT_MIN_SECURITIES_PER_COMBO = 2
ISSUED_AMOUNT_MULTIPLIER = 1000
MAX_TRADE_FRACTION_OF_ISSUED_AMOUNT = 0.5

# 2) File paths
#    Change these defaults if your office folder structure is different. You can
#    also override them from the command line with --input and --output-dir.
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "Output" / "XBB_holdings_with_dealer_inventory.csv"
DEFAULT_OUTPUT_DIR = ROOT / "Output"

# 3) Input table column mapping
#    Left side = internal variable used by this script.
#    Right side = actual column name in your input file.
#    When your office file uses different headers, change only the right side.
COLUMN_MAPPING = {
    # Required numeric fields
    "shares": "Shares",
    "duration": "Duration",
    "dealer_inventory": "Dealer Inventory",
    "bmk_weight": "Bmk Weight",
    "issued_amount": "Issued Amount",
    "price": "Price",
    "ticker": "Ticker",

    # Required classification field
    "sector": "Sector",

    # Optional descriptive fields used only in output files
    "name": "Name",
    "maturity": "Maturity",
    "coupon": "Coupon (%)",
}

# 4) Output column names
#    Change these only if you want different headers in the generated trade list.
OUTPUT_COLUMNS = {
    "ticker": "Ticker",
    "name": "Name",
    "sector": "Sector",
    "sector_group": "Sector Group",
    "duration_bucket": "Duration Bucket",
    "duration": "Duration",
    "price": "Price",
    "current_shares": "Current Shares",
    "dealer_inventory": "Dealer Inventory",
    "trade_shares": "Trade Shares",
    "trade_market_value": "Trade Market Value",
    "post_trade_shares": "Post-Trade Shares",
    "maturity": "Maturity",
    "coupon": "Coupon (%)",
}

# 5) Sector grouping rules
#    The script maps raw sector names into these three optimization groups.
#    Edit the sets if your office file uses names like "Government", "Prov", etc.
FEDERAL_SECTORS = {"federal"}
GOV_SECTORS = {"provincial", "municipal"}
DEFAULT_SECTOR_GROUP = "corporate"

# 6) Round lot and duration bucket definitions
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
    """Convert spreadsheet-style numeric strings into floats.

    The holdings files often store numbers as strings with commas, for example
    "1,234,000.00". Blank values are treated as 0.0 so downstream calculations
    do not crash on optional or missing cells.
    """
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    return float(text)


def fmt_money(value):
    """Format a numeric CAD amount for human-readable CSV output."""
    return f"{value:,.2f}"


def fmt_shares(value):
    """Format share quantities as whole numbers for output files."""
    return f"{int(round(value))}"


def input_value(row, field_name, default=""):
    """Read a value from an input row using COLUMN_MAPPING.

    All office-specific column names should be handled by COLUMN_MAPPING at the
    top of the file. The rest of the script asks for stable internal names such
    as "shares", "duration", or "dealer_inventory".
    """
    column_name = COLUMN_MAPPING[field_name]
    return row.get(column_name, default)


def validate_required_columns(fieldnames):
    """Fail early if the input CSV does not contain required mapped columns."""
    required_fields = [
        "shares",
        "duration",
        "dealer_inventory",
        "bmk_weight",
        "issued_amount",
        "price",
        "ticker",
        "sector",
    ]
    missing = [
        COLUMN_MAPPING[field]
        for field in required_fields
        if COLUMN_MAPPING[field] not in fieldnames
    ]
    if missing:
        raise ValueError(
            "Input file is missing required column(s): "
            + ", ".join(missing)
            + ". Update COLUMN_MAPPING at the top of this script if your file uses different headers."
        )


def sector_group(sector):
    """Map the raw sector value into one optimization group.

    The model uses exactly three sector groups:
    - federal
    - gov, which combines provincial and municipal
    - corporate, which is the fallback for everything else
    """
    normalized = str(sector).strip().lower()
    if normalized in FEDERAL_SECTORS:
        return "federal"
    if normalized in GOV_SECTORS:
        return "gov"
    return DEFAULT_SECTOR_GROUP


def duration_bucket(duration):
    """Assign a numeric duration to one configured duration bucket."""
    for name, low, high, include_high in DURATION_BUCKETS:
        if duration >= low and (duration < high or (include_high and duration <= high)):
            return name
    return "out-of-range"


def read_holdings(path):
    """Load the holdings CSV and enrich each row with internal numeric fields.

    The original row is preserved so descriptive output fields can still be
    written later. Internal keys beginning with "_" are added for calculations,
    for example _shares, _duration, _sector_group, and _bucket_key.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        validate_required_columns(reader.fieldnames or [])
        rows = list(reader)

    holdings = []
    for row_number, row in enumerate(rows, start=2):
        shares = parse_number(input_value(row, "shares"))
        duration = parse_number(input_value(row, "duration"))
        dealer_inventory = parse_number(input_value(row, "dealer_inventory"))
        bmk_weight = parse_number(input_value(row, "bmk_weight"))
        issued_amount = parse_number(input_value(row, "issued_amount"))
        price = parse_number(input_value(row, "price"))
        if price <= 0:
            continue

        price_per_share = price
        group = sector_group(input_value(row, "sector"))
        bucket = duration_bucket(duration)
        row["_row_number"] = row_number
        row["_shares"] = shares
        row["_duration"] = duration
        row["_dealer_inventory"] = dealer_inventory
        row["_bmk_weight"] = bmk_weight
        row["_issued_amount"] = issued_amount
        row["_price"] = price
        row["_price_per_share"] = price_per_share
        row["_sector_group"] = group
        row["_duration_bucket"] = bucket
        row["_bucket_key"] = f"{group}|{bucket}"
        holdings.append(row)

    return holdings


def weighted_duration(rows, shares_key="_shares"):
    """Calculate value-weighted duration for a list of holdings or trades."""
    total_value = 0.0
    total_duration_value = 0.0
    for row in rows:
        shares = row[shares_key]
        value = shares * row["_price_per_share"]
        total_value += value
        total_duration_value += value * row["_duration"]
    return total_duration_value / total_value if total_value else 0.0


def benchmark_weighted_duration(rows):
    """Calculate benchmark-weighted duration from the Bmk Weight column."""
    total_weight = 0.0
    total_duration_weight = 0.0
    for row in rows:
        weight = row["_bmk_weight"]
        total_weight += weight
        total_duration_weight += weight * row["_duration"]
    return total_duration_weight / total_weight if total_weight else 0.0


def duration_bucket_targets(holdings, total_trade_value):
    """Build benchmark-weighted target value and duration for each bucket.

    If 0-5 years is 46% of the benchmark weight, then 46% of the
    create/redemption notional is assigned to the 0-5 trade bucket. Duration is
    also calculated from Bmk Weight, not from current market value.
    """
    buckets = defaultdict(lambda: {"bmk_weight": 0.0, "duration_weight": 0.0})
    total_bmk_weight = 0.0
    for row in holdings:
        bucket = row["_duration_bucket"]
        if bucket == "out-of-range":
            continue
        bmk_weight = row["_bmk_weight"]
        total_bmk_weight += bmk_weight
        buckets[bucket]["bmk_weight"] += bmk_weight
        buckets[bucket]["duration_weight"] += bmk_weight * row["_duration"]

    targets = {}
    for bucket, stats in buckets.items():
        weight = stats["bmk_weight"] / total_bmk_weight if total_bmk_weight else 0.0
        targets[bucket] = {
            "weight": weight,
            "target_value": total_trade_value * weight,
            "target_duration": stats["duration_weight"] / stats["bmk_weight"],
        }
    return targets


def sector_duration_targets(holdings, total_trade_value):
    """Build benchmark-weighted target notional and duration for each combo.

    This is the main basket construction level. For example, "gov|5-10" gets
    its own target value and target duration based on benchmark weights in the
    full tracking portfolio's gov 5-10 holdings.
    """
    combos = defaultdict(lambda: {"bmk_weight": 0.0, "duration_weight": 0.0})
    total_bmk_weight = 0.0
    for row in holdings:
        if row["_duration_bucket"] == "out-of-range":
            continue
        bmk_weight = row["_bmk_weight"]
        total_bmk_weight += bmk_weight
        combo = row["_bucket_key"]
        combos[combo]["bmk_weight"] += bmk_weight
        combos[combo]["duration_weight"] += bmk_weight * row["_duration"]

    targets = {}
    for combo, stats in combos.items():
        weight = stats["bmk_weight"] / total_bmk_weight if total_bmk_weight else 0.0
        targets[combo] = {
            "sector_group": combo.split("|", 1)[0],
            "duration_bucket": combo.split("|", 1)[1],
            "weight": weight,
            "target_value": total_trade_value * weight,
            "target_duration": stats["duration_weight"] / stats["bmk_weight"],
        }
    return targets


def build_candidates(holdings, side, require_dealer_inventory):
    """Create the list of securities that are eligible for optimization.

    For create, the maximum trade size is dealer inventory.
    For redemption, the maximum trade size is the smaller of current holdings
    and dealer inventory, so post-trade shares cannot go below zero.
    Both sides are also capped by issued amount:
    abs(trade shares) <= Issued Amount * ISSUED_AMOUNT_MULTIPLIER
                         * MAX_TRADE_FRACTION_OF_ISSUED_AMOUNT.
    """
    candidates = []
    for row in holdings:
        inventory = math.floor(row["_dealer_inventory"] / ROUND_LOT) * ROUND_LOT
        if require_dealer_inventory and inventory < ROUND_LOT:
            continue

        if side == "create":
            max_shares = inventory if require_dealer_inventory else math.inf
        else:
            max_by_position = math.floor(row["_shares"] / ROUND_LOT) * ROUND_LOT
            max_shares = max_by_position
            if require_dealer_inventory:
                max_shares = min(max_shares, inventory)

        issued_amount_cap = (
            row["_issued_amount"]
            * ISSUED_AMOUNT_MULTIPLIER
            * MAX_TRADE_FRACTION_OF_ISSUED_AMOUNT
        )
        issued_amount_cap = math.floor(issued_amount_cap / ROUND_LOT) * ROUND_LOT
        max_shares = min(max_shares, issued_amount_cap)

        if max_shares < ROUND_LOT:
            continue

        candidates.append({
            "row": row,
            "max_lots": int(max_shares // ROUND_LOT),
            "lot_value": ROUND_LOT * row["_price_per_share"],
            "duration": row["_duration"],
            "bucket_key": row["_bucket_key"],
        })
    return candidates


def filter_combo_targets_for_available_inventory(candidates, combo_targets, min_securities_per_combo):
    """Keep only sector-duration combos that can support combo-level rules.

    A portfolio may contain benchmark weight in a sector-duration combo where
    the dealer provides no eligible inventory. In that case the optimizer should
    still run: it drops only the most granular combo-level coverage rule for
    that combo, while keeping the broader duration-bucket and global targets.
    """
    eligible_row_ids = defaultdict(set)
    for candidate in candidates:
        combo = candidate["row"]["_bucket_key"]
        if combo in combo_targets:
            eligible_row_ids[combo].add(candidate["row"]["_row_number"])

    active_targets = {}
    skipped_targets = []
    for combo, target in sorted(combo_targets.items()):
        eligible_count = len(eligible_row_ids.get(combo, set()))
        if eligible_count >= min_securities_per_combo:
            active_targets[combo] = target
            continue

        skipped_targets.append({
            "combo": combo,
            "sector_group": target["sector_group"],
            "duration_bucket": target["duration_bucket"],
            "eligible_security_count": eligible_count,
            "required_security_count": min_securities_per_combo,
            "target_market_value_cad": round(target["target_value"], 2),
            "target_duration": round(target["target_duration"], 6),
            "reason": (
                "no eligible Dealer Inventory"
                if eligible_count == 0
                else "not enough eligible Dealer Inventory names"
            ),
        })

    return active_targets, skipped_targets


def portfolio_stats(trades):
    """Return total market value and value-weighted duration of a trade basket."""
    total_value = sum(t["shares_abs"] * t["row"]["_price_per_share"] for t in trades.values())
    total_duration_value = sum(
        t["shares_abs"] * t["row"]["_price_per_share"] * t["row"]["_duration"]
        for t in trades.values()
    )
    duration = total_duration_value / total_value if total_value else 0.0
    return total_value, duration


def post_trade_portfolio_duration(holdings, trades, side):
    """Calculate portfolio duration after applying create/redemption trades.

    Create adds the selected trade shares to the portfolio. Redemption subtracts
    them. Prices are held constant from the input file's Price column.
    """
    sign = 1 if side == "create" else -1
    signed_trades = {
        row_id: sign * trade["shares_abs"]
        for row_id, trade in trades.items()
    }

    total_value = 0.0
    total_duration_value = 0.0
    for row in holdings:
        post_trade_shares = row["_shares"] + signed_trades.get(row["_row_number"], 0)
        if post_trade_shares <= 0:
            continue
        value = post_trade_shares * row["_price_per_share"]
        total_value += value
        total_duration_value += value * row["_duration"]

    return total_duration_value / total_value if total_value else 0.0


def bucket_stats(trades):
    """Aggregate a trade basket by duration bucket."""
    buckets = defaultdict(lambda: {"market_value": 0.0, "duration_value": 0.0, "securities": 0})
    for trade in trades.values():
        row = trade["row"]
        value = trade["shares_abs"] * row["_price_per_share"]
        item = buckets[row["_duration_bucket"]]
        item["market_value"] += value
        item["duration_value"] += value * row["_duration"]
        item["securities"] += 1
    return buckets


def duration_bucket_gaps(trades, targets):
    """Return actual minus target duration for each duration bucket."""
    gaps = {}
    for bucket, stats in bucket_stats(trades).items():
        if stats["market_value"] <= 0 or bucket not in targets:
            continue
        duration = stats["duration_value"] / stats["market_value"]
        gaps[bucket] = duration - targets[bucket]["target_duration"]
    return gaps


def sector_duration_gaps(trades, targets):
    """Return actual minus target duration for each sector-duration combo."""
    combos = defaultdict(lambda: {"market_value": 0.0, "duration_value": 0.0, "securities": 0})
    for trade in trades.values():
        row = trade["row"]
        value = trade["shares_abs"] * row["_price_per_share"]
        item = combos[row["_bucket_key"]]
        item["market_value"] += value
        item["duration_value"] += value * row["_duration"]
        item["securities"] += 1

    gaps = {}
    for combo, stats in combos.items():
        if stats["market_value"] <= 0 or combo not in targets:
            continue
        duration = stats["duration_value"] / stats["market_value"]
        gaps[combo] = duration - targets[combo]["target_duration"]
    return gaps


def constraints_pass(trades, target_duration, bucket_targets, max_global_gap, max_bucket_gap):
    """Check only the global and duration-bucket duration constraints."""
    _, actual_duration = portfolio_stats(trades)
    if abs(actual_duration - target_duration) > max_global_gap:
        return False
    gaps = duration_bucket_gaps(trades, bucket_targets)
    required_buckets = set(bucket_targets)
    if set(gaps) != required_buckets:
        return False
    return all(abs(gap) <= max_bucket_gap for gap in gaps.values())


def min_securities_per_combo_pass(trades, combo_targets, min_securities_per_combo):
    """Check that every sector-duration combo has enough distinct securities."""
    counts = defaultdict(int)
    for trade in trades.values():
        counts[trade["row"]["_bucket_key"]] += 1
    return all(counts[combo] >= min_securities_per_combo for combo in combo_targets)


def combo_security_counts(trades):
    """Count selected securities by sector-duration combo."""
    counts = defaultdict(int)
    for trade in trades.values():
        counts[trade["row"]["_bucket_key"]] += 1
    return counts


def constraint_score(
    trades,
    target_value,
    target_duration,
    bucket_targets,
    combo_targets,
    max_global_gap,
    max_bucket_gap,
    max_value_gap,
    min_securities_per_combo,
):
    """Score a basket for final repair using hard-constraint priority order.

    Python compares tuples from left to right. That lets us express business
    priority cleanly:
    1. value gap violation beyond max_value_gap
    2. global duration violation
    3. duration-bucket violation
    4. minimum-security violation
    5. smaller absolute value/duration gaps
    6. fewer securities

    This is not a mathematical optimizer; it is a deterministic heuristic that
    chooses the next add/remove round lot only when the tuple improves.
    """
    value, duration = portfolio_stats(trades)
    value_gap = value - target_value
    global_gap = duration - target_duration
    bucket_gaps = duration_bucket_gaps(trades, bucket_targets)
    counts = combo_security_counts(trades)

    value_violation = max(0.0, abs(value_gap) - max_value_gap)
    global_violation = max(0.0, abs(global_gap) - max_global_gap)
    bucket_violation = sum(
        max(0.0, abs(bucket_gaps.get(bucket, math.inf)) - max_bucket_gap)
        for bucket in bucket_targets
    )
    min_security_violation = sum(
        max(0, min_securities_per_combo - counts[combo])
        for combo in combo_targets
    )
    max_bucket_abs_gap = max(
        (abs(bucket_gaps.get(bucket, math.inf)) for bucket in bucket_targets),
        default=math.inf,
    )

    return (
        round(value_violation, 6),
        round(global_violation, 6),
        round(bucket_violation, 6),
        min_security_violation,
        round(abs(value_gap), 6),
        round(abs(global_gap), 9),
        round(max_bucket_abs_gap, 9),
        len(trades),
    )


def score_after_add(trades, candidate, target_value, target_duration, selected_keys):
    """Score adding one round lot during the older broad greedy helper.

    The current main flow uses sector-duration combo construction first. This
    helper remains useful in repair paths where a simple "what if we add this
    lot?" comparison is needed.
    """
    current_shares = trades.get(candidate["row"]["_row_number"], {}).get("shares_abs", 0)
    new_value = sum(t["shares_abs"] * t["row"]["_price_per_share"] for t in trades.values())
    new_duration_value = sum(
        t["shares_abs"] * t["row"]["_price_per_share"] * t["row"]["_duration"]
        for t in trades.values()
    )

    add_value = candidate["lot_value"]
    add_duration_value = add_value * candidate["duration"]
    after_value = new_value + add_value
    after_duration = (new_duration_value + add_duration_value) / after_value

    duration_gap = abs(after_duration - target_duration)
    value_gap = abs(after_value - target_value) / target_value
    overshoot = max(0.0, after_value - target_value) / target_value
    new_security_penalty = 0.0025 if current_shares == 0 else 0.0
    coverage_bonus = -0.001 if candidate["bucket_key"] not in selected_keys else 0.0

    return (
        value_gap * 0.20
        + overshoot * 2.00
        + duration_gap
        + new_security_penalty
        + coverage_bonus
    )


def score_after_add_in_duration_bucket(trades, candidate, target_value, target_duration):
    """Score adding one round lot inside one duration bucket or combo."""
    current_shares = trades.get(candidate["row"]["_row_number"], {}).get("shares_abs", 0)
    current_value, _ = portfolio_stats(trades)
    current_duration_value = sum(
        t["shares_abs"] * t["row"]["_price_per_share"] * t["row"]["_duration"]
        for t in trades.values()
    )

    add_value = candidate["lot_value"]
    after_value = current_value + add_value
    after_duration = (current_duration_value + add_value * candidate["duration"]) / after_value

    duration_gap = abs(after_duration - target_duration)
    value_gap = abs(after_value - target_value) / target_value
    overshoot = max(0.0, after_value - target_value) / target_value
    new_security_penalty = 0.015 if current_shares == 0 else 0.0

    return duration_gap * 3.0 + value_gap * 0.35 + overshoot * 1.25 + new_security_penalty


def add_lot(trades, candidate):
    """Add exactly one ROUND_LOT to a trade basket for one candidate security."""
    row_id = candidate["row"]["_row_number"]
    if row_id not in trades:
        trades[row_id] = {"row": candidate["row"], "shares_abs": 0}
    trades[row_id]["shares_abs"] += ROUND_LOT


def seed_bucket_coverage(candidates, target_value, target_duration):
    """Legacy helper: seed one security per sector-duration bucket.

    The main optimizer now enforces at least two securities per sector-duration
    combo, so this function is kept only for the older optimize_trade helper.
    """
    trades = {}
    capacity_used = defaultdict(int)
    selected_keys = set()
    buckets = defaultdict(list)
    for candidate in candidates:
        buckets[candidate["bucket_key"]].append(candidate)

    for key in sorted(buckets):
        current_value, _ = portfolio_stats(trades)
        if current_value >= target_value * 0.98:
            break
        available = [c for c in buckets[key] if capacity_used[c["row"]["_row_number"]] < c["max_lots"]]
        if not available:
            continue
        candidate = min(
            available,
            key=lambda c: (
                abs(c["duration"] - target_duration),
                c["lot_value"],
                input_value(c["row"], "ticker"),
                input_value(c["row"], "name"),
            ),
        )
        add_lot(trades, candidate)
        capacity_used[candidate["row"]["_row_number"]] += 1
        selected_keys.add(candidate["bucket_key"])

    return trades, capacity_used, selected_keys


def optimize_trade(candidates, target_value, target_duration):
    """Legacy broad greedy optimizer kept for reference.

    The production path below is optimize_trade_with_duration_constraints().
    This older function builds one basket against the overall target duration
    and does not enforce the newer sector-duration minimum-security rule.
    """
    if not candidates:
        raise ValueError("No eligible securities with usable Dealer Inventory.")

    trades, capacity_used, selected_keys = seed_bucket_coverage(
        candidates, target_value, target_duration
    )

    max_iterations = int(target_value / 500) + 10000
    for _ in range(max_iterations):
        current_value, _ = portfolio_stats(trades)
        if current_value >= target_value:
            break

        feasible = [
            c for c in candidates
            if capacity_used[c["row"]["_row_number"]] < c["max_lots"]
        ]
        if not feasible:
            break

        candidate = min(
            feasible,
            key=lambda c: score_after_add(trades, c, target_value, target_duration, selected_keys),
        )
        add_lot(trades, candidate)
        capacity_used[candidate["row"]["_row_number"]] += 1
        selected_keys.add(candidate["bucket_key"])

    # If the last lot overshot and removing it improves target value without badly hurting duration, remove it.
    improved = True
    while improved:
        improved = False
        base_value, base_duration = portfolio_stats(trades)
        base_score = abs(base_value - target_value) / target_value * 0.20 + abs(base_duration - target_duration)
        for row_id, trade in list(trades.items()):
            if trade["shares_abs"] <= ROUND_LOT:
                continue
            trade["shares_abs"] -= ROUND_LOT
            new_value, new_duration = portfolio_stats(trades)
            new_score = abs(new_value - target_value) / target_value * 0.20 + abs(new_duration - target_duration)
            if new_score + 1e-12 < base_score:
                improved = True
                break
            trade["shares_abs"] += ROUND_LOT

    return trades


def optimize_duration_bucket(candidates, target_value, target_duration, max_bucket_gap, min_securities=1):
    """Optimize one sector-duration combo or one duration bucket.

    In normal use this receives candidates for a single combo, such as
    "corporate|5-10". It searches one-security or two-security combinations in
    1,000-share round lots, balancing target notional and target duration while
    respecting each security's max_lots capacity.
    """
    if not candidates:
        raise ValueError("No candidates available for one duration bucket.")

    def candidate_to_trade(candidate, lots):
        """Build a temporary trade dict for one security and a lot count."""
        return {
            candidate["row"]["_row_number"]: {
                "row": candidate["row"],
                "shares_abs": lots * ROUND_LOT,
            }
        }

    def pair_to_trade(first, first_lots, second, second_lots):
        """Build a temporary trade dict for a two-security combination."""
        trades = {}
        if first_lots > 0:
            trades[first["row"]["_row_number"]] = {
                "row": first["row"],
                "shares_abs": first_lots * ROUND_LOT,
            }
        if second_lots > 0:
            trades[second["row"]["_row_number"]] = {
                "row": second["row"],
                "shares_abs": trades.get(second["row"]["_row_number"], {"shares_abs": 0})["shares_abs"]
                    + second_lots * ROUND_LOT,
            }
        return trades

    def score(trades):
        """Score one combo-level candidate basket.

        The minimum-security shortfall is deliberately huge so the search
        prefers two names when min_securities=2. Duration comes before value at
        this local level; the final global repair enforces the hard value gap.
        """
        value, duration = portfolio_stats(trades)
        value_gap = abs(value - target_value) / target_value
        duration_gap = abs(duration - target_duration)
        over_bucket_limit = max(0.0, duration_gap - max_bucket_gap)
        security_count = len(trades)
        min_security_shortfall = max(0, min_securities - security_count)
        return (
            min_security_shortfall * 10000.0
            + over_bucket_limit * 1000.0
            + duration_gap * 10.0
            + value_gap
            + security_count * 0.0001
        )

    best = None
    best_score = math.inf

    if min_securities <= 1:
        for candidate in candidates:
            ideal_lots = target_value / candidate["lot_value"]
            lot_options = {
                max(1, min(candidate["max_lots"], int(math.floor(ideal_lots)))),
                max(1, min(candidate["max_lots"], int(round(ideal_lots)))),
                max(1, min(candidate["max_lots"], int(math.ceil(ideal_lots)))),
            }
            for lots in lot_options:
                trades = candidate_to_trade(candidate, lots)
                candidate_score = score(trades)
                if candidate_score < best_score:
                    best = trades
                    best_score = candidate_score

    # Try pairs ordered by closeness to target duration. Pairing one shorter
    # bond with one longer bond often matches the combo's target duration better
    # than using only the closest single bond.
    sorted_candidates = sorted(
        candidates,
        key=lambda c: (abs(c["duration"] - target_duration), c["lot_value"]),
    )
    for index, first in enumerate(sorted_candidates):
        for second in sorted_candidates[index + 1:]:
            d1 = first["duration"]
            d2 = second["duration"]
            if d1 == d2:
                first_fraction = 0.5
            else:
                first_fraction = (d2 - target_duration) / (d2 - d1)
            first_fraction = min(1.0, max(0.0, first_fraction))
            second_fraction = 1.0 - first_fraction

            ideal_first_lots = target_value * first_fraction / first["lot_value"]
            ideal_second_lots = target_value * second_fraction / second["lot_value"]

            def lot_options(candidate, centers):
                """Return practical lot counts around useful search centers.

                Centers include the theoretical duration-matching mix, capacity
                limits, and value-balancing points. The +/- 25 window keeps the
                search small while still giving the heuristic room to repair
                round-lot and inventory effects.
                """
                options = {1, candidate["max_lots"]}
                for center in centers:
                    clipped = max(1, min(candidate["max_lots"], int(round(center))))
                    for lots in range(clipped - 25, clipped + 26):
                        if 1 <= lots <= candidate["max_lots"]:
                            options.add(lots)
                return sorted(options)

            first_centers = [ideal_first_lots]
            first_clipped = max(1, min(first["max_lots"], int(round(ideal_first_lots))))
            second_after_first = (target_value - first_clipped * first["lot_value"]) / second["lot_value"]
            first_after_second_max = (target_value - second["max_lots"] * second["lot_value"]) / first["lot_value"]
            first_centers.extend([first_clipped, first_after_second_max])

            second_centers = [ideal_second_lots, second_after_first]
            second_clipped = max(1, min(second["max_lots"], int(round(ideal_second_lots))))
            first_after_second = (target_value - second_clipped * second["lot_value"]) / first["lot_value"]
            second_after_first_max = (target_value - first["max_lots"] * first["lot_value"]) / second["lot_value"]
            first_centers.append(first_after_second)
            second_centers.extend([second_clipped, second_after_first_max])

            for first_lots in lot_options(first, first_centers):
                if first_lots < 1 or first_lots > first["max_lots"]:
                    continue
                remaining_value = target_value - first_lots * first["lot_value"]
                second_dynamic_centers = [
                    *second_centers,
                    remaining_value / second["lot_value"],
                ]
                for second_lots in lot_options(second, second_dynamic_centers):
                    if second_lots < 1 or second_lots > second["max_lots"]:
                        continue
                    trades = pair_to_trade(first, first_lots, second, second_lots)
                    candidate_score = score(trades)
                    if candidate_score < best_score:
                        best = trades
                        best_score = candidate_score

    if best is None:
        raise ValueError("Could not build a trade for one duration/sector bucket.")

    return best


def merge_trades(bucket_trades):
    """Merge multiple per-combo trade dictionaries into one basket."""
    merged = {}
    for trades in bucket_trades:
        for row_id, trade in trades.items():
            if row_id not in merged:
                merged[row_id] = {"row": trade["row"], "shares_abs": 0}
            merged[row_id]["shares_abs"] += trade["shares_abs"]
    return merged


def optimize_trade_with_duration_constraints(
    candidates,
    target_value,
    target_duration,
    bucket_targets,
    combo_targets,
    max_global_gap,
    max_bucket_gap,
    max_value_gap,
    min_securities_per_combo,
):
    """Main optimization workflow used by the command-line script.

    Workflow:
    1. Split candidates into sector-duration combos.
    2. For each combo, build a local two-name basket against combo targets.
    3. Merge all combo baskets into one trade basket.
    4. Repair duration constraints if any bucket/global gap is outside limits.
    5. Repair value/duration together with a lexicographic hard-constraint score.

    If all constraints cannot be satisfied, the function still returns the best
    basket found. The JSON summary tells the user exactly which constraints
    passed or failed.
    """
    candidates_by_bucket = defaultdict(list)
    candidates_by_combo = defaultdict(list)
    for candidate in candidates:
        bucket = candidate["row"]["_duration_bucket"]
        if bucket in bucket_targets:
            candidates_by_bucket[bucket].append(candidate)
        combo = candidate["row"]["_bucket_key"]
        if combo in combo_targets:
            candidates_by_combo[combo].append(candidate)

    combo_trades = []
    for combo in sorted(combo_targets):
        target = combo_targets[combo]
        # Build each combo independently first. This guarantees broad coverage
        # before the global repair step starts moving round lots around.
        combo_trades.append(
            optimize_duration_bucket(
                candidates_by_combo[combo],
                target["target_value"],
                target["target_duration"],
                max_bucket_gap,
                min_securities=min_securities_per_combo,
            )
        )

    trades = merge_trades(combo_trades)
    if (
        constraints_pass(trades, target_duration, bucket_targets, max_global_gap, max_bucket_gap)
        and abs(portfolio_stats(trades)[0] - target_value) <= max_value_gap
    ):
        return trades

    # Repair pass 1:
    # Add round lots only when duration constraints are outside their bands.
    # This pass does not target value gap directly; value is handled by the
    # final repair pass below so the hard value tolerance remains explicit.
    for _ in range(20000):
        gaps = duration_bucket_gaps(trades, bucket_targets)
        bad_buckets = [
            bucket for bucket, gap in gaps.items()
            if abs(gap) > max_bucket_gap
        ]
        _, global_duration = portfolio_stats(trades)
        if (
            not bad_buckets
            and abs(global_duration - target_duration) <= max_global_gap
        ):
            break

        if bad_buckets:
            # When a specific duration bucket is out of tolerance, repair within
            # that bucket so we do not accidentally worsen another bucket.
            repair_bucket = max(bad_buckets, key=lambda b: abs(gaps[b]))
            repair_target = bucket_targets[repair_bucket]["target_duration"]
            feasible = candidates_by_bucket[repair_bucket]
        else:
            # If only the global duration is off, all candidates are eligible.
            repair_bucket = None
            repair_target = target_duration
            feasible = candidates

        current_lots = {
            row_id: int(trade["shares_abs"] // ROUND_LOT)
            for row_id, trade in trades.items()
        }
        feasible = [
            c for c in feasible
            if current_lots.get(c["row"]["_row_number"], 0) < c["max_lots"]
        ]
        if not feasible:
            break

        if repair_bucket:
            bucket_only_trades = {
                row_id: trade for row_id, trade in trades.items()
                if trade["row"]["_duration_bucket"] == repair_bucket
            }
            candidate = min(
                feasible,
                key=lambda c: score_after_add_in_duration_bucket(
                    bucket_only_trades,
                    c,
                    bucket_targets[repair_bucket]["target_value"],
                    repair_target,
                ),
            )
        else:
            selected_keys = {t["row"]["_bucket_key"] for t in trades.values()}
            candidate = min(
                feasible,
                key=lambda c: score_after_add(
                    trades, c, target_value, repair_target, selected_keys
                ),
            )
        add_lot(trades, candidate)

    # Repair pass 2:
    # Add or remove one round lot at a time. The score is lexicographic, so the
    # value hard constraint is repaired before duration polish or security count.
    best_score = constraint_score(
        trades,
        target_value,
        target_duration,
        bucket_targets,
        combo_targets,
        max_global_gap,
        max_bucket_gap,
        max_value_gap,
        min_securities_per_combo,
    )
    for _ in range(30000):
        current_lots = {
            row_id: int(trade["shares_abs"] // ROUND_LOT)
            for row_id, trade in trades.items()
        }
        current_counts = combo_security_counts(trades)
        operations = []

        # Adding is allowed if the security still has unused dealer inventory
        # capacity. For redemption, build_candidates already capped this by
        # current shares so post-trade shares cannot become negative.
        for candidate in candidates:
            row_id = candidate["row"]["_row_number"]
            if current_lots.get(row_id, 0) < candidate["max_lots"]:
                operations.append(("add", candidate))

        # Removing is allowed if it does not delete the last lot of a required
        # security, unless the combo still has more than the minimum name count.
        for row_id, trade in trades.items():
            lots = int(trade["shares_abs"] // ROUND_LOT)
            combo = trade["row"]["_bucket_key"]
            if (
                combo not in combo_targets
                or lots > 1
                or current_counts[combo] > min_securities_per_combo
            ):
                operations.append(("remove", row_id))

        best_operation = None
        best_candidate_score = best_score
        for operation, item in operations:
            trial = {
                row_id: {"row": trade["row"], "shares_abs": trade["shares_abs"]}
                for row_id, trade in trades.items()
            }
            if operation == "add":
                add_lot(trial, item)
            else:
                trial[item]["shares_abs"] -= ROUND_LOT
                if trial[item]["shares_abs"] <= 0:
                    del trial[item]

            trial_score = constraint_score(
                trial,
                target_value,
                target_duration,
                bucket_targets,
                combo_targets,
                max_global_gap,
                max_bucket_gap,
                max_value_gap,
                min_securities_per_combo,
            )
            if trial_score < best_candidate_score:
                best_candidate_score = trial_score
                best_operation = (operation, item)

        if best_operation is None:
            break

        operation, item = best_operation
        if operation == "add":
            add_lot(trades, item)
        else:
            trades[item]["shares_abs"] -= ROUND_LOT
            if trades[item]["shares_abs"] <= 0:
                del trades[item]
        best_score = best_candidate_score

    return trades


def summarize_by_duration_bucket(trades, bucket_targets):
    """Create rows for the duration-bucket summary CSV."""
    buckets = defaultdict(lambda: {"securities": 0, "shares_abs": 0.0, "market_value": 0.0, "duration_value": 0.0})
    for trade in trades.values():
        row = trade["row"]
        bucket = buckets[row["_duration_bucket"]]
        value = trade["shares_abs"] * row["_price_per_share"]
        bucket["securities"] += 1
        bucket["shares_abs"] += trade["shares_abs"]
        bucket["market_value"] += value
        bucket["duration_value"] += value * row["_duration"]

    output = []
    for key, item in sorted(buckets.items()):
        duration = item["duration_value"] / item["market_value"] if item["market_value"] else 0.0
        target = bucket_targets.get(key, {})
        target_duration = target.get("target_duration", 0.0)
        target_value = target.get("target_value", 0.0)
        output.append({
            "Duration Bucket": key,
            "Securities": item["securities"],
            "Trade Shares Abs": fmt_shares(item["shares_abs"]),
            "Market Value": fmt_money(item["market_value"]),
            "Target Market Value": fmt_money(target_value),
            "Weighted Duration": f"{duration:.4f}",
            "Target Duration": f"{target_duration:.4f}",
            "Duration Gap": f"{duration - target_duration:.4f}",
        })
    return output


def summarize_by_sector_duration_combo(trades, combo_targets):
    """Create rows for the sector-duration combo summary CSV."""
    combos = defaultdict(lambda: {"securities": 0, "shares_abs": 0.0, "market_value": 0.0, "duration_value": 0.0})
    for trade in trades.values():
        row = trade["row"]
        combo = combos[row["_bucket_key"]]
        value = trade["shares_abs"] * row["_price_per_share"]
        combo["securities"] += 1
        combo["shares_abs"] += trade["shares_abs"]
        combo["market_value"] += value
        combo["duration_value"] += value * row["_duration"]

    output = []
    for key in sorted(combo_targets):
        item = combos[key]
        target = combo_targets[key]
        duration = item["duration_value"] / item["market_value"] if item["market_value"] else 0.0
        output.append({
            "Sector Group": target["sector_group"],
            "Duration Bucket": target["duration_bucket"],
            "Combo": key,
            "Securities": item["securities"],
            "Trade Shares Abs": fmt_shares(item["shares_abs"]),
            "Market Value": fmt_money(item["market_value"]),
            "Target Market Value": fmt_money(target["target_value"]),
            "Weighted Duration": f"{duration:.4f}",
            "Target Duration": f"{target['target_duration']:.4f}",
            "Duration Gap": f"{duration - target['target_duration']:.4f}",
        })
    return output


def summarize_complete_portfolio(holdings, trades, side):
    """Create one output row for every security in the full portfolio.

    The regular trade list only shows securities selected by the optimizer. This
    complete file is an audit view: it keeps every loaded holding and adds the
    calculated Sector Group, Duration Bucket, Trade Shares, and Post-Trade
    Shares so users can inspect classification results row by row.
    """
    sign = 1 if side == "create" else -1
    signed_trade_shares = {
        row_id: sign * trade["shares_abs"]
        for row_id, trade in trades.items()
    }

    output = []
    for row in sorted(
        holdings,
        key=lambda item: (
            item["_sector_group"],
            item["_duration_bucket"],
            input_value(item, "ticker"),
            input_value(item, "name"),
        ),
    ):
        trade_shares = signed_trade_shares.get(row["_row_number"], 0)
        post_trade_shares = row["_shares"] + trade_shares
        output.append({
            OUTPUT_COLUMNS["ticker"]: input_value(row, "ticker"),
            OUTPUT_COLUMNS["name"]: input_value(row, "name"),
            OUTPUT_COLUMNS["sector"]: input_value(row, "sector"),
            OUTPUT_COLUMNS["sector_group"]: row["_sector_group"],
            OUTPUT_COLUMNS["duration_bucket"]: row["_duration_bucket"],
            OUTPUT_COLUMNS["duration"]: f"{row['_duration']:.4f}",
            "Bmk Weight": f"{row['_bmk_weight']:.8f}",
            OUTPUT_COLUMNS["price"]: input_value(row, "price"),
            OUTPUT_COLUMNS["current_shares"]: fmt_shares(row["_shares"]),
            OUTPUT_COLUMNS["dealer_inventory"]: fmt_shares(row["_dealer_inventory"]),
            "Issued Amount": fmt_shares(row["_issued_amount"]),
            OUTPUT_COLUMNS["trade_shares"]: fmt_shares(trade_shares),
            OUTPUT_COLUMNS["trade_market_value"]: fmt_money(abs(trade_shares) * row["_price_per_share"]),
            OUTPUT_COLUMNS["post_trade_shares"]: fmt_shares(post_trade_shares),
            OUTPUT_COLUMNS["maturity"]: input_value(row, "maturity"),
            OUTPUT_COLUMNS["coupon"]: input_value(row, "coupon"),
        })
    return output


def write_csv(path, rows, fieldnames):
    """Write a list of dictionaries to CSV with a fixed header order."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    """Command-line entry point.

    This function wires together:
    - command-line arguments
    - input loading and target calculation
    - optimization
    - validation/summary metrics
    - CSV/JSON output files
    """
    parser = argparse.ArgumentParser(description="Optimize an XBB create/redemption basket from dealer inventory.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--cash-spend-raise",
        type=float,
        default=DEFAULT_CASH_SPEND_RAISE,
        help="Positive cash means create/spend cash; negative cash means redemption/raise cash.",
    )
    parser.add_argument("--max-global-duration-gap", type=float, default=DEFAULT_MAX_GLOBAL_DURATION_GAP)
    parser.add_argument("--max-bucket-duration-gap", type=float, default=DEFAULT_MAX_BUCKET_DURATION_GAP)
    parser.add_argument("--max-value-gap", type=float, default=DEFAULT_MAX_VALUE_GAP_CAD)
    parser.add_argument("--min-securities-per-combo", type=int, default=DEFAULT_MIN_SECURITIES_PER_COMBO)
    parser.add_argument(
        "--allow-non-inventory",
        action="store_true",
        help="Allow securities without positive Dealer Inventory. Default uses only Dealer Inventory > 0.",
    )
    args = parser.parse_args()
    if abs(args.cash_spend_raise) < 1e-9:
        raise ValueError("cash_spend_raise cannot be zero. Use a positive value for create or a negative value for redemption.")
    side = "create" if args.cash_spend_raise > 0 else "redemption"

    # Load and normalize the source holdings. The optimizer works from enriched
    # internal fields, while the original row values remain available for output.
    holdings = read_holdings(args.input)

    # Overall benchmark target. The trade basket should have a weighted duration
    # close to the Bmk Weight weighted duration, regardless of create/redemption.
    target_duration = benchmark_weighted_duration(holdings)

    # Trade notional target. Positive cash is create/spend cash; negative cash
    # is redemption/raise cash. The optimizer works from absolute market value.
    target_value = abs(args.cash_spend_raise)

    # Split the overall target into duration buckets and sector-duration combos
    # using Bmk Weight, not current market-value weights.
    bucket_targets = duration_bucket_targets(holdings, target_value)
    all_combo_targets = sector_duration_targets(holdings, target_value)

    # Candidate max_lots encodes create/redemption differences:
    # create is capped by dealer inventory; redemption is capped by both current
    # shares and dealer inventory.
    require_dealer_inventory = not args.allow_non_inventory
    candidates = build_candidates(holdings, side, require_dealer_inventory)
    combo_targets, skipped_combo_targets = filter_combo_targets_for_available_inventory(
        candidates,
        all_combo_targets,
        args.min_securities_per_combo,
    )
    suffix_cash = f"{abs(args.cash_spend_raise):,.0f}".replace(",", "")
    suffix = f"{side}_cash_{suffix_cash}"

    # Build the trade basket. If hard constraints cannot all be satisfied, this
    # still returns the best effort basket and the summary below records failures.
    trades = optimize_trade_with_duration_constraints(
        candidates,
        target_value,
        target_duration,
        bucket_targets,
        combo_targets,
        args.max_global_duration_gap,
        args.max_bucket_duration_gap,
        args.max_value_gap,
        args.min_securities_per_combo,
    )

    # Calculate final metrics. These are written to summary.json and are
    # also printed to the terminal for quick review.
    actual_value, actual_duration = portfolio_stats(trades)
    post_trade_duration = post_trade_portfolio_duration(holdings, trades, side)
    bucket_gaps = duration_bucket_gaps(trades, bucket_targets)
    combo_gaps = sector_duration_gaps(trades, combo_targets)
    global_gap = actual_duration - target_duration
    value_gap = actual_value - target_value
    value_pass = abs(value_gap) <= args.max_value_gap
    global_duration_pass = abs(global_gap) <= args.max_global_duration_gap
    bucket_duration_pass = all(
        abs(bucket_gaps.get(bucket, math.inf)) <= args.max_bucket_duration_gap
        for bucket in bucket_targets
    )
    min_combo_security_pass = min_securities_per_combo_pass(
        trades,
        combo_targets,
        args.min_securities_per_combo,
    )
    constraints_passed = (
        value_pass
        and global_duration_pass
        and bucket_duration_pass
        and min_combo_security_pass
    )
    value_constraint_violation = {}
    if not value_pass:
        value_constraint_violation = {
            "gap": round(value_gap, 2),
            "limit": args.max_value_gap,
        }
    duration_constraint_violations = {}
    if not global_duration_pass:
        duration_constraint_violations["global"] = {
            "gap": round(global_gap, 6),
            "limit": args.max_global_duration_gap,
        }
    for bucket in sorted(bucket_targets):
        gap = bucket_gaps.get(bucket, math.inf)
        if abs(gap) > args.max_bucket_duration_gap:
            duration_constraint_violations[bucket] = {
                "gap": round(gap, 6) if math.isfinite(gap) else None,
                "limit": args.max_bucket_duration_gap,
            }
    combo_counts = defaultdict(int)
    for trade in trades.values():
        combo_counts[trade["row"]["_bucket_key"]] += 1
    security_count_violations = {
        combo: combo_counts[combo]
        for combo in sorted(combo_targets)
        if combo_counts[combo] < args.min_securities_per_combo
    }

    # Convert absolute selected shares into signed trade shares:
    # create = positive buy amount; redemption = negative sell amount.
    sign = 1 if side == "create" else -1
    output_rows = []
    for trade in sorted(
        trades.values(),
        key=lambda t: (
            -t["shares_abs"] * t["row"]["_price_per_share"],
            input_value(t["row"], "ticker"),
            input_value(t["row"], "name"),
        ),
    ):
        row = trade["row"]
        trade_shares = sign * trade["shares_abs"]
        post_trade_shares = row["_shares"] + trade_shares
        # This should already be prevented by build_candidates(), but keep the
        # check here as a final safety guard for manual code changes.
        if post_trade_shares < -1e-9:
            raise ValueError(f"Post-trade shares below zero for row {row['_row_number']}.")
        output_rows.append({
            OUTPUT_COLUMNS["ticker"]: input_value(row, "ticker"),
            OUTPUT_COLUMNS["name"]: input_value(row, "name"),
            OUTPUT_COLUMNS["sector"]: input_value(row, "sector"),
            OUTPUT_COLUMNS["sector_group"]: row["_sector_group"],
            OUTPUT_COLUMNS["duration_bucket"]: row["_duration_bucket"],
            OUTPUT_COLUMNS["duration"]: f"{row['_duration']:.4f}",
            OUTPUT_COLUMNS["price"]: input_value(row, "price"),
            OUTPUT_COLUMNS["current_shares"]: fmt_shares(row["_shares"]),
            OUTPUT_COLUMNS["dealer_inventory"]: fmt_shares(row["_dealer_inventory"]),
            OUTPUT_COLUMNS["trade_shares"]: fmt_shares(trade_shares),
            OUTPUT_COLUMNS["trade_market_value"]: fmt_money(trade["shares_abs"] * row["_price_per_share"]),
            OUTPUT_COLUMNS["post_trade_shares"]: fmt_shares(post_trade_shares),
            OUTPUT_COLUMNS["maturity"]: input_value(row, "maturity"),
            OUTPUT_COLUMNS["coupon"]: input_value(row, "coupon"),
        })

    # The summary JSON is the audit trail. It clearly tells users whether the
    # basket passed value, duration, and minimum-security constraints.
    summary = {
        "side": side,
        "cash_spend_raise": round(args.cash_spend_raise, 2),
        "target_market_value_cad": round(target_value, 2),
        "actual_trade_market_value_cad": round(actual_value, 2),
        "market_value_gap_cad": round(value_gap, 2),
        "max_value_gap_cad": args.max_value_gap,
        "value_constraint_pass": value_pass,
        "value_constraint_violation": value_constraint_violation,
        "bmk_duration": round(target_duration, 6),
        "trade_duration": round(actual_duration, 6),
        "post_trade_portfolio_duration": round(post_trade_duration, 6),
        "target_portfolio_duration": round(target_duration, 6),
        "target_duration_source": COLUMN_MAPPING["bmk_weight"],
        "trade_portfolio_duration": round(actual_duration, 6),
        "duration_gap": round(global_gap, 6),
        "max_global_duration_gap": args.max_global_duration_gap,
        "max_bucket_duration_gap": args.max_bucket_duration_gap,
        "duration_bucket_gaps": {k: round(v, 6) for k, v in sorted(bucket_gaps.items())},
        "sector_duration_combo_gaps": {k: round(v, 6) for k, v in sorted(combo_gaps.items())},
        "constraints_pass": constraints_passed,
        "duration_constraints_pass": global_duration_pass and bucket_duration_pass,
        "duration_constraint_violations": duration_constraint_violations,
        "min_securities_per_combo": args.min_securities_per_combo,
        "sector_duration_combo_targets_total": len(all_combo_targets),
        "sector_duration_combo_targets_active": len(combo_targets),
        "sector_duration_combo_targets_skipped": len(skipped_combo_targets),
        "skipped_sector_duration_combos": skipped_combo_targets,
        "min_combo_security_pass": min_combo_security_pass,
        "security_count_violations": security_count_violations,
        "number_of_securities": len(output_rows),
        "dealer_inventory_required": require_dealer_inventory,
        "round_lot": ROUND_LOT,
        "issued_amount_multiplier": ISSUED_AMOUNT_MULTIPLIER,
        "max_trade_fraction_of_issued_amount": MAX_TRADE_FRACTION_OF_ISSUED_AMOUNT,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Output files:
    # 1. trade list by security
    # 2. duration-bucket summary
    # 3. sector-duration combo summary
    # 4. complete portfolio audit file with every security
    # 5. JSON pass/fail summary
    trades_path = args.output_dir / f"XBB_trade_optimization_{suffix}.csv"
    bucket_path = args.output_dir / f"XBB_trade_optimization_{suffix}_bucket_summary.csv"
    combo_path = args.output_dir / f"XBB_trade_optimization_{suffix}_combo_summary.csv"
    complete_path = args.output_dir / f"XBB_trade_optimization_{suffix}_complete_portfolio.csv"
    summary_path = args.output_dir / f"XBB_trade_optimization_{suffix}_summary.json"

    write_csv(trades_path, output_rows, list(output_rows[0].keys()) if output_rows else ["Ticker"])
    bucket_rows = summarize_by_duration_bucket(trades, bucket_targets)
    write_csv(bucket_path, bucket_rows, list(bucket_rows[0].keys()) if bucket_rows else ["Bucket"])
    combo_rows = summarize_by_sector_duration_combo(trades, all_combo_targets)
    write_csv(combo_path, combo_rows, list(combo_rows[0].keys()) if combo_rows else ["Combo"])
    complete_rows = summarize_complete_portfolio(holdings, trades, side)
    write_csv(complete_path, complete_rows, list(complete_rows[0].keys()) if complete_rows else ["Ticker"])
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps({
        "trades": str(trades_path),
        "bucket_summary": str(bucket_path),
        "combo_summary": str(combo_path),
        "complete_portfolio": str(complete_path),
        "summary": str(summary_path),
        **summary,
    }, indent=2))


if __name__ == "__main__":
    main()
