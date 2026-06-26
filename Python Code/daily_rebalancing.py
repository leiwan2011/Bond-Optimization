#!/usr/bin/env python3
"""
Daily rebalancing optimizer for an XBB-style tracking portfolio.

This script uses PuLP mixed-integer optimization. It starts from the current
portfolio quantity (`pre_trade_qty`) and finds trades that move the end
portfolio toward the index market value and risk profile.

Important business rules implemented in this first version:
- trade_qty = trade_lots * round_lot for normal tradable bonds
- Canada Housing Trust uses a 5,000 round lot; all other normal bonds use 1,000
- roll-out names are sold to zero and are not subject to round lot
- no-trade names are kept unchanged
- post_trade_qty cannot be negative
- normal tradable names have abs(trade_qty) <= 0.5 * Issued Amount * 1000
- primary tracking targets are modeled with violation variables and very large
  objective penalties, so the script still returns the closest basket if a day
  is not strictly feasible.
"""

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from datetime import datetime
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

# 1) File paths
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "Output" / "XBB_holdings_with_dealer_inventory_enriched_pre_trade_qty.csv"
DEFAULT_ISSUER_EXCLUSION = ROOT / "Output" / "issuer exclusion list.csv"
DEFAULT_OUTPUT_DIR = ROOT / "Output"

# 2) Input table column mapping
#    Left side = stable internal variable used by this script.
#    Right side = actual column name in the input file.
COLUMN_MAPPING = {
    "issuer": "Issuer",
    "isin": "ISIN",
    "category": "Category",
    "industry_group": "industry group",
    "industry_subgroup": "industry subgroup",
    "duration": "Duration",
    "principal_factor": "Princial_factor",
    "pre_trade_qty": "pre_trade_qty",
    "ratings": "Ratings",
    "bmk_weight": "Bmk Weight",
    "price": "Price",
    "idx_mv": "Market Value",
    "issued_amount": "Issued Amount",
    "effective_date": "Effective Date",
}
ISSUER_EXCLUSION_COLUMN = "Issuers"

# 3) Trading assumptions
DEFAULT_ROUND_LOT = 1000
CANADA_HOUSING_TRUST_ROUND_LOT = 5000
CANADA_HOUSING_TRUST_ISSUER = "canada housing trust"
ISSUED_AMOUNT_MULTIPLIER = 1000
MAX_TRADE_FRACTION_OF_ISSUED_AMOUNT = 0.5
MIN_NORMAL_TRADE_QTY = 10000
PRICE_DIVISOR = 100.0

# 4) Tracking limits
MAX_PORTFOLIO_MV_GAP = 3000.0
MAX_COMBO_MV_GAP = 15000.0
MAX_PORTFOLIO_DURATION_GAP = 0.005
MAX_BUCKET_DURATION_GAP = 0.005
MAX_CATEGORY_GROUP1_ACTIVE_WEIGHT_GAP = 0.0001  # 0.01%
MAX_RATING_4_ACTIVE_WEIGHT_GAP = 0.001  # 0.10%
MAX_CORPORATE_INDUSTRY_ACTIVE_WEIGHT_GAP = 0.0015  # 0.15%
MAX_CORPORATE_ISSUER_ACTIVE_WEIGHT_GAP = 0.002  # 0.20%
MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP = 0.0015  # 0.15%
MAX_QUEBEC_HYDRO_QUEBEC_ACTIVE_WEIGHT_GAP = 0.001  # 0.10%

# 5) Penalty multipliers
#    These weights control optimization priority without changing the model
#    logic below. Violation penalties mean "gap above the configured limit";
#    gap penalties mean the remaining absolute gap inside or outside the limit.
PENALTY_WEIGHTS = {
    # Highest priority: keep top-level tracking limits inside tolerance.
    "portfolio_mv_violation": 1_000_000_000,
    "portfolio_duration_violation": 1_000_000_000,

    # Strong tracking penalties by category/duration and duration bucket.
    "combo_mv_violation": 100_000_000,
    "bucket_duration_violation": 100_000_000,

    # Active-weight tolerance violations.
    "category_group1_active_weight_violation": 1_000_000_000,
    "category_group1_bucket_active_weight_violation": 1_000_000_000,
    "rating_4_active_weight_violation": 1_000_000_000,
    "corporate_industry_group_active_weight_violation": 1_000_000_000,
    "corporate_industry_subgroup_active_weight_violation": 1_000_000_000,
    "corporate_issuer_active_weight_violation": 1_000_000,
    "other_issuer_active_weight_violation": 1_000_000,
    "quebec_hydro_active_weight_violation": 1_000_000,
    "quebec_hydro_bucket_active_weight_violation": 1_000_000,

    # Residual gap polishing after the main tolerance violations are minimized.
    "portfolio_mv_gap": 1.0,
    "portfolio_duration_gap": 1.0,
    "combo_mv_gap": 0.25,
    "bucket_duration_gap": 0.25,
    "category_group1_active_weight_gap": 1.0,
    "category_group1_bucket_active_weight_gap": 1.0,
    "rating_4_active_weight_gap": 1.0,
    "corporate_industry_group_active_weight_gap": 1.0,
    "corporate_industry_subgroup_active_weight_gap": 1.0,
    "corporate_industry_group_bucket_active_weight_gap": 0.25,
    "corporate_industry_subgroup_bucket_active_weight_gap": 0.25,
    "corporate_issuer_active_weight_gap": 1.0,
    "corporate_issuer_bucket_active_weight_gap": 0.25,
    "other_issuer_active_weight_gap": 1.0,
    "other_issuer_bucket_active_weight_gap": 0.25,
    "provincial_municipal_issuer_combo_active_weight_gap": 0.25,
    "agency_ssa_combo_active_weight_gap": 0.25,
    "quebec_hydro_active_weight_gap": 1.0,
    "quebec_hydro_bucket_active_weight_gap": 1.0,

    # Trading cost / operational simplicity.
    "turnover": 0.001,
    "optional_traded_count": 10_000,
}

# 6) No-trade rules
NO_TRADE_INDUSTRY_SUBGROUPS = {"health"}
NO_TRADE_CATEGORIES = {"ssa", "cash and/or derivatives"}
CORPORATE_EXCLUDED_INDUSTRIES = {"health"}
OTHER_ISSUER_ACTIVE_WEIGHT_EXCLUDED_ISSUERS = {"ontario (province of)"}
OTHER_ISSUER_ACTIVE_WEIGHT_EXCLUDED_CATEGORIES = {"agency", "ssa"}
QUEBEC_HYDRO_QUEBEC_ISSUERS = {"province of quebec", "hydro-quebec"}

# 7) Duration bucket definitions
DURATION_BUCKETS = (
    ("0-5", 0.0, 5.0, False),
    ("5-10", 5.0, 10.0, False),
    ("10-14", 10.0, 14.0, False),
    ("14-30", 14.0, 30.0, True),
)

# 8) Solver defaults
DEFAULT_SOLVER_TIME_LIMIT_SECONDS = 60

# =============================================================================
# END CONFIGURATION SECTION
# =============================================================================


def normalize_text(value):
    """Normalize text before comparing issuer/category/industry fields."""
    return str(value or "").strip().lower()


def issuer_family_name(issuer):
    """Map common issuer name variants to the family used by issuer rules."""
    normalized = normalize_text(issuer)
    compact = re.sub(r"\s+", " ", normalized.replace(".", "")).strip()

    if compact in {"ontario (province of)", "ontario province of"}:
        return "ontario (province of)"
    if compact.startswith("ontario (province of) "):
        return "ontario (province of)"

    if compact in {"province of quebec", "quebec province of", "quebec (province of)"}:
        return "province of quebec"
    if compact.startswith("province of quebec "):
        return "province of quebec"
    if compact.startswith("quebec (province of) "):
        return "province of quebec"

    if compact == "hydro-quebec" or compact.startswith("hydro-quebec "):
        return "hydro-quebec"

    return normalized


def parse_number(value):
    """Convert spreadsheet-style numeric strings into floats."""
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    return float(text)


def fmt_money(value):
    """Format a numeric CAD amount for CSV output."""
    return f"{value:,.2f}"


def fmt_qty(value):
    """Format share quantities as whole numbers."""
    return f"{int(round(value))}"


def fmt_decimal(value, digits=8):
    """Format ratios and durations with a stable number of decimals."""
    return f"{value:.{digits}f}"


def input_value(row, field_name, default=""):
    """Read a mapped input field from a CSV row."""
    return row.get(COLUMN_MAPPING[field_name], default)


def validate_required_columns(fieldnames):
    """Fail early when the input file does not contain required mapped columns."""
    missing = [
        column
        for column in COLUMN_MAPPING.values()
        if column not in fieldnames
    ]
    if missing:
        raise ValueError(
            "Input file is missing required column(s): "
            + ", ".join(missing)
            + ". Update COLUMN_MAPPING at the top of this script if your file uses different headers."
        )


def duration_bucket(duration):
    """Assign numeric duration to the configured duration bucket."""
    for name, low, high, include_high in DURATION_BUCKETS:
        if duration >= low and (duration < high or (include_high and duration <= high)):
            return name
    return "out-of-range"


def category_group1(category):
    """Three-way category definition: treasury, government related, corporate."""
    normalized = normalize_text(category)
    if normalized == "federal":
        return "treasury"
    if normalized in {"provincial", "municipal", "agency", "ssa"}:
        return "government related"
    return "corporate"


def category_group2(category):
    """Four-way category definition: treasury, provincial, agency, corporate."""
    normalized = normalize_text(category)
    if normalized == "federal":
        return "treasury"
    if normalized in {"provincial", "municipal"}:
        return "provincial"
    if normalized in {"agency", "ssa"}:
        return "agency"
    return "corporate"


