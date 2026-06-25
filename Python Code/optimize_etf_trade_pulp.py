#!/usr/bin/env python3
"""
Build an ETF create/redemption trade list using PuLP mixed-integer optimization.

This is a separate optimizer path from optimize_etf_trade.py. The front
configuration section intentionally matches the existing script so migration is
easy, but the basket construction is modeled as a mixed-integer optimization:

- x_i = number of 1,000-share lots to trade for security i
- y_i = 1 if security i is selected, otherwise 0

Positive Cash Spend/Raise means create/buy.
Negative Cash Spend/Raise means redemption/sell.
"""

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path

try:
    import pulp
except ImportError as exc:
    raise SystemExit(
        "PuLP is required for this script. Install it with:\n"
        '  python -m pip install "pulp[cbc]"\n'
    ) from exc


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
DEFAULT_SOLVER_TIME_LIMIT_SECONDS = 30
DEFAULT_COMBO_VALUE_GAP_WEIGHT = 0.10
DEFAULT_CORPORATE_INDUSTRY_GROUP_VALUE_GAP_WEIGHT = 0.10
DEFAULT_CORPORATE_INDUSTRY_SUBGROUP_VALUE_GAP_WEIGHT = 0.10
DEFAULT_CORPORATE_ISSUER_VALUE_GAP_WEIGHT = 0.10
DEFAULT_CATEGORY_AGENCY_SSA_VALUE_GAP_WEIGHT = 0.10
DEFAULT_QUEBEC_HYDRO_QUEBEC_VALUE_GAP_WEIGHT = 0.10
DEFAULT_ALBERTA_VALUE_GAP_WEIGHT = 0.10
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
    "category": "Category",

    # Required classification field
    "sector": "Sector",

    # Optional descriptive fields used only in output files
    "name": "Name",
    "maturity": "Maturity",
    "coupon": "Coupon (%)",

    # Optional corporate detail fields used by soft market-value penalties
    "industry_group": "industry group",
    "industry_subgroup": "industry subgroup",
    "issuer": "Issuer",
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

# 6) Special subsector market-value penalty definitions
#    Values are compared after lowercase/space/punctuation normalization.
CATEGORY_AGENCY_SSA_VALUES = {"agency", "ssa"}
QUEBEC_HYDRO_QUEBEC_ISSUERS = {
    "province of quebec",
    "quebec (province of)",
    "quebec province of",
    "hydro-quebec",
}
ALBERTA_ISSUERS = {
    "province of alberta",
    "alberta (province of)",
    "alberta province of",
}

# 7) Round lot and duration bucket definitions
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
    """Convert spreadsheet-style numeric strings into floats."""
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
    """Read a value from an input row using COLUMN_MAPPING."""
    return row.get(COLUMN_MAPPING[field_name], default)


def clean_group_value(value):
    """Normalize optional grouping fields used by corporate detail penalties."""
    text = str(value or "").strip()
    return text if text else "unclassified"


def normalize_match_value(value):
    """Normalize category/issuer values for configuration-set matching."""
    text = clean_group_value(value).lower().replace(".", "")
    return re.sub(r"\s+", " ", text).strip()


def normalized_config_values(values):
    """Normalize all values from a configuration set."""
    return {normalize_match_value(value) for value in values}


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
    """Map the raw sector value into federal, gov, or corporate."""
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
    """Load the holdings CSV and enrich each row with internal numeric fields."""
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

        price_per_share = price / 100.0
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
        row["_category"] = clean_group_value(input_value(row, "category", input_value(row, "sector")))
        row["_category_normalized"] = normalize_match_value(row["_category"])
        row["_industry_group"] = clean_group_value(input_value(row, "industry_group"))
        row["_industry_subgroup"] = clean_group_value(input_value(row, "industry_subgroup"))
        row["_issuer"] = clean_group_value(input_value(row, "issuer", input_value(row, "name")))
        row["_issuer_normalized"] = normalize_match_value(row["_issuer"])
        holdings.append(row)

    return holdings


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
    """Build benchmark-weighted target value and duration for each bucket."""
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
    """Build benchmark-weighted target notional and duration for each combo."""
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