def round_lot_for_issuer(issuer):
    """Canada Housing Trust trades in 5,000 lots; all other names use 1,000."""
    if normalize_text(issuer) == CANADA_HOUSING_TRUST_ISSUER:
        return CANADA_HOUSING_TRUST_ROUND_LOT
    return DEFAULT_ROUND_LOT


def use_other_issuer_active_weight_rule(row):
    """Return True when a non-corporate issuer should receive issuer AW rules."""
    if row["_category_group1"] == "corporate":
        return False
    if row["_issuer_family"] in OTHER_ISSUER_ACTIVE_WEIGHT_EXCLUDED_ISSUERS:
        return False
    if row["_issuer_family"] in QUEBEC_HYDRO_QUEBEC_ISSUERS:
        return False
    if row["_category_normalized"] in OTHER_ISSUER_ACTIVE_WEIGHT_EXCLUDED_CATEGORIES:
        return False
    return True


def use_provincial_municipal_issuer_combo_penalty(row):
    """Return True for provincial/municipal issuers in the combo-level penalty."""
    if row["_category_normalized"] not in {"provincial", "municipal"}:
        return False
    if row["_issuer_family"] in OTHER_ISSUER_ACTIVE_WEIGHT_EXCLUDED_ISSUERS:
        return False
    if row["_issuer_family"] in QUEBEC_HYDRO_QUEBEC_ISSUERS:
        return False
    return True


def use_agency_ssa_combo_penalty(row):
    """Return True for Agency/SSA rows in the combo-level subsector penalty."""
    return row["_category_normalized"] in {"agency", "ssa"}


def is_valid_effective_date(value):
    """Return False only for #N/A, blanks, and unparseable date values."""
    text = str(value or "").strip()
    if not text or text.upper() == "#N/A":
        return False

    # Excel CSV exports may store copied-by-value dates as serial numbers
    # instead of text dates. Treat plausible positive serial dates as valid.
    try:
        serial_date = float(text.replace(",", ""))
        if 0 < serial_date < 100000:
            return True
    except ValueError:
        pass

    for date_format in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y"):
        try:
            datetime.strptime(text, date_format)
            return True
        except ValueError:
            continue
    return False


def read_excluded_issuers(path):
    """Read issuer exclusion list and normalize all names to lowercase."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if ISSUER_EXCLUSION_COLUMN not in (reader.fieldnames or []):
            raise ValueError(
                f"Issuer exclusion file is missing column: {ISSUER_EXCLUSION_COLUMN}"
            )
        return {
            normalize_text(row.get(ISSUER_EXCLUSION_COLUMN))
            for row in reader
            if normalize_text(row.get(ISSUER_EXCLUSION_COLUMN))
        }


def no_trade_reasons(row, excluded_issuers):
    """Collect every reason that makes a row no-trade."""
    reasons = []
    if row["_issuer_normalized"] in excluded_issuers:
        reasons.append("issuer exclusion list")
    if row["_principal_factor"] < 1.0:
        reasons.append("principal factor < 1")
    if row["_issued_amount"] < 300.0:
        reasons.append("issued amount < 300")
    if row["_industry_subgroup_normalized"] in NO_TRADE_INDUSTRY_SUBGROUPS:
        reasons.append("industry subgroup = health")
    if row["_category_normalized"] in NO_TRADE_CATEGORIES:
        reasons.append(f"category = {row['_category_normalized']}")
    return reasons


def read_holdings(path, excluded_issuers):
    """Load holdings and add normalized numeric/classification fields."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        validate_required_columns(reader.fieldnames or [])
        rows = list(reader)

    holdings = []
    for row_number, row in enumerate(rows, start=2):
        price = parse_number(input_value(row, "price"))
        if price <= 0:
            continue

        issuer = input_value(row, "issuer")
        category = input_value(row, "category")
        industry_group = input_value(row, "industry_group")
        industry_subgroup = input_value(row, "industry_subgroup")
        duration = parse_number(input_value(row, "duration"))

        row["_row_number"] = row_number
        row["_issuer_normalized"] = normalize_text(issuer)
        row["_issuer_family"] = issuer_family_name(issuer)
        row["_category_normalized"] = normalize_text(category)
        row["_industry_group_normalized"] = normalize_text(industry_group)
        row["_industry_subgroup_normalized"] = normalize_text(industry_subgroup)
        row["_duration"] = duration
        row["_duration_bucket"] = duration_bucket(duration)
        row["_category_group1"] = category_group1(category)
        row["_category_group2"] = category_group2(category)
        row["_combo_group1_bucket"] = f"{row['_category_group1']}|{row['_duration_bucket']}"
        row["_price"] = price
        row["_price_per_qty"] = price / PRICE_DIVISOR
        row["_idx_mv"] = parse_number(input_value(row, "idx_mv"))
        row["_pre_trade_qty"] = parse_number(input_value(row, "pre_trade_qty"))
        row["_principal_factor"] = parse_number(input_value(row, "principal_factor"))
        row["_ratings"] = str(input_value(row, "ratings")).strip()
        row["_bmk_weight"] = parse_number(input_value(row, "bmk_weight"))
        row["_issued_amount"] = parse_number(input_value(row, "issued_amount"))
        row["_round_lot"] = round_lot_for_issuer(issuer)
        row["_is_roll_out"] = not is_valid_effective_date(input_value(row, "effective_date"))
        row["_no_trade_reasons"] = no_trade_reasons(row, excluded_issuers)

        # Roll-out has priority over no-trade. A no-trade name is fixed only
        # when it is not also a mandatory roll-out name.
        if row["_is_roll_out"]:
            row["_trade_status"] = "roll_out"
        elif row["_no_trade_reasons"]:
            row["_trade_status"] = "no_trade"
        else:
            row["_trade_status"] = "tradable"
        holdings.append(row)

    return holdings


def sanitize_name(value):
    """Create a solver-safe name fragment."""
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value))
    return text.strip("_") or "blank"


def lp_sum(items):
    """Small wrapper so empty sums are clean PuLP expressions."""
    return pulp.lpSum(list(items))


def add_abs_gap(problem, expression, name):
    """Add a non-negative variable representing abs(expression)."""
    variable = pulp.LpVariable(name, lowBound=0)
    problem += variable >= expression, f"{name}_positive"
    problem += variable >= -expression, f"{name}_negative"
    return variable


def add_violation(problem, abs_gap, allowed_gap_expr, name):
    """Add max(0, abs_gap - allowed_gap_expr) as a non-negative variable."""
    variable = pulp.LpVariable(name, lowBound=0)
    problem += variable >= abs_gap - allowed_gap_expr, f"{name}_above_allowed"
    return variable


def record_penalty(penalty_terms, penalty_key, expression):
    """Store a model expression under a named configurable penalty bucket."""
    if penalty_key not in PENALTY_WEIGHTS:
        raise KeyError(f"Missing penalty weight configuration for: {penalty_key}")
    penalty_terms[penalty_key].append(expression)


def weighted_penalty_objective(penalty_terms):
    """Build the objective expression from configured penalty multipliers."""
    return lp_sum(
        PENALTY_WEIGHTS[penalty_key] * lp_sum(expressions)
        for penalty_key, expressions in penalty_terms.items()
        if PENALTY_WEIGHTS[penalty_key] != 0
    )


def weighted_duration(rows, value_key):
    """Calculate value-weighted duration using a row numeric value key."""
    total_value = sum(row[value_key] for row in rows)
    total_duration_value = sum(row[value_key] * row["_duration"] for row in rows)
    return total_duration_value / total_value if total_value else 0.0


def group_rows(holdings, key_func):
    """Return a dictionary of grouped holdings."""
    grouped = defaultdict(list)
    for row in holdings:
        key = key_func(row)
        if key is not None:
            grouped[key].append(row)
    return grouped


def build_model(holdings, solver_time_limit, solver_msg=False):
    """Build and solve the PuLP model, returning expressions and solution data."""
    problem = pulp.LpProblem("Daily_Rebalancing", pulp.LpMinimize)

    trade_qty_expr = {}
    post_qty_expr = {}
    port_end_mv_expr = {}
    abs_trade_mv_vars = {}
    traded_flag_vars = {}
    trade_lot_vars = {}

    for idx, row in enumerate(holdings):
        row_id = row["_row_number"]
        price_per_qty = row["_price_per_qty"]

        if row["_trade_status"] == "roll_out":
            trade_qty_expr[row_id] = -row["_pre_trade_qty"]
            post_qty_expr[row_id] = 0
            port_end_mv_expr[row_id] = 0
            continue

        if row["_trade_status"] == "no_trade":
            trade_qty_expr[row_id] = 0
            post_qty_expr[row_id] = row["_pre_trade_qty"]
            port_end_mv_expr[row_id] = row["_pre_trade_qty"] * price_per_qty
            continue

        round_lot = row["_round_lot"]
        max_trade_qty = (
            row["_issued_amount"]
            * ISSUED_AMOUNT_MULTIPLIER
            * MAX_TRADE_FRACTION_OF_ISSUED_AMOUNT
        )
        max_buy_lots = int(math.floor(max_trade_qty / round_lot))
        max_sell_lots = int(math.floor(row["_pre_trade_qty"] / round_lot))
        max_sell_lots = min(max_sell_lots, max_buy_lots)
        min_buy_lots = int(math.ceil(MIN_NORMAL_TRADE_QTY / round_lot))
        min_sell_lots = min(max_sell_lots, min_buy_lots)

        # If a name has no allowed trade capacity, keep it fixed.
        if max_buy_lots == 0 and max_sell_lots == 0:
            trade_qty_expr[row_id] = 0
            post_qty_expr[row_id] = row["_pre_trade_qty"]
            port_end_mv_expr[row_id] = row["_pre_trade_qty"] * price_per_qty
            row["_trade_status"] = "capacity_fixed"
            row["_no_trade_reasons"] = ["max trade capacity is zero"]
            continue

        variable_name = f"lots_{idx}_{sanitize_name(input_value(row, 'isin'))}"
        # CBC can be brittle with negative integer lower bounds in MPS files.
        # Use a non-negative shifted variable and subtract the sell offset in
        # the linear expression. Business meaning is still signed trade lots.
        shifted_lots = pulp.LpVariable(
            variable_name,
            lowBound=0,
            upBound=max_buy_lots + max_sell_lots,
            cat="Integer",
        )
        signed_lots = shifted_lots - max_sell_lots
        trade_lot_vars[row_id] = signed_lots
        trade_qty = signed_lots * round_lot
        post_qty = row["_pre_trade_qty"] + trade_qty
        port_end_mv = post_qty * price_per_qty
        trade_mv = trade_qty * price_per_qty

        # This repeats the bound logic explicitly, making the business rule easy
        # to audit even if the bound calculation changes later.
        problem += post_qty >= 0, f"post_qty_nonnegative_{row_id}"
        problem += trade_qty <= max_trade_qty, f"max_buy_qty_{row_id}"
        problem += -trade_qty <= max_trade_qty, f"max_sell_qty_{row_id}"

        abs_trade_mv = pulp.LpVariable(f"abs_trade_mv_{row_id}", lowBound=0)
        problem += abs_trade_mv >= trade_mv, f"abs_trade_mv_pos_{row_id}"
        problem += abs_trade_mv >= -trade_mv, f"abs_trade_mv_neg_{row_id}"

        flag = pulp.LpVariable(f"traded_flag_{row_id}", lowBound=0, upBound=1, cat="Binary")
        buy_flag = pulp.LpVariable(f"buy_flag_{row_id}", lowBound=0, upBound=1, cat="Binary")
        sell_flag = pulp.LpVariable(f"sell_flag_{row_id}", lowBound=0, upBound=1, cat="Binary")
        problem += buy_flag + sell_flag == flag, f"trade_direction_flag_{row_id}"

        # If a normal tradable name is bought, it must trade at least
        # MIN_NORMAL_TRADE_QTY. If a position smaller than that is sold, the
        # minimum sell size is the available pre-trade position. Roll-out and
        # no-trade rows do not reach this branch.
        problem += signed_lots <= max_buy_lots * buy_flag, f"buy_lot_max_{row_id}"
        problem += signed_lots >= -max_sell_lots * sell_flag, f"sell_lot_max_{row_id}"
        problem += (
            signed_lots >= min_buy_lots * buy_flag - max_sell_lots * sell_flag
        ), f"buy_min_trade_lots_{row_id}"
        problem += (
            signed_lots <= max_buy_lots * buy_flag - min_sell_lots * sell_flag
        ), f"sell_min_trade_lots_{row_id}"

        trade_qty_expr[row_id] = trade_qty
        post_qty_expr[row_id] = post_qty
        port_end_mv_expr[row_id] = port_end_mv
        abs_trade_mv_vars[row_id] = abs_trade_mv
        traded_flag_vars[row_id] = flag

    total_idx_mv = sum(row["_idx_mv"] for row in holdings)
    idx_duration_total = weighted_duration(holdings, "_idx_mv")
    total_port_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in holdings)

    penalty_terms = defaultdict(list)

    # Portfolio market value gap.
    portfolio_mv_gap = total_port_mv - total_idx_mv
    portfolio_mv_abs = add_abs_gap(problem, portfolio_mv_gap, "portfolio_mv_abs_gap")
    portfolio_mv_violation = add_violation(
        problem,
        portfolio_mv_abs,
        MAX_PORTFOLIO_MV_GAP,
        "portfolio_mv_violation",
    )
    record_penalty(penalty_terms, "portfolio_mv_violation", portfolio_mv_violation)
    record_penalty(penalty_terms, "portfolio_mv_gap", portfolio_mv_abs)

    # Portfolio duration gap, expressed as dollar-duration so it stays linear.
    portfolio_duration_dollar_gap = lp_sum(
        port_end_mv_expr[row["_row_number"]] * (row["_duration"] - idx_duration_total)
        for row in holdings
    )
    portfolio_duration_abs = add_abs_gap(
        problem,
        portfolio_duration_dollar_gap,
        "portfolio_duration_dollar_abs_gap",
    )
    portfolio_duration_violation = add_violation(
        problem,
        portfolio_duration_abs,
        MAX_PORTFOLIO_DURATION_GAP * total_port_mv,
        "portfolio_duration_violation",
    )
    record_penalty(penalty_terms, "portfolio_duration_violation", portfolio_duration_violation)
    record_penalty(penalty_terms, "portfolio_duration_gap", portfolio_duration_abs)

    # category_group1 + duration bucket market value gap. It is controlled by a
    # violation penalty above MAX_COMBO_MV_GAP plus a residual gap penalty.
    group1_bucket_groups = group_rows(
        holdings,
        lambda row: (row["_category_group1"], row["_duration_bucket"]),
    )
    for (group1, bucket), rows in sorted(group1_bucket_groups.items()):
        idx_mv = sum(row["_idx_mv"] for row in rows)
        port_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in rows)
        safe_name = f"{sanitize_name(group1)}_{sanitize_name(bucket)}"
        abs_gap = add_abs_gap(
            problem,
            port_mv - idx_mv,
            f"combo_mv_abs_{safe_name}",
        )
        violation = add_violation(
            problem,
            abs_gap,
            MAX_COMBO_MV_GAP,
            f"combo_mv_violation_{safe_name}",
        )
        record_penalty(penalty_terms, "combo_mv_violation", violation)
        record_penalty(penalty_terms, "combo_mv_gap", abs_gap)

    # Duration bucket duration gap. It is expressed as dollar-duration so the
    # model remains linear, with configurable violation and residual penalties.
    duration_bucket_groups = group_rows(holdings, lambda row: row["_duration_bucket"])
    for bucket, rows in sorted(duration_bucket_groups.items()):
        idx_mv = sum(row["_idx_mv"] for row in rows)
        if idx_mv <= 0:
            continue
        idx_duration = sum(row["_idx_mv"] * row["_duration"] for row in rows) / idx_mv
        bucket_port_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in rows)
        bucket_duration_gap = lp_sum(
            port_end_mv_expr[row["_row_number"]] * (row["_duration"] - idx_duration)
            for row in rows
        )
        abs_gap = add_abs_gap(
            problem,
            bucket_duration_gap,
            f"bucket_duration_abs_{sanitize_name(bucket)}",
        )
        violation = add_violation(
            problem,
            abs_gap,
            MAX_BUCKET_DURATION_GAP * bucket_port_mv,
            f"bucket_duration_violation_{sanitize_name(bucket)}",
        )
        record_penalty(penalty_terms, "bucket_duration_violation", violation)
        record_penalty(penalty_terms, "bucket_duration_gap", abs_gap)

    # category_group1 active weight at portfolio level.
    category_group1_groups = group_rows(holdings, lambda row: row["_category_group1"])
    for group1, rows in sorted(category_group1_groups.items()):
        idx_mv = sum(row["_idx_mv"] for row in rows)
        if total_idx_mv <= 0:
            continue
        port_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in rows)
        active_dollar_gap = port_mv - idx_mv
        abs_gap = add_abs_gap(
            problem,
            active_dollar_gap,
            f"group1_active_abs_{sanitize_name(group1)}",
        )
        violation = add_violation(
            problem,
            abs_gap,
            MAX_CATEGORY_GROUP1_ACTIVE_WEIGHT_GAP * total_idx_mv,
            f"group1_active_violation_{sanitize_name(group1)}",
        )
        record_penalty(penalty_terms, "category_group1_active_weight_violation", violation)
        record_penalty(penalty_terms, "category_group1_active_weight_gap", abs_gap)

    # category_group1 active weight within each duration bucket.
    for bucket, bucket_rows in sorted(duration_bucket_groups.items()):
        idx_bucket_mv = sum(row["_idx_mv"] for row in bucket_rows)
        if idx_bucket_mv <= 0:
            continue
        bucket_by_group1 = group_rows(bucket_rows, lambda row: row["_category_group1"])
        for group1, rows in sorted(bucket_by_group1.items()):
            idx_group_bucket_mv = sum(row["_idx_mv"] for row in rows)
            port_group_bucket_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in rows)
            active_dollar_gap = port_group_bucket_mv - idx_group_bucket_mv
            abs_gap = add_abs_gap(
                problem,
                active_dollar_gap,
                f"group1_bucket_active_abs_{sanitize_name(group1)}_{sanitize_name(bucket)}",
            )
            violation = add_violation(
                problem,
                abs_gap,
                MAX_CATEGORY_GROUP1_ACTIVE_WEIGHT_GAP * idx_bucket_mv,
                f"group1_bucket_active_violation_{sanitize_name(group1)}_{sanitize_name(bucket)}",
            )
            record_penalty(penalty_terms, "category_group1_bucket_active_weight_violation", violation)
            record_penalty(penalty_terms, "category_group1_bucket_active_weight_gap", abs_gap)

    # Rating 4 active weight at portfolio level.
    rating4_rows = [row for row in holdings if str(row["_ratings"]).strip() == "4"]
    if rating4_rows and total_idx_mv > 0:
        idx_mv = sum(row["_idx_mv"] for row in rating4_rows)
        port_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in rating4_rows)
        active_dollar_gap = port_mv - idx_mv
        abs_gap = add_abs_gap(problem, active_dollar_gap, "rating4_active_abs")
        violation = add_violation(
            problem,
            abs_gap,
            MAX_RATING_4_ACTIVE_WEIGHT_GAP * total_idx_mv,
            "rating4_active_violation",
        )
        record_penalty(penalty_terms, "rating_4_active_weight_violation", violation)
        record_penalty(penalty_terms, "rating_4_active_weight_gap", abs_gap)

    # Corporate industry group/subgroup active weight at portfolio level.
    corporate_rows = [row for row in holdings if row["_category_group1"] == "corporate"]
    for field_name, normalized_key in (
        ("industry_group", "_industry_group_normalized"),
        ("industry_subgroup", "_industry_subgroup_normalized"),
    ):
        industry_violation_key = f"corporate_{field_name}_active_weight_violation"
        industry_gap_key = f"corporate_{field_name}_active_weight_gap"
        industry_bucket_gap_key = f"corporate_{field_name}_bucket_active_weight_gap"
        grouped = group_rows(
            corporate_rows,
            lambda row, key=normalized_key: None
            if (
                row["_industry_group_normalized"] in CORPORATE_EXCLUDED_INDUSTRIES
                or row["_industry_subgroup_normalized"] in CORPORATE_EXCLUDED_INDUSTRIES
            )
            else row[key],
        )
        for group_index, (industry_name, rows) in enumerate(sorted(grouped.items())):
            if not industry_name:
                continue
            name_key = f"{group_index}_{sanitize_name(industry_name)}"
            idx_mv = sum(row["_idx_mv"] for row in rows)
            if idx_mv <= 0 or total_idx_mv <= 0:
                continue
            port_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in rows)
            active_dollar_gap = port_mv - idx_mv
            abs_gap = add_abs_gap(
                problem,
                active_dollar_gap,
                f"corp_{field_name}_active_abs_{name_key}",
            )
            violation = add_violation(
                problem,
                abs_gap,
                MAX_CORPORATE_INDUSTRY_ACTIVE_WEIGHT_GAP * total_idx_mv,
                f"corp_{field_name}_active_violation_{name_key}",
            )
            record_penalty(penalty_terms, industry_violation_key, violation)
            record_penalty(penalty_terms, industry_gap_key, abs_gap)

        # Duration-bucket industry active weights are soft penalties.
        for bucket, bucket_rows in sorted(duration_bucket_groups.items()):
            corporate_bucket_rows = [
                row for row in bucket_rows
                if row["_category_group1"] == "corporate"
                and row["_industry_group_normalized"] not in CORPORATE_EXCLUDED_INDUSTRIES
                and row["_industry_subgroup_normalized"] not in CORPORATE_EXCLUDED_INDUSTRIES
            ]
            idx_bucket_mv = sum(row["_idx_mv"] for row in corporate_bucket_rows)
            if idx_bucket_mv <= 0:
                continue
            grouped_bucket = group_rows(corporate_bucket_rows, lambda row, key=normalized_key: row[key])
            for group_index, (industry_name, rows) in enumerate(sorted(grouped_bucket.items())):
                name_key = f"{group_index}_{sanitize_name(industry_name)}"
                idx_industry_bucket_mv = sum(row["_idx_mv"] for row in rows)
                port_industry_bucket_mv = lp_sum(
                    port_end_mv_expr[row["_row_number"]]
                    for row in rows
                )
                active_dollar_gap = port_industry_bucket_mv - idx_industry_bucket_mv
                abs_gap = add_abs_gap(
                    problem,
                    active_dollar_gap,
                    f"soft_corp_{field_name}_{name_key}_{sanitize_name(bucket)}",
                )
                record_penalty(penalty_terms, industry_bucket_gap_key, abs_gap)

    # Corporate issuer active weight at portfolio level.
    corporate_issuer_groups = group_rows(
        corporate_rows,
        lambda row: row["_issuer_normalized"],
    )
    for group_index, (issuer_name, rows) in enumerate(sorted(corporate_issuer_groups.items())):
        if not issuer_name:
            continue
        name_key = f"{group_index}_{sanitize_name(issuer_name)}"
        idx_mv = sum(row["_idx_mv"] for row in rows)
        if idx_mv <= 0 or total_idx_mv <= 0:
            continue
        port_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in rows)
        active_dollar_gap = port_mv - idx_mv
        abs_gap = add_abs_gap(
            problem,
            active_dollar_gap,
            f"corp_issuer_active_abs_{name_key}",
        )
        violation = add_violation(
            problem,
            abs_gap,
            MAX_CORPORATE_ISSUER_ACTIVE_WEIGHT_GAP * total_idx_mv,
            f"corp_issuer_active_violation_{name_key}",
        )
        record_penalty(penalty_terms, "corporate_issuer_active_weight_violation", violation)
        record_penalty(penalty_terms, "corporate_issuer_active_weight_gap", abs_gap)

    # Corporate issuer active weights inside each duration bucket are soft
    # penalties. This encourages issuer balance by bucket without making the
    # daily problem infeasible when a bucket has only a few issuer names.
    for bucket, bucket_rows in sorted(duration_bucket_groups.items()):
        corporate_bucket_rows = [
            row for row in bucket_rows
            if row["_category_group1"] == "corporate"
        ]
        idx_bucket_mv = sum(row["_idx_mv"] for row in corporate_bucket_rows)
        if idx_bucket_mv <= 0:
            continue
        issuer_bucket_groups = group_rows(
            corporate_bucket_rows,
            lambda row: row["_issuer_normalized"],
        )
        for group_index, (issuer_name, rows) in enumerate(sorted(issuer_bucket_groups.items())):
            name_key = f"{group_index}_{sanitize_name(issuer_name)}"
            idx_issuer_bucket_mv = sum(row["_idx_mv"] for row in rows)
            port_issuer_bucket_mv = lp_sum(
                port_end_mv_expr[row["_row_number"]]
                for row in rows
            )
            active_dollar_gap = port_issuer_bucket_mv - idx_issuer_bucket_mv
            abs_gap = add_abs_gap(
                problem,
                active_dollar_gap,
                f"soft_corp_issuer_{name_key}_{sanitize_name(bucket)}",
            )
            record_penalty(penalty_terms, "corporate_issuer_bucket_active_weight_gap", abs_gap)

    # Non-corporate issuer active weight rules. Ontario, Agency, and SSA are
    # intentionally excluded from this issuer-level limit.
    other_issuer_rows = [
        row for row in holdings
        if use_other_issuer_active_weight_rule(row)
    ]
    other_issuer_groups = group_rows(
        other_issuer_rows,
        lambda row: row["_issuer_normalized"],
    )
    for group_index, (issuer_name, rows) in enumerate(sorted(other_issuer_groups.items())):
        if not issuer_name:
            continue
        name_key = f"{group_index}_{sanitize_name(issuer_name)}"
        idx_mv = sum(row["_idx_mv"] for row in rows)
        if idx_mv <= 0 or total_idx_mv <= 0:
            continue
        port_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in rows)
        active_dollar_gap = port_mv - idx_mv
        abs_gap = add_abs_gap(
            problem,
            active_dollar_gap,
            f"other_issuer_active_abs_{name_key}",
        )
        violation = add_violation(
            problem,
            abs_gap,
            MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP * total_idx_mv,
            f"other_issuer_active_violation_{name_key}",
        )
        record_penalty(penalty_terms, "other_issuer_active_weight_violation", violation)
        record_penalty(penalty_terms, "other_issuer_active_weight_gap", abs_gap)

    for bucket, bucket_rows in sorted(duration_bucket_groups.items()):
        other_bucket_rows = [
            row for row in bucket_rows
            if use_other_issuer_active_weight_rule(row)
        ]
        idx_bucket_mv = sum(row["_idx_mv"] for row in other_bucket_rows)
        if idx_bucket_mv <= 0:
            continue
        issuer_bucket_groups = group_rows(
            other_bucket_rows,
            lambda row: row["_issuer_normalized"],
        )
        for group_index, (issuer_name, rows) in enumerate(sorted(issuer_bucket_groups.items())):
            name_key = f"{group_index}_{sanitize_name(issuer_name)}"
            idx_issuer_bucket_mv = sum(row["_idx_mv"] for row in rows)
            port_issuer_bucket_mv = lp_sum(
                port_end_mv_expr[row["_row_number"]]
                for row in rows
            )
            active_dollar_gap = port_issuer_bucket_mv - idx_issuer_bucket_mv
            abs_gap = add_abs_gap(
                problem,
                active_dollar_gap,
                f"other_issuer_bucket_active_abs_{name_key}_{sanitize_name(bucket)}",
            )
            record_penalty(penalty_terms, "other_issuer_bucket_active_weight_gap", abs_gap)

    # Provincial/municipal issuer active weights inside each category_group1 +
    # duration combo. Ontario is excluded entirely, and Quebec + Hydro-Quebec
    # are excluded here because they have their own combined rule below.
    prov_muni_rows = [
        row for row in holdings
        if use_provincial_municipal_issuer_combo_penalty(row)
    ]
    prov_muni_combo_groups = group_rows(
        prov_muni_rows,
        lambda row: row["_combo_group1_bucket"],
    )
    all_rows_by_combo = group_rows(holdings, lambda row: row["_combo_group1_bucket"])
    for combo, combo_prov_muni_rows in sorted(prov_muni_combo_groups.items()):
        combo_rows = all_rows_by_combo[combo]
        idx_combo_mv = sum(row["_idx_mv"] for row in combo_rows)
        if idx_combo_mv <= 0:
            continue
        port_combo_mv = lp_sum(
            port_end_mv_expr[row["_row_number"]]
            for row in combo_rows
        )
        issuer_groups = group_rows(
            combo_prov_muni_rows,
            lambda row: row["_issuer_family"],
        )
        for group_index, (issuer_name, rows) in enumerate(sorted(issuer_groups.items())):
            idx_issuer_combo_mv = sum(row["_idx_mv"] for row in rows)
            if idx_issuer_combo_mv <= 0:
                continue
            target_weight = idx_issuer_combo_mv / idx_combo_mv
            port_issuer_combo_mv = lp_sum(
                port_end_mv_expr[row["_row_number"]]
                for row in rows
            )
            # This linear expression is portfolio issuer MV minus the issuer's
            # index weight inside the combo times total portfolio combo MV.
            active_weight_gap_expr = port_issuer_combo_mv - target_weight * port_combo_mv
            abs_gap = add_abs_gap(
                problem,
                active_weight_gap_expr,
                (
                    "prov_muni_issuer_combo_active_abs_"
                    f"{group_index}_{sanitize_name(issuer_name)}_{sanitize_name(combo)}"
                ),
            )
            record_penalty(
                penalty_terms,
                "provincial_municipal_issuer_combo_active_weight_gap",
                abs_gap,
            )

    # Agency/SSA subsector active weight inside each category_group1 + duration
    # combo. Agency and SSA are grouped together as one subsector.
    agency_ssa_rows = [
        row for row in holdings
        if use_agency_ssa_combo_penalty(row)
    ]
    agency_ssa_combo_groups = group_rows(
        agency_ssa_rows,
        lambda row: row["_combo_group1_bucket"],
    )
    for combo, combo_agency_ssa_rows in sorted(agency_ssa_combo_groups.items()):
        combo_rows = all_rows_by_combo[combo]
        idx_combo_mv = sum(row["_idx_mv"] for row in combo_rows)
        idx_agency_ssa_combo_mv = sum(row["_idx_mv"] for row in combo_agency_ssa_rows)
        if idx_combo_mv <= 0 or idx_agency_ssa_combo_mv <= 0:
            continue
        target_weight = idx_agency_ssa_combo_mv / idx_combo_mv
        port_combo_mv = lp_sum(
            port_end_mv_expr[row["_row_number"]]
            for row in combo_rows
        )
        port_agency_ssa_combo_mv = lp_sum(
            port_end_mv_expr[row["_row_number"]]
            for row in combo_agency_ssa_rows
        )
        active_weight_gap_expr = port_agency_ssa_combo_mv - target_weight * port_combo_mv
        abs_gap = add_abs_gap(
            problem,
            active_weight_gap_expr,
            f"agency_ssa_combo_active_abs_{sanitize_name(combo)}",
        )
        record_penalty(
            penalty_terms,
            "agency_ssa_combo_active_weight_gap",
            abs_gap,
        )

    # Province of Quebec and Hydro-Quebec are exempt from individual issuer
    # limits, but their combined active weight is controlled at portfolio and
    # duration-bucket levels.
    quebec_hydro_rows = [
        row for row in holdings
        if row["_issuer_family"] in QUEBEC_HYDRO_QUEBEC_ISSUERS
    ]
    if quebec_hydro_rows and total_idx_mv > 0:
        idx_mv = sum(row["_idx_mv"] for row in quebec_hydro_rows)
        port_mv = lp_sum(port_end_mv_expr[row["_row_number"]] for row in quebec_hydro_rows)
        active_dollar_gap = port_mv - idx_mv
        abs_gap = add_abs_gap(
            problem,
            active_dollar_gap,
            "quebec_hydro_active_abs",
        )
        violation = add_violation(
            problem,
            abs_gap,
            MAX_QUEBEC_HYDRO_QUEBEC_ACTIVE_WEIGHT_GAP * total_idx_mv,
            "quebec_hydro_active_violation",
        )
        record_penalty(penalty_terms, "quebec_hydro_active_weight_violation", violation)
        record_penalty(penalty_terms, "quebec_hydro_active_weight_gap", abs_gap)

    for bucket, bucket_rows in sorted(duration_bucket_groups.items()):
        quebec_hydro_bucket_rows = [
            row for row in bucket_rows
            if row["_issuer_family"] in QUEBEC_HYDRO_QUEBEC_ISSUERS
        ]
        idx_bucket_mv = sum(row["_idx_mv"] for row in bucket_rows)
        idx_quebec_hydro_bucket_mv = sum(row["_idx_mv"] for row in quebec_hydro_bucket_rows)
        if idx_bucket_mv <= 0 or idx_quebec_hydro_bucket_mv <= 0:
            continue
        port_quebec_hydro_bucket_mv = lp_sum(
            port_end_mv_expr[row["_row_number"]]
            for row in quebec_hydro_bucket_rows
        )
        active_dollar_gap = port_quebec_hydro_bucket_mv - idx_quebec_hydro_bucket_mv
        abs_gap = add_abs_gap(
            problem,
            active_dollar_gap,
            f"quebec_hydro_bucket_active_abs_{sanitize_name(bucket)}",
        )
        violation = add_violation(
            problem,
            abs_gap,
            MAX_QUEBEC_HYDRO_QUEBEC_ACTIVE_WEIGHT_GAP * idx_bucket_mv,
            f"quebec_hydro_bucket_active_violation_{sanitize_name(bucket)}",
        )
        record_penalty(penalty_terms, "quebec_hydro_bucket_active_weight_violation", violation)
        record_penalty(penalty_terms, "quebec_hydro_bucket_active_weight_gap", abs_gap)

    turnover = lp_sum(abs_trade_mv_vars.values())
    optional_traded_count = lp_sum(traded_flag_vars.values())
    record_penalty(penalty_terms, "turnover", turnover)
    record_penalty(penalty_terms, "optional_traded_count", optional_traded_count)

    # The objective is entirely controlled by PENALTY_WEIGHTS in the
    # configuration section. To change business priority, adjust those
    # multipliers without changing the model logic.
    problem += weighted_penalty_objective(penalty_terms)

    solver = pulp.PULP_CBC_CMD(msg=solver_msg, timeLimit=solver_time_limit)
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]

    return {
        "problem": problem,
        "status": status,
        "trade_qty_expr": trade_qty_expr,
        "post_qty_expr": post_qty_expr,
        "port_end_mv_expr": port_end_mv_expr,
        "abs_trade_mv_vars": abs_trade_mv_vars,
        "traded_flag_vars": traded_flag_vars,
        "trade_lot_vars": trade_lot_vars,
        "penalty_terms": penalty_terms,
        "total_idx_mv": total_idx_mv,
        "idx_duration_total": idx_duration_total,
    }