def corporate_combo_detail_targets(holdings, total_trade_value, row_field):
    """Build target market value for a corporate detail group inside each combo."""
    total_bmk_weight = 0.0
    detail_weights = defaultdict(float)
    for row in holdings:
        if row["_duration_bucket"] == "out-of-range":
            continue
        bmk_weight = row["_bmk_weight"]
        total_bmk_weight += bmk_weight
        if row["_sector_group"] != DEFAULT_SECTOR_GROUP:
            continue
        key = (row["_bucket_key"], row[row_field])
        detail_weights[key] += bmk_weight

    targets = {}
    for (combo, detail_value), bmk_weight in detail_weights.items():
        weight = bmk_weight / total_bmk_weight if total_bmk_weight else 0.0
        targets[(combo, detail_value)] = {
            "combo": combo,
            "detail_value": detail_value,
            "target_value": total_trade_value * weight,
            "row_field": row_field,
        }
    return targets


def special_subsector_combo_targets(holdings, total_trade_value, group_name, match_field, match_values):
    """Build target value for a configured special subsector inside each combo."""
    total_bmk_weight = 0.0
    group_weights = defaultdict(float)
    normalized_values = normalized_config_values(match_values)

    for row in holdings:
        if row["_duration_bucket"] == "out-of-range":
            continue
        bmk_weight = row["_bmk_weight"]
        total_bmk_weight += bmk_weight
        if row[match_field] not in normalized_values:
            continue
        group_weights[row["_bucket_key"]] += bmk_weight

    targets = {}
    for combo, bmk_weight in group_weights.items():
        weight = bmk_weight / total_bmk_weight if total_bmk_weight else 0.0
        targets[combo] = {
            "group_name": group_name,
            "combo": combo,
            "target_value": total_trade_value * weight,
            "match_field": match_field,
            "match_values": normalized_values,
        }
    return targets


def build_candidates(holdings, side, require_dealer_inventory):
    """Create the list of securities eligible for optimization."""
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

    If a benchmark combo has no eligible dealer inventory, the model should not
    fail immediately. It drops only that granular combo-level coverage rule and
    keeps solving against value, duration-bucket, and global duration targets.
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


def add_abs_gap_constraints(problem, value, name):
    """Create a non-negative variable equal to at least abs(value)."""
    gap = pulp.LpVariable(name, lowBound=0)
    problem += gap >= value, f"{name}_positive_side"
    problem += gap >= -value, f"{name}_negative_side"
    return gap


def add_violation_constraints(problem, abs_gap, allowed_gap, name):
    """Create a non-negative variable for max(0, abs_gap - allowed_gap)."""
    violation = pulp.LpVariable(name, lowBound=0)
    problem += violation >= abs_gap - allowed_gap, f"{name}_above_allowed"
    return violation