def value_of(expression):
    """Return a numeric value for constants or PuLP expressions."""
    value = pulp.value(expression)
    return 0.0 if value is None else float(value)


def summarize_penalties(model):
    """Summarize configured penalty buckets after the solver has run."""
    rows = []
    for penalty_key, expressions in sorted(model.get("penalty_terms", {}).items()):
        raw_value = sum(value_of(expression) for expression in expressions)
        weight = PENALTY_WEIGHTS[penalty_key]
        rows.append({
            "penalty": penalty_key,
            "terms": len(expressions),
            "weight": weight,
            "raw_value": raw_value,
            "weighted_value": raw_value * weight,
        })
    return rows


def calculated_results(holdings, model):
    """Convert model expressions into per-row numeric results."""
    results = []
    for row in holdings:
        row_id = row["_row_number"]
        trade_qty = value_of(model["trade_qty_expr"][row_id])
        post_qty = value_of(model["post_qty_expr"][row_id])
        port_end_mv = value_of(model["port_end_mv_expr"][row_id])
        trade_mv = trade_qty * row["_price_per_qty"]
        idx_mv = row["_idx_mv"]
        results.append({
            "row": row,
            "trade_qty": trade_qty,
            "post_trade_qty": post_qty,
            "trade_mv": trade_mv,
            "port_end_mv": port_end_mv,
            "idx_mv": idx_mv,
        })
    return results


def summarize_portfolio(results):
    """Create the top-level portfolio summary dictionary."""
    idx_mv = sum(item["idx_mv"] for item in results)
    port_end_mv = sum(item["port_end_mv"] for item in results)
    idx_duration = (
        sum(item["idx_mv"] * item["row"]["_duration"] for item in results) / idx_mv
        if idx_mv else 0.0
    )
    port_end_duration = (
        sum(item["port_end_mv"] * item["row"]["_duration"] for item in results) / port_end_mv
        if port_end_mv else 0.0
    )
    turnover = sum(abs(item["trade_mv"]) for item in results)
    mandatory_traded_count = sum(
        1 for item in results
        if item["row"]["_trade_status"] == "roll_out" and abs(item["trade_qty"]) > 1e-9
    )
    optional_traded_count = sum(
        1 for item in results
        if item["row"]["_trade_status"] == "tradable" and abs(item["trade_qty"]) > 1e-9
    )
    return {
        "idx_mv": idx_mv,
        "port_end_mv": port_end_mv,
        "mv_gap": port_end_mv - idx_mv,
        "idx_duration": idx_duration,
        "port_end_duration": port_end_duration,
        "duration_gap": port_end_duration - idx_duration,
        "turnover": turnover,
        "mandatory_traded_count": mandatory_traded_count,
        "optional_traded_count": optional_traded_count,
        "total_traded_count": mandatory_traded_count + optional_traded_count,
    }