def solve_with_pulp(
    candidates,
    target_value,
    target_duration,
    bucket_targets,
    combo_targets,
    corporate_detail_targets,
    special_subsector_targets,
    max_global_gap,
    max_bucket_gap,
    max_value_gap,
    min_securities_per_combo,
    time_limit_seconds,
    combo_value_gap_weight,
    corporate_industry_group_value_gap_weight,
    corporate_industry_subgroup_value_gap_weight,
    corporate_issuer_value_gap_weight,
    category_agency_ssa_value_gap_weight,
    quebec_hydro_quebec_value_gap_weight,
    alberta_value_gap_weight,
):
    """Solve the ETF basket as a mixed-integer optimization model."""
    if not candidates:
        raise ValueError("No eligible securities with usable Dealer Inventory.")

    candidates_by_combo = defaultdict(list)
    candidates_by_bucket = defaultdict(list)
    for idx, candidate in enumerate(candidates):
        candidate["model_index"] = idx
        candidates_by_combo[candidate["row"]["_bucket_key"]].append(candidate)
        candidates_by_bucket[candidate["row"]["_duration_bucket"]].append(candidate)

    problem = pulp.LpProblem("ETF_Trade_Basket", pulp.LpMinimize)
    lots = {}
    selected = {}
    for candidate in candidates:
        idx = candidate["model_index"]
        lots[idx] = pulp.LpVariable(f"lots_{idx}", lowBound=0, upBound=candidate["max_lots"], cat="Integer")
        selected[idx] = pulp.LpVariable(f"selected_{idx}", lowBound=0, upBound=1, cat="Binary")
        problem += lots[idx] <= candidate["max_lots"] * selected[idx], f"link_max_{idx}"
        problem += lots[idx] >= selected[idx], f"link_min_{idx}"

    total_value = pulp.lpSum(candidate["lot_value"] * lots[candidate["model_index"]] for candidate in candidates)
    total_dollar_duration_gap = pulp.lpSum(
        candidate["lot_value"]
        * (candidate["duration"] - target_duration)
        * lots[candidate["model_index"]]
        for candidate in candidates
    )
    value_abs_gap = add_abs_gap_constraints(problem, total_value - target_value, "value_abs_gap")
    value_violation = add_violation_constraints(problem, value_abs_gap, max_value_gap, "value_violation")

    global_abs_gap = add_abs_gap_constraints(problem, total_dollar_duration_gap, "global_dollar_duration_abs_gap")
    global_allowed = max_global_gap * target_value
    global_violation = add_violation_constraints(problem, global_abs_gap, global_allowed, "global_duration_violation")

    bucket_abs_gaps = []
    bucket_violations = []
    for bucket, target in sorted(bucket_targets.items()):
        bucket_gap_expr = pulp.lpSum(
            candidate["lot_value"]
            * (candidate["duration"] - target["target_duration"])
            * lots[candidate["model_index"]]
            for candidate in candidates_by_bucket[bucket]
        )
        abs_gap = add_abs_gap_constraints(problem, bucket_gap_expr, f"bucket_{bucket}_abs_gap")
        violation = add_violation_constraints(
            problem,
            abs_gap,
            max_bucket_gap * target["target_value"],
            f"bucket_{bucket}_duration_violation",
        )
        bucket_abs_gaps.append(abs_gap)
        bucket_violations.append(violation)

    combo_abs_gaps = []
    combo_value_abs_gaps = []
    for combo, target in sorted(combo_targets.items()):
        combo_items = candidates_by_combo[combo]
        problem += (
            pulp.lpSum(selected[item["model_index"]] for item in combo_items)
            >= min_securities_per_combo
        ), f"min_security_count_{combo}"

        combo_value_expr = pulp.lpSum(
            item["lot_value"] * lots[item["model_index"]]
            for item in combo_items
        ) - target["target_value"]
        combo_value_abs_gaps.append(
            add_abs_gap_constraints(
                problem,
                combo_value_expr,
                f"combo_{combo}_value_abs_gap",
            )
        )

        combo_gap_expr = pulp.lpSum(
            item["lot_value"]
            * (item["duration"] - target["target_duration"])
            * lots[item["model_index"]]
            for item in combo_items
        )
        combo_abs_gaps.append(add_abs_gap_constraints(problem, combo_gap_expr, f"combo_{combo}_abs_gap"))

    corporate_detail_value_abs_gaps = {}
    corporate_detail_weights = {
        "industry_group": corporate_industry_group_value_gap_weight,
        "industry_subgroup": corporate_industry_subgroup_value_gap_weight,
        "issuer": corporate_issuer_value_gap_weight,
    }
    for detail_name, detail_targets in sorted(corporate_detail_targets.items()):
        abs_gaps = []
        for target_index, ((combo, detail_value), target) in enumerate(sorted(detail_targets.items())):
            if combo not in combo_targets:
                continue
            row_field = target["row_field"]
            combo_detail_items = [
                item for item in candidates_by_combo[combo]
                if item["row"][row_field] == detail_value
            ]
            if not combo_detail_items:
                continue

            detail_value_expr = pulp.lpSum(
                item["lot_value"] * lots[item["model_index"]]
                for item in combo_detail_items
            ) - target["target_value"]
            abs_gaps.append(
                add_abs_gap_constraints(
                    problem,
                    detail_value_expr,
                    f"corp_{detail_name}_{target_index}_value_abs_gap",
                )
            )
        corporate_detail_value_abs_gaps[detail_name] = abs_gaps

    special_subsector_value_abs_gaps = {}
    special_subsector_weights = {
        "category_agency_or_ssa": category_agency_ssa_value_gap_weight,
        "issuer_quebec_plus_hydro_quebec": quebec_hydro_quebec_value_gap_weight,
        "issuer_province_of_alberta": alberta_value_gap_weight,
    }
    for group_name, targets in sorted(special_subsector_targets.items()):
        abs_gaps = []
        for target_index, (combo, target) in enumerate(sorted(targets.items())):
            if combo not in combo_targets:
                continue
            match_field = target["match_field"]
            match_values = target["match_values"]
            combo_group_items = [
                item for item in candidates_by_combo[combo]
                if item["row"][match_field] in match_values
            ]
            if not combo_group_items:
                continue

            group_value_expr = pulp.lpSum(
                item["lot_value"] * lots[item["model_index"]]
                for item in combo_group_items
            ) - target["target_value"]
            abs_gaps.append(
                add_abs_gap_constraints(
                    problem,
                    group_value_expr,
                    f"special_{group_name}_{target_index}_value_abs_gap",
                )
            )
        special_subsector_value_abs_gaps[group_name] = abs_gaps

    security_count = pulp.lpSum(selected[candidate["model_index"]] for candidate in candidates)
    # Big weights make the objective behave like business priority:
    # first reduce hard-limit violations, then polish gaps, then use fewer names.
    problem += (
        1_000_000 * value_violation
        + 100_000 * global_violation
        + 10_000 * pulp.lpSum(bucket_violations)
        + 10 * value_abs_gap
        + global_abs_gap
        + 0.25 * pulp.lpSum(bucket_abs_gaps)
        + 0.10 * pulp.lpSum(combo_abs_gaps)
        + combo_value_gap_weight * pulp.lpSum(combo_value_abs_gaps)
        + pulp.lpSum(
            corporate_detail_weights[detail_name] * pulp.lpSum(abs_gaps)
            for detail_name, abs_gaps in corporate_detail_value_abs_gaps.items()
        )
        + pulp.lpSum(
            special_subsector_weights[group_name] * pulp.lpSum(abs_gaps)
            for group_name, abs_gaps in special_subsector_value_abs_gaps.items()
        )
        + 1_000 * security_count
    )

    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_seconds)
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]
    if status not in {"Optimal", "Feasible"}:
        raise ValueError(f"PuLP solver did not find a feasible basket. Solver status: {status}.")

    trades = {}
    for candidate in candidates:
        idx = candidate["model_index"]
        solved_lots = int(round(pulp.value(lots[idx]) or 0))
        if solved_lots <= 0:
            continue
        row = candidate["row"]
        trades[row["_row_number"]] = {
            "row": row,
            "shares_abs": solved_lots * ROUND_LOT,
        }

    if not trades:
        raise ValueError("PuLP solver returned no selected trades.")

    return trades, {
        "solver_status": status,
        "objective_value": pulp.value(problem.objective),
        "value_abs_gap_model": pulp.value(value_abs_gap),
        "value_violation_model": pulp.value(value_violation),
        "global_dollar_duration_abs_gap_model": pulp.value(global_abs_gap),
        "global_duration_violation_model": pulp.value(global_violation),
        "combo_value_gap_weight": combo_value_gap_weight,
        "combo_value_abs_gap_model": pulp.value(pulp.lpSum(combo_value_abs_gaps)),
        "corporate_industry_group_value_gap_weight": corporate_industry_group_value_gap_weight,
        "corporate_industry_subgroup_value_gap_weight": corporate_industry_subgroup_value_gap_weight,
        "corporate_issuer_value_gap_weight": corporate_issuer_value_gap_weight,
        "corporate_detail_value_abs_gap_model": {
            detail_name: pulp.value(pulp.lpSum(abs_gaps))
            for detail_name, abs_gaps in sorted(corporate_detail_value_abs_gaps.items())
        },
        "category_agency_ssa_value_gap_weight": category_agency_ssa_value_gap_weight,
        "quebec_hydro_quebec_value_gap_weight": quebec_hydro_quebec_value_gap_weight,
        "alberta_value_gap_weight": alberta_value_gap_weight,
        "special_subsector_value_abs_gap_model": {
            group_name: pulp.value(pulp.lpSum(abs_gaps))
            for group_name, abs_gaps in sorted(special_subsector_value_abs_gaps.items())
        },
    }


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
    """Calculate portfolio duration after applying create/redemption trades."""
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