def summarize_groups(results, key_func, total_idx_mv, total_port_mv):
    """Generic group summary with market value, weight, and duration gaps."""
    grouped = defaultdict(list)
    for item in results:
        grouped[key_func(item["row"])].append(item)

    rows = []
    for key, items in sorted(grouped.items()):
        idx_mv = sum(item["idx_mv"] for item in items)
        port_mv = sum(item["port_end_mv"] for item in items)
        idx_duration = (
            sum(item["idx_mv"] * item["row"]["_duration"] for item in items) / idx_mv
            if idx_mv else 0.0
        )
        port_duration = (
            sum(item["port_end_mv"] * item["row"]["_duration"] for item in items) / port_mv
            if port_mv else 0.0
        )
        rows.append({
            "group": key,
            "idx_mv": idx_mv,
            "port_end_mv": port_mv,
            "mv_gap": port_mv - idx_mv,
            "idx_weight": idx_mv / total_idx_mv if total_idx_mv else 0.0,
            "port_weight": port_mv / total_port_mv if total_port_mv else 0.0,
            "active_weight": (
                port_mv / total_port_mv - idx_mv / total_idx_mv
                if total_idx_mv and total_port_mv else 0.0
            ),
            "idx_duration": idx_duration,
            "port_end_duration": port_duration,
            "duration_gap": port_duration - idx_duration,
        })
    return rows


def write_csv(path, rows, fieldnames):
    """Write dictionaries to CSV with a fixed header order."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(output_dir, holdings, model, solver_time_limit):
    """Write the trade CSV and one consolidated readable log file."""
    results = calculated_results(holdings, model)
    portfolio = summarize_portfolio(results)
    total_idx_mv = portfolio["idx_mv"]
    total_port_mv = portfolio["port_end_mv"]

    trade_rows = []
    complete_rows = []
    for item in results:
        row = item["row"]
        no_trade_reason = "; ".join(row["_no_trade_reasons"])
        roll_out_reason = "invalid effective date" if row["_is_roll_out"] else ""
        output_row = {
            "Issuer": input_value(row, "issuer"),
            "ISIN": input_value(row, "isin"),
            "Category": input_value(row, "category"),
            "category_group1": row["_category_group1"],
            "category_group2": row["_category_group2"],
            "industry group": input_value(row, "industry_group"),
            "industry subgroup": input_value(row, "industry_subgroup"),
            "Duration Bucket": row["_duration_bucket"],
            "Duration": fmt_decimal(row["_duration"], 6),
            "Ratings": row["_ratings"],
            "Price": input_value(row, "price"),
            "Idx MV": fmt_money(item["idx_mv"]),
            "Pre Trade Shares": fmt_qty(row["_pre_trade_qty"]),
            "Trade Shares": fmt_qty(item["trade_qty"]),
            "Post Trade Shares": fmt_qty(item["post_trade_qty"]),
            "Trade MV": fmt_money(item["trade_mv"]),
            "Port End MV": fmt_money(item["port_end_mv"]),
            "Round Lot": row["_round_lot"],
            "Trade Status": row["_trade_status"],
            "No Trade Flag": bool(row["_no_trade_reasons"] and not row["_is_roll_out"]),
            "No Trade Reason": no_trade_reason,
            "Roll Out Flag": row["_is_roll_out"],
            "Roll Out Reason": roll_out_reason,
            "Effective Date": input_value(row, "effective_date"),
            "Issued Amount": input_value(row, "issued_amount"),
            "Principal Factor": input_value(row, "principal_factor"),
        }
        complete_rows.append(output_row)
        if abs(item["trade_qty"]) > 1e-9:
            trade_rows.append(output_row)

    output_dir.mkdir(parents=True, exist_ok=True)
    trade_path = output_dir / "daily_rebalancing_trades.csv"
    log_path = output_dir / "daily_rebalancing_log.txt"
    for old_path in output_dir.glob("daily_rebalancing_*"):
        if old_path not in {trade_path, log_path} and old_path.is_file():
            old_path.unlink()

    complete_fieldnames = list(complete_rows[0].keys()) if complete_rows else ["Issuer"]
    write_csv(trade_path, trade_rows, complete_fieldnames)

    constraint_rows = build_constraint_summary(results, portfolio)
    log_path.write_text(
        build_log_text(
            model,
            solver_time_limit,
            portfolio,
            results,
            constraint_rows,
            trade_path,
        ),
        encoding="utf-8",
    )

    summary = {
        "solver_status": model["status"],
        "solver_time_limit_seconds": solver_time_limit,
        "outputs": {
            "trades": str(trade_path),
            "log": str(log_path),
        },
        "portfolio": portfolio,
        "limits": {
            "portfolio_mv_gap": MAX_PORTFOLIO_MV_GAP,
            "combo_mv_gap": MAX_COMBO_MV_GAP,
            "portfolio_duration_gap": MAX_PORTFOLIO_DURATION_GAP,
            "bucket_duration_gap": MAX_BUCKET_DURATION_GAP,
            "category_group1_active_weight_gap": MAX_CATEGORY_GROUP1_ACTIVE_WEIGHT_GAP,
            "rating_4_active_weight_gap": MAX_RATING_4_ACTIVE_WEIGHT_GAP,
            "corporate_industry_active_weight_gap": MAX_CORPORATE_INDUSTRY_ACTIVE_WEIGHT_GAP,
            "corporate_issuer_active_weight_gap": MAX_CORPORATE_ISSUER_ACTIVE_WEIGHT_GAP,
            "other_issuer_active_weight_gap": MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP,
            "quebec_hydro_quebec_active_weight_gap": MAX_QUEBEC_HYDRO_QUEBEC_ACTIVE_WEIGHT_GAP,
        },
        "penalty_weights": PENALTY_WEIGHTS,
    }
    return summary


def build_log_text(model, solver_time_limit, portfolio, results, constraint_rows, trade_path):
    """Build one human-readable log file containing all optimization summaries."""
    total_idx_mv = portfolio["idx_mv"]
    total_port_mv = portfolio["port_end_mv"]
    failed_constraints = [row for row in constraint_rows if row["Status"] != "PASS"]

    group1_rows = summarize_groups(results, lambda row: row["_category_group1"], total_idx_mv, total_port_mv)
    group1_bucket_rows = summarize_groups(
        results,
        lambda row: f"{row['_category_group1']}|{row['_duration_bucket']}",
        total_idx_mv,
        total_port_mv,
    )
    duration_rows = summarize_groups(results, lambda row: row["_duration_bucket"], total_idx_mv, total_port_mv)
    rating_rows = summarize_groups(results, lambda row: f"rating {row['_ratings']}", total_idx_mv, total_port_mv)

    roll_out_rows = [
        item for item in results
        if item["row"]["_is_roll_out"]
    ]
    no_trade_rows = [
        item for item in results
        if item["row"]["_no_trade_reasons"] and not item["row"]["_is_roll_out"]
    ]
    roll_out_trade_mv = sum(abs(item["trade_mv"]) for item in roll_out_rows)

    lines = []
    lines.append("Daily Rebalancing Optimization Log")
    lines.append("=" * 40)
    lines.append("")
    lines.append("Output Files")
    lines.append(f"- Trade CSV: {trade_path}")
    lines.append("- This log file contains the optimization summary and constraint audit.")
    lines.append("")
    lines.append("Solver")
    lines.append(f"- Status: {model['status']}")
    lines.append(f"- Time limit seconds: {solver_time_limit}")
    lines.append("")
    lines.append("Portfolio Summary")
    lines.append(f"- Index MV: {fmt_money(portfolio['idx_mv'])}")
    lines.append(f"- Portfolio End MV: {fmt_money(portfolio['port_end_mv'])}")
    lines.append(f"- MV Gap: {fmt_money(portfolio['mv_gap'])} (limit {fmt_money(MAX_PORTFOLIO_MV_GAP)})")
    lines.append(f"- Index Duration: {fmt_decimal(portfolio['idx_duration'], 8)}")
    lines.append(f"- Portfolio End Duration: {fmt_decimal(portfolio['port_end_duration'], 8)}")
    lines.append(f"- Duration Gap: {fmt_decimal(portfolio['duration_gap'], 8)} (limit {fmt_decimal(MAX_PORTFOLIO_DURATION_GAP, 8)})")
    lines.append(f"- Turnover: {fmt_money(portfolio['turnover'])}")
    lines.append(f"- Mandatory traded count: {portfolio['mandatory_traded_count']}")
    lines.append(f"- Optional traded count: {portfolio['optional_traded_count']}")
    lines.append(f"- Total traded count: {portfolio['total_traded_count']}")
    lines.append("")
    lines.append("Mandatory / Restricted Names")
    lines.append(f"- Roll out names: {len(roll_out_rows)}")
    lines.append(f"- Roll out absolute trade MV: {fmt_money(roll_out_trade_mv)}")
    lines.append(f"- No-trade names: {len(no_trade_rows)}")
    lines.append("")
    lines.append("Limits")
    lines.append(f"- Portfolio MV gap: {fmt_money(MAX_PORTFOLIO_MV_GAP)}")
    lines.append(f"- Category group1 + duration MV gap: {fmt_money(MAX_COMBO_MV_GAP)} (penalty)")
    lines.append(f"- Portfolio duration gap: {fmt_decimal(MAX_PORTFOLIO_DURATION_GAP, 8)}")
    lines.append(f"- Duration bucket duration gap: {fmt_decimal(MAX_BUCKET_DURATION_GAP, 8)} (penalty)")
    lines.append(f"- Category group1 active weight: {fmt_decimal(MAX_CATEGORY_GROUP1_ACTIVE_WEIGHT_GAP, 8)}")
    lines.append(f"- Rating 4 active weight: {fmt_decimal(MAX_RATING_4_ACTIVE_WEIGHT_GAP, 8)}")
    lines.append(f"- Corporate industry active weight: {fmt_decimal(MAX_CORPORATE_INDUSTRY_ACTIVE_WEIGHT_GAP, 8)}")
    lines.append(f"- Corporate issuer active weight: {fmt_decimal(MAX_CORPORATE_ISSUER_ACTIVE_WEIGHT_GAP, 8)}")
    lines.append(f"- Other issuer active weight: {fmt_decimal(MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP, 8)}")
    lines.append(f"- Quebec + Hydro-Quebec active weight: {fmt_decimal(MAX_QUEBEC_HYDRO_QUEBEC_ACTIVE_WEIGHT_GAP, 8)}")
    lines.append("")
    lines.append("Penalty Multipliers")
    for row in summarize_penalties(model):
        lines.append(
            f"- {row['penalty']}: weight {row['weight']}, terms {row['terms']}, "
            f"raw {fmt_decimal(row['raw_value'], 6)}, weighted {fmt_decimal(row['weighted_value'], 6)}"
        )
    lines.append("")
    lines.append("Constraint Audit")
    pass_count = sum(1 for row in constraint_rows if row["Status"] == "PASS")
    soft_count = sum(1 for row in constraint_rows if row["Status"] == "SOFT")
    fail_count = len(constraint_rows) - pass_count - soft_count
    lines.append(f"- PASS: {pass_count}")
    lines.append(f"- FAIL: {fail_count}")
    lines.append(f"- SOFT: {soft_count}")
    hard_failed_constraints = [row for row in failed_constraints if row["Status"] != "SOFT"]
    soft_constraints = [row for row in failed_constraints if row["Status"] == "SOFT"]
    if hard_failed_constraints:
        lines.append("")
        lines.append("Failed Constraints")
        for row in hard_failed_constraints:
            lines.append(
                f"- {row['Constraint']} | {row['Group']} | gap {row['Gap']} | limit {row['Limit']}"
            )
    else:
        lines.append("- All required constraints passed.")

    if soft_constraints:
        lines.append("")
        lines.append("Soft Penalty Constraints Above Limit")
        for row in soft_constraints:
            lines.append(
                f"- {row['Constraint']} | {row['Group']} | gap {row['Gap']} | limit {row['Limit']}"
            )

    append_group_summary(lines, "Category Group1 Summary", group1_rows)
    append_group_summary(lines, "Category Group1 + Duration Summary", group1_bucket_rows)
    append_group_summary(lines, "Duration Bucket Summary", duration_rows)
    append_group_summary(lines, "Rating Summary", rating_rows)

    if roll_out_rows:
        lines.append("")
        lines.append("Roll Out Details")
        for item in roll_out_rows:
            row = item["row"]
            lines.append(
                f"- {input_value(row, 'issuer')} | {input_value(row, 'isin')} | "
                f"effective date {input_value(row, 'effective_date')} | "
                f"trade qty {fmt_qty(item['trade_qty'])} | trade MV {fmt_money(item['trade_mv'])}"
            )

    lines.append("")
    return "\n".join(lines)


def append_group_summary(lines, title, rows):
    """Append compact group-level MV/weight/duration rows to the log."""
    lines.append("")
    lines.append(title)
    if not rows:
        lines.append("- No rows")
        return
    for row in rows:
        lines.append(
            f"- {row['group']}: MV gap {fmt_money(row['mv_gap'])}, "
            f"active weight {fmt_decimal(row['active_weight'], 8)}, "
            f"duration gap {fmt_decimal(row['duration_gap'], 8)}"
        )


def pass_fail(abs_gap, limit):
    """Return PASS when a numeric gap is inside the given absolute limit."""
    return "PASS" if abs(abs_gap) <= limit + 1e-9 else "FAIL"


def soft_pass_fail(abs_gap, limit):
    """Return PASS inside limit and SOFT when only the penalty is carrying it."""
    return "PASS" if abs(abs_gap) <= limit + 1e-9 else "SOFT"


def build_constraint_summary(results, portfolio):
    """Create an audit table for the main tracking constraints."""
    rows = []
    total_idx_mv = portfolio["idx_mv"]
    total_port_mv = portfolio["port_end_mv"]

    rows.append({
        "Constraint": "portfolio_mv_gap",
        "Group": "portfolio",
        "Gap": fmt_decimal(portfolio["mv_gap"], 8),
        "Limit": fmt_decimal(MAX_PORTFOLIO_MV_GAP, 8),
        "Status": pass_fail(portfolio["mv_gap"], MAX_PORTFOLIO_MV_GAP),
    })
    rows.append({
        "Constraint": "portfolio_duration_gap",
        "Group": "portfolio",
        "Gap": fmt_decimal(portfolio["duration_gap"], 8),
        "Limit": fmt_decimal(MAX_PORTFOLIO_DURATION_GAP, 8),
        "Status": pass_fail(portfolio["duration_gap"], MAX_PORTFOLIO_DURATION_GAP),
    })

    group1_bucket_rows = summarize_groups(
        results,
        lambda row: f"{row['_category_group1']}|{row['_duration_bucket']}",
        total_idx_mv,
        total_port_mv,
    )
    for row in group1_bucket_rows:
        rows.append({
            "Constraint": "category_group1_duration_mv_gap",
            "Group": row["group"],
            "Gap": fmt_decimal(row["mv_gap"], 8),
            "Limit": fmt_decimal(MAX_COMBO_MV_GAP, 8),
            "Status": soft_pass_fail(row["mv_gap"], MAX_COMBO_MV_GAP),
        })

    duration_rows = summarize_groups(results, lambda row: row["_duration_bucket"], total_idx_mv, total_port_mv)
    for row in duration_rows:
        rows.append({
            "Constraint": "duration_bucket_duration_gap",
            "Group": row["group"],
            "Gap": fmt_decimal(row["duration_gap"], 8),
            "Limit": fmt_decimal(MAX_BUCKET_DURATION_GAP, 8),
            "Status": soft_pass_fail(row["duration_gap"], MAX_BUCKET_DURATION_GAP),
        })

    # category_group1 active weight inside each duration bucket.
    results_by_bucket = defaultdict(list)
    for item in results:
        results_by_bucket[item["row"]["_duration_bucket"]].append(item)
    for bucket, bucket_items in sorted(results_by_bucket.items()):
        idx_bucket_mv = sum(item["idx_mv"] for item in bucket_items)
        port_bucket_mv = sum(item["port_end_mv"] for item in bucket_items)
        if idx_bucket_mv <= 0 or port_bucket_mv <= 0:
            continue
        bucket_grouped = defaultdict(list)
        for item in bucket_items:
            bucket_grouped[item["row"]["_category_group1"]].append(item)
        for group1, items in sorted(bucket_grouped.items()):
            idx_group_mv = sum(item["idx_mv"] for item in items)
            port_group_mv = sum(item["port_end_mv"] for item in items)
            gap = (port_group_mv / port_bucket_mv) - (idx_group_mv / idx_bucket_mv)
            rows.append({
                "Constraint": "category_group1_within_duration_active_weight",
                "Group": f"{group1}|{bucket}",
                "Gap": fmt_decimal(gap, 8),
                "Limit": fmt_decimal(MAX_CATEGORY_GROUP1_ACTIVE_WEIGHT_GAP, 8),
                "Status": pass_fail(gap, MAX_CATEGORY_GROUP1_ACTIVE_WEIGHT_GAP),
            })

    group1_rows = summarize_groups(results, lambda row: row["_category_group1"], total_idx_mv, total_port_mv)
    for row in group1_rows:
        rows.append({
            "Constraint": "category_group1_active_weight",
            "Group": row["group"],
            "Gap": fmt_decimal(row["active_weight"], 8),
            "Limit": fmt_decimal(MAX_CATEGORY_GROUP1_ACTIVE_WEIGHT_GAP, 8),
            "Status": pass_fail(row["active_weight"], MAX_CATEGORY_GROUP1_ACTIVE_WEIGHT_GAP),
        })

    rating_rows = summarize_groups(results, lambda row: f"rating {row['_ratings']}", total_idx_mv, total_port_mv)
    for row in rating_rows:
        if row["group"] == "rating 4":
            rows.append({
                "Constraint": "rating_4_active_weight",
                "Group": row["group"],
                "Gap": fmt_decimal(row["active_weight"], 8),
                "Limit": fmt_decimal(MAX_RATING_4_ACTIVE_WEIGHT_GAP, 8),
                "Status": pass_fail(row["active_weight"], MAX_RATING_4_ACTIVE_WEIGHT_GAP),
            })
            break

    corporate_items = [
        item for item in results
        if item["row"]["_category_group1"] == "corporate"
        and item["row"]["_industry_group_normalized"] not in CORPORATE_EXCLUDED_INDUSTRIES
        and item["row"]["_industry_subgroup_normalized"] not in CORPORATE_EXCLUDED_INDUSTRIES
    ]
    for label, key_func in (
        ("corporate_industry_group_active_weight", lambda row: row["_industry_group_normalized"]),
        ("corporate_industry_subgroup_active_weight", lambda row: row["_industry_subgroup_normalized"]),
    ):
        grouped = defaultdict(list)
        for item in corporate_items:
            grouped[key_func(item["row"])].append(item)
        for group_name, items in sorted(grouped.items()):
            idx_mv = sum(item["idx_mv"] for item in items)
            port_mv = sum(item["port_end_mv"] for item in items)
            gap = (
                port_mv / total_port_mv - idx_mv / total_idx_mv
                if total_idx_mv and total_port_mv else 0.0
            )
            rows.append({
                "Constraint": label,
                "Group": group_name,
                "Gap": fmt_decimal(gap, 8),
                "Limit": fmt_decimal(MAX_CORPORATE_INDUSTRY_ACTIVE_WEIGHT_GAP, 8),
                "Status": pass_fail(gap, MAX_CORPORATE_INDUSTRY_ACTIVE_WEIGHT_GAP),
            })

    issuer_grouped = defaultdict(list)
    for item in results:
        if item["row"]["_category_group1"] == "corporate":
            issuer_grouped[item["row"]["_issuer_normalized"]].append(item)
    for issuer_name, items in sorted(issuer_grouped.items()):
        idx_mv = sum(item["idx_mv"] for item in items)
        port_mv = sum(item["port_end_mv"] for item in items)
        gap = (
            port_mv / total_port_mv - idx_mv / total_idx_mv
            if total_idx_mv and total_port_mv else 0.0
        )
        rows.append({
            "Constraint": "corporate_issuer_active_weight",
            "Group": issuer_name,
            "Gap": fmt_decimal(gap, 8),
            "Limit": fmt_decimal(MAX_CORPORATE_ISSUER_ACTIVE_WEIGHT_GAP, 8),
            "Status": pass_fail(gap, MAX_CORPORATE_ISSUER_ACTIVE_WEIGHT_GAP),
        })

    other_issuer_grouped = defaultdict(list)
    for item in results:
        if use_other_issuer_active_weight_rule(item["row"]):
            other_issuer_grouped[item["row"]["_issuer_normalized"]].append(item)
    for issuer_name, items in sorted(other_issuer_grouped.items()):
        idx_mv = sum(item["idx_mv"] for item in items)
        port_mv = sum(item["port_end_mv"] for item in items)
        gap = (
            port_mv / total_port_mv - idx_mv / total_idx_mv
            if total_idx_mv and total_port_mv else 0.0
        )
        rows.append({
            "Constraint": "other_issuer_active_weight",
            "Group": issuer_name,
            "Gap": fmt_decimal(gap, 8),
            "Limit": fmt_decimal(MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP, 8),
            "Status": pass_fail(gap, MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP),
        })

    for bucket, bucket_items in sorted(results_by_bucket.items()):
        other_bucket_items = [
            item for item in bucket_items
            if use_other_issuer_active_weight_rule(item["row"])
        ]
        idx_bucket_mv = sum(item["idx_mv"] for item in other_bucket_items)
        port_bucket_mv = sum(item["port_end_mv"] for item in other_bucket_items)
        if idx_bucket_mv <= 0 or port_bucket_mv <= 0:
            continue
        grouped = defaultdict(list)
        for item in other_bucket_items:
            grouped[item["row"]["_issuer_normalized"]].append(item)
        for issuer_name, items in sorted(grouped.items()):
            idx_mv = sum(item["idx_mv"] for item in items)
            port_mv = sum(item["port_end_mv"] for item in items)
            gap = (port_mv / port_bucket_mv) - (idx_mv / idx_bucket_mv)
            rows.append({
                "Constraint": "soft_other_issuer_within_duration_active_weight",
                "Group": f"{issuer_name}|{bucket}",
                "Gap": fmt_decimal(gap, 8),
                "Limit": fmt_decimal(MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP, 8),
                "Status": soft_pass_fail(gap, MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP),
            })

    results_by_combo = defaultdict(list)
    for item in results:
        results_by_combo[item["row"]["_combo_group1_bucket"]].append(item)
    for combo, combo_items in sorted(results_by_combo.items()):
        idx_combo_mv = sum(item["idx_mv"] for item in combo_items)
        port_combo_mv = sum(item["port_end_mv"] for item in combo_items)
        if idx_combo_mv <= 0 or port_combo_mv <= 0:
            continue
        eligible_items = [
            item for item in combo_items
            if use_provincial_municipal_issuer_combo_penalty(item["row"])
        ]
        grouped = defaultdict(list)
        for item in eligible_items:
            grouped[item["row"]["_issuer_family"]].append(item)
        for issuer_name, items in sorted(grouped.items()):
            idx_mv = sum(item["idx_mv"] for item in items)
            port_mv = sum(item["port_end_mv"] for item in items)
            gap = (port_mv / port_combo_mv) - (idx_mv / idx_combo_mv)
            rows.append({
                "Constraint": "provincial_municipal_issuer_combo_active_weight",
                "Group": f"{issuer_name}|{combo}",
                "Gap": fmt_decimal(gap, 8),
                "Limit": fmt_decimal(MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP, 8),
                "Status": soft_pass_fail(gap, MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP),
            })

    for combo, combo_items in sorted(results_by_combo.items()):
        idx_combo_mv = sum(item["idx_mv"] for item in combo_items)
        port_combo_mv = sum(item["port_end_mv"] for item in combo_items)
        if idx_combo_mv <= 0 or port_combo_mv <= 0:
            continue
        agency_ssa_items = [
            item for item in combo_items
            if use_agency_ssa_combo_penalty(item["row"])
        ]
        if not agency_ssa_items:
            continue
        idx_mv = sum(item["idx_mv"] for item in agency_ssa_items)
        port_mv = sum(item["port_end_mv"] for item in agency_ssa_items)
        gap = (port_mv / port_combo_mv) - (idx_mv / idx_combo_mv)
        rows.append({
            "Constraint": "agency_ssa_combo_active_weight",
            "Group": f"agency + ssa|{combo}",
            "Gap": fmt_decimal(gap, 8),
            "Limit": fmt_decimal(MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP, 8),
            "Status": soft_pass_fail(gap, MAX_OTHER_ISSUER_ACTIVE_WEIGHT_GAP),
        })

    quebec_hydro_items = [
        item for item in results
        if item["row"]["_issuer_family"] in QUEBEC_HYDRO_QUEBEC_ISSUERS
    ]
    if quebec_hydro_items:
        idx_mv = sum(item["idx_mv"] for item in quebec_hydro_items)
        port_mv = sum(item["port_end_mv"] for item in quebec_hydro_items)
        gap = (
            port_mv / total_port_mv - idx_mv / total_idx_mv
            if total_idx_mv and total_port_mv else 0.0
        )
        rows.append({
            "Constraint": "quebec_hydro_quebec_active_weight",
            "Group": "province of quebec + hydro-quebec",
            "Gap": fmt_decimal(gap, 8),
            "Limit": fmt_decimal(MAX_QUEBEC_HYDRO_QUEBEC_ACTIVE_WEIGHT_GAP, 8),
            "Status": pass_fail(gap, MAX_QUEBEC_HYDRO_QUEBEC_ACTIVE_WEIGHT_GAP),
        })

    for bucket, bucket_items in sorted(results_by_bucket.items()):
        quebec_hydro_bucket_items = [
            item for item in bucket_items
            if item["row"]["_issuer_family"] in QUEBEC_HYDRO_QUEBEC_ISSUERS
        ]
        if not quebec_hydro_bucket_items:
            continue
        idx_bucket_mv = sum(item["idx_mv"] for item in bucket_items)
        port_bucket_mv = sum(item["port_end_mv"] for item in bucket_items)
        idx_mv = sum(item["idx_mv"] for item in quebec_hydro_bucket_items)
        port_mv = sum(item["port_end_mv"] for item in quebec_hydro_bucket_items)
        if idx_bucket_mv <= 0 or port_bucket_mv <= 0:
            continue
        gap = (port_mv / port_bucket_mv) - (idx_mv / idx_bucket_mv)
        rows.append({
            "Constraint": "quebec_hydro_quebec_within_duration_active_weight",
            "Group": f"province of quebec + hydro-quebec|{bucket}",
            "Gap": fmt_decimal(gap, 8),
            "Limit": fmt_decimal(MAX_QUEBEC_HYDRO_QUEBEC_ACTIVE_WEIGHT_GAP, 8),
            "Status": pass_fail(gap, MAX_QUEBEC_HYDRO_QUEBEC_ACTIVE_WEIGHT_GAP),
        })

    return rows


def write_formatted_summary(path, rows, group_header):
    """Write group summary rows with consistent financial/weight formatting."""
    formatted_rows = []
    for row in rows:
        formatted_rows.append({
            group_header: row["group"],
            "Idx MV": fmt_money(row["idx_mv"]),
            "Port End MV": fmt_money(row["port_end_mv"]),
            "MV Gap": fmt_money(row["mv_gap"]),
            "Idx Weight": fmt_decimal(row["idx_weight"], 8),
            "Port Weight": fmt_decimal(row["port_weight"], 8),
            "Active Weight": fmt_decimal(row["active_weight"], 8),
            "Idx Duration": fmt_decimal(row["idx_duration"], 8),
            "Port End Duration": fmt_decimal(row["port_end_duration"], 8),
            "Duration Gap": fmt_decimal(row["duration_gap"], 8),
        })
    write_csv(
        path,
        formatted_rows,
        list(formatted_rows[0].keys()) if formatted_rows else [group_header],
    )


def main():
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Run daily XBB rebalancing optimization.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--issuer-exclusion", type=Path, default=DEFAULT_ISSUER_EXCLUSION)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--solver-time-limit", type=int, default=DEFAULT_SOLVER_TIME_LIMIT_SECONDS)
    parser.add_argument("--solver-msg", action="store_true", help="Show CBC solver output for debugging.")
    args = parser.parse_args()

    excluded_issuers = read_excluded_issuers(args.issuer_exclusion)
    holdings = read_holdings(args.input, excluded_issuers)
    model = build_model(holdings, args.solver_time_limit, args.solver_msg)
    summary = write_outputs(args.output_dir, holdings, model, args.solver_time_limit)

    print(json.dumps({
        "solver_status": summary["solver_status"],
        "portfolio": {
            "idx_mv": round(summary["portfolio"]["idx_mv"], 2),
            "port_end_mv": round(summary["portfolio"]["port_end_mv"], 2),
            "mv_gap": round(summary["portfolio"]["mv_gap"], 2),
            "idx_duration": round(summary["portfolio"]["idx_duration"], 8),
            "port_end_duration": round(summary["portfolio"]["port_end_duration"], 8),
            "duration_gap": round(summary["portfolio"]["duration_gap"], 8),
            "turnover": round(summary["portfolio"]["turnover"], 2),
            "total_traded_count": summary["portfolio"]["total_traded_count"],
        },
        "outputs": summary["outputs"],
    }, indent=2))


if __name__ == "__main__":
    main()