def min_securities_per_combo_pass(trades, combo_targets, min_securities_per_combo):
    """Check that every sector-duration combo has enough distinct securities."""
    counts = defaultdict(int)
    for trade in trades.values():
        counts[trade["row"]["_bucket_key"]] += 1
    return all(counts[combo] >= min_securities_per_combo for combo in combo_targets)


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
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Optimize an ETF create/redemption basket with PuLP.")
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
    parser.add_argument("--solver-time-limit", type=int, default=DEFAULT_SOLVER_TIME_LIMIT_SECONDS)
    parser.add_argument(
        "--combo-value-gap-weight",
        type=float,
        default=DEFAULT_COMBO_VALUE_GAP_WEIGHT,
        help="Penalty weight for sector-duration combo market value gaps.",
    )
    parser.add_argument(
        "--corporate-industry-group-value-gap-weight",
        type=float,
        default=DEFAULT_CORPORATE_INDUSTRY_GROUP_VALUE_GAP_WEIGHT,
        help="Penalty weight for corporate industry group market value gaps inside each combo.",
    )
    parser.add_argument(
        "--corporate-industry-subgroup-value-gap-weight",
        type=float,
        default=DEFAULT_CORPORATE_INDUSTRY_SUBGROUP_VALUE_GAP_WEIGHT,
        help="Penalty weight for corporate industry subgroup market value gaps inside each combo.",
    )
    parser.add_argument(
        "--corporate-issuer-value-gap-weight",
        type=float,
        default=DEFAULT_CORPORATE_ISSUER_VALUE_GAP_WEIGHT,
        help="Penalty weight for corporate issuer market value gaps inside each combo.",
    )
    parser.add_argument(
        "--category-agency-ssa-value-gap-weight",
        type=float,
        default=DEFAULT_CATEGORY_AGENCY_SSA_VALUE_GAP_WEIGHT,
        help="Penalty weight for Agency/SSA category market value gaps inside each combo.",
    )
    parser.add_argument(
        "--quebec-hydro-quebec-value-gap-weight",
        type=float,
        default=DEFAULT_QUEBEC_HYDRO_QUEBEC_VALUE_GAP_WEIGHT,
        help="Penalty weight for combined Province of Quebec + Hydro-Quebec market value gaps inside each combo.",
    )
    parser.add_argument(
        "--alberta-value-gap-weight",
        type=float,
        default=DEFAULT_ALBERTA_VALUE_GAP_WEIGHT,
        help="Penalty weight for Province of Alberta market value gaps inside each combo.",
    )
    parser.add_argument(
        "--allow-non-inventory",
        action="store_true",
        help="Allow securities without positive Dealer Inventory. Default uses only Dealer Inventory > 0.",
    )
    args = parser.parse_args()
    if abs(args.cash_spend_raise) < 1e-9:
        raise ValueError("cash_spend_raise cannot be zero. Use a positive value for create or a negative value for redemption.")
    side = "create" if args.cash_spend_raise > 0 else "redemption"
    target_value = abs(args.cash_spend_raise)

    holdings = read_holdings(args.input)
    target_duration = benchmark_weighted_duration(holdings)
    bucket_targets = duration_bucket_targets(holdings, target_value)
    all_combo_targets = sector_duration_targets(holdings, target_value)
    corporate_detail_targets = {
        "industry_group": corporate_combo_detail_targets(holdings, target_value, "_industry_group"),
        "industry_subgroup": corporate_combo_detail_targets(holdings, target_value, "_industry_subgroup"),
        "issuer": corporate_combo_detail_targets(holdings, target_value, "_issuer"),
    }
    special_subsector_targets = {
        "category_agency_or_ssa": special_subsector_combo_targets(
            holdings,
            target_value,
            "category_agency_or_ssa",
            "_category_normalized",
            CATEGORY_AGENCY_SSA_VALUES,
        ),
        "issuer_quebec_plus_hydro_quebec": special_subsector_combo_targets(
            holdings,
            target_value,
            "issuer_quebec_plus_hydro_quebec",
            "_issuer_normalized",
            QUEBEC_HYDRO_QUEBEC_ISSUERS,
        ),
        "issuer_province_of_alberta": special_subsector_combo_targets(
            holdings,
            target_value,
            "issuer_province_of_alberta",
            "_issuer_normalized",
            ALBERTA_ISSUERS,
        ),
    }
    require_dealer_inventory = not args.allow_non_inventory
    candidates = build_candidates(holdings, side, require_dealer_inventory)
    combo_targets, skipped_combo_targets = filter_combo_targets_for_available_inventory(
        candidates,
        all_combo_targets,
        args.min_securities_per_combo,
    )

    trades, solver_summary = solve_with_pulp(
        candidates,
        target_value,
        target_duration,
        bucket_targets,
        combo_targets,
        corporate_detail_targets,
        special_subsector_targets,
        args.max_global_duration_gap,
        args.max_bucket_duration_gap,
        args.max_value_gap,
        args.min_securities_per_combo,
        args.solver_time_limit,
        args.combo_value_gap_weight,
        args.corporate_industry_group_value_gap_weight,
        args.corporate_industry_subgroup_value_gap_weight,
        args.corporate_issuer_value_gap_weight,
        args.category_agency_ssa_value_gap_weight,
        args.quebec_hydro_quebec_value_gap_weight,
        args.alberta_value_gap_weight,
    )

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

    summary = {
        "optimizer": "PuLP mixed-integer model",
        "solver_status": solver_summary["solver_status"],
        "solver_time_limit_seconds": args.solver_time_limit,
        "side": side,
        "cash_spend_raise": round(args.cash_spend_raise, 2),
        "target_market_value_cad": round(target_value, 2),
        "actual_trade_market_value_cad": round(actual_value, 2),
        "market_value_gap_cad": round(value_gap, 2),
        "max_value_gap_cad": args.max_value_gap,
        "value_constraint_pass": value_pass,
        "value_constraint_violation": {} if value_pass else {"gap": round(value_gap, 2), "limit": args.max_value_gap},
        "bmk_duration": round(target_duration, 6),
        "trade_duration": round(actual_duration, 6),
        "post_trade_portfolio_duration": round(post_trade_duration, 6),
        "target_duration_source": COLUMN_MAPPING["bmk_weight"],
        "duration_gap": round(global_gap, 6),
        "max_global_duration_gap": args.max_global_duration_gap,
        "max_bucket_duration_gap": args.max_bucket_duration_gap,
        "combo_value_gap_weight": args.combo_value_gap_weight,
        "corporate_industry_group_value_gap_weight": args.corporate_industry_group_value_gap_weight,
        "corporate_industry_subgroup_value_gap_weight": args.corporate_industry_subgroup_value_gap_weight,
        "corporate_issuer_value_gap_weight": args.corporate_issuer_value_gap_weight,
        "category_agency_ssa_value_gap_weight": args.category_agency_ssa_value_gap_weight,
        "quebec_hydro_quebec_value_gap_weight": args.quebec_hydro_quebec_value_gap_weight,
        "alberta_value_gap_weight": args.alberta_value_gap_weight,
        "special_subsector_value_gap_definitions": {
            "category_agency_or_ssa": {
                "input_column": COLUMN_MAPPING["category"],
                "match_values": sorted(CATEGORY_AGENCY_SSA_VALUES),
                "weight": args.category_agency_ssa_value_gap_weight,
            },
            "issuer_quebec_plus_hydro_quebec": {
                "input_column": COLUMN_MAPPING["issuer"],
                "match_values": sorted(QUEBEC_HYDRO_QUEBEC_ISSUERS),
                "weight": args.quebec_hydro_quebec_value_gap_weight,
            },
            "issuer_province_of_alberta": {
                "input_column": COLUMN_MAPPING["issuer"],
                "match_values": sorted(ALBERTA_ISSUERS),
                "weight": args.alberta_value_gap_weight,
            },
        },
        "special_subsector_value_gap_targets": {
            group_name: [
                {
                    "combo": combo,
                    "target_market_value_cad": round(target["target_value"], 2),
                }
                for combo, target in sorted(targets.items())
            ]
            for group_name, targets in sorted(special_subsector_targets.items())
        },
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
        **solver_summary,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    suffix_cash = f"{abs(args.cash_spend_raise):,.0f}".replace(",", "")
    suffix = f"{side}_cash_{suffix_cash}_pulp"
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
