"""
Sales chat query engine.

Chat path: the LLM writes DuckDB SQL against v_orders / v_sold (see text_to_sql.py).
Eval path: pass an injected plan to keep the old compiler for tests.
"""

from __future__ import annotations

import json
import re
import threading
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from src.core.llm_router import call_llm, list_available_models, resolve_model as resolve_model_router

# ---------------------------------------------------------------------------
# Config — change these in one place once real data is confirmed
# ---------------------------------------------------------------------------

DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "sales_inventory.duckdb")
PARALLEL_COMPARISON_PATH = Path(__file__).resolve().parent.parent.parent / "parallel_comparison_log.jsonl"

# Default model (will be resolved by llm_router)
MODEL = "llama3.2:3b"


def list_chat_models() -> list[dict]:
    """Get list of all available chat models from llm_router."""
    return list_available_models()


def resolve_model(requested: str | None) -> str:
    """Resolve requested model to an available model."""
    return resolve_model_router(requested)

# Exact string in inventory_transaction.reference that means "from a sales order".
# Source column name in exports is typically "Reference".
REFERENCE_SALES_ORDER_VALUE = "Sales order"

# Exact string in inventory_transaction.issue that means the stock actually left.
# "On order" / "Reserved *" rows can also have negative quantity — they are not sold yet.
ISSUE_SOLD_VALUE = "Sold"

# Loaded column name for "sale happened on this date".
# Source export is typically "Physical date" (alternative: "Financial date").
SALE_DATE_FIELD = "physical_date"

# D365 export uses US spelling "Canceled". Keep both so a UK extract still filters.
CANCELLED_STATUS_VALUES = ("Canceled", "Cancelled")
DO_NOT_PROCESS_YES_VALUE = "Yes"

DEFAULT_GROUP_LIMIT = 10
MAX_GROUP_LIMIT = 100

# ---------------------------------------------------------------------------
# Schema — fed to the LLM so it knows what exists, not so it searches
# ---------------------------------------------------------------------------

SCHEMA_DESCRIPTION = f"""
Chat uses two views only (never raw Excel tables).

VIEW v_orders — sales order headers (one row per order)
- sales_order_number: unique order id (same value as inventory Number)
- customer_account, customer_name: who ordered
- order_type: already filtered to 'Sales order' (Returned order is excluded)
- invoice_account: invoice customer (usually same as customer_account)
- channel: only GC001 or NULL — both are valid; NULL is not missing data
- status: Invoiced / Open order / Delivered (Canceled already excluded)
- release_status: already filtered to Open
- do_not_process: already filtered to No
- sales_taker: who took the order
- site, warehouse: order location
- invoice_date: when it was invoiced
- This view has NO items and NO quantities

VIEW v_sold — completed unit sales (inventory lines that are sales)
- item_number / product_number: same SKU (use either; prefer item_number)
- sale_date: Physical date (when the sale happened)
- sales_order_number: inventory Number = v_orders.sales_order_number
- sold_qty: units sold, already positive. SUM(sold_qty) for quantity
- unit: unit of measure
- cost_amount: inventory cost (not selling price / not revenue)
- site, warehouse: inventory location
- customer_account, customer_name, invoice_account, channel, sales_taker: from the order header

Join already applied: inventory.Number = sales_order."Sales order"
AND inventory.Reference = 'Sales order' only.
Transfers, purchases, BOM, and other Reference types are not in these views.
"""

# ---------------------------------------------------------------------------
# Allowed vocabulary — the ONLY things the LLM may pick. New countable thing
# later = one new line here. Compiler does not change.
# ---------------------------------------------------------------------------

# Track which table each metric queries
METRIC_SOURCE = {
    "order_count": "sales_order_only",      # all placed orders, regardless of fulfillment
    "issue_quantity": "inventory_joined",   # only actually-completed sales
    "forecast_sales": "forecast",           # time series forecast (no SQL, uses Prophet)
}

ALLOWED_METRICS = {
    "order_count": "COUNT(DISTINCT so.sales_order_number)",
    "issue_quantity": "SUM(-it.quantity)",  # quantity is negative for issues, so -(-5) = 5 units sold
    "forecast_sales": None,  # Special: triggers forecast_service instead of SQL
}

# Dimensions now depend on which table is being queried
# (can't group order_count by item — sales_order table has no item info)
DIMENSION_SQL = {
    "sales_order_only": {
        "sale_date": "CAST(so.invoice_date AS DATE)",
        "customer_account": "so.customer_account",
        "channel": "so.channel",
        "site": "so.site",
        "warehouse": "so.warehouse",
    },
    "inventory_joined": {
        "item_number": "it.item_number",
        "sale_date": f"CAST(it.{SALE_DATE_FIELD} AS DATE)",
        "customer_account": "so.customer_account",
        "channel": "so.channel",
        "site": "so.site",
        "warehouse": "so.warehouse",
    },
}

METRIC_LABELS = {
    "issue_quantity": "Quantity sold",
    "order_count": "Order count",
    "forecast_sales": "Forecast (historical + predicted)",
}

DIMENSION_LABELS = {
    "item_number": "Item",
    "sale_date": "Date",
    "customer_account": "Customer",
    "channel": "Channel",
    "site": "Site",
    "warehouse": "Warehouse",
}

# Get all unique dimension keys for LLM schema
_ALL_DIMENSIONS = set()
for dims in DIMENSION_SQL.values():
    _ALL_DIMENSIONS.update(dims.keys())

PLAN_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {
            "type": "string",
            "enum": list(ALLOWED_METRICS.keys()) + ["unsupported"],
        },
        "dimension": {
            "type": ["string", "null"],
            "enum": list(_ALL_DIMENSIONS) + [None],
        },
        "period": {
            "type": "string",
            "enum": ["all", "this_week", "this_month", "this_year", "last_week", "last_month", "last_year", "last_n", "last_n_months"],
        },
        "date_range_days": {"type": ["integer", "null"]},
        "sort": {"type": "string", "enum": ["asc", "desc"]},
        "limit": {"type": ["integer", "null"]},
    },
    "required": ["metric", "dimension", "period", "date_range_days", "sort", "limit"],
}

PLAN_SYSTEM_PROMPT = f"""You fill a query plan form. You never write SQL or calendar dates.
Respond with ONLY a JSON object. No markdown, no explanation.

{SCHEMA_DESCRIPTION}

Allowed metric values: {", ".join(ALLOWED_METRICS.keys())}
Allowed dimension values: {", ".join(sorted(_ALL_DIMENSIONS))} or null
Allowed period values: all, this_week, last_week, this_month, last_month, this_year, last_year, last_n, last_n_months
- dimension null means one overall number (no grouping)
- period "this_month" is the calendar month of the latest sale date, not rolling 30 days
- period "last_month" is the calendar month before that
- period "last_n_months" is the last N calendar months including the current month; date_range_days holds N
- period "last_n" is a rolling day window; date_range_days holds the day count
- period "all" means no date filter — never use it if the question named a time range
- sort is "desc" for top/highest, "asc" for lowest or chronological trends
- limit is how many grouped rows to return; use null for a single number or a daily trend

IMPORTANT CONSTRAINTS:
- "order_count" metric: can group by customer, channel, site, warehouse, sale_date
- "order_count" metric: CANNOT group by "item_number" (sales_order has no items)
- "issue_quantity" metric: can group by item_number, customer, channel, site, warehouse, sale_date
- "forecast_sales" metric: ONLY for forecast/prediction questions, dimension must be null

If the question asks for FORECAST or PREDICTION:
return metric "forecast_sales", dimension null, period all, date_range_days 365, limit 365

If the question cannot be answered from these metrics and dimensions
(profit, margin, cost, current stock on hand, anything not listed),
return metric "unsupported" and the other fields null / "desc".

Examples:
Q: top 5 items by quantity sold in the last 90 days
{{"metric": "issue_quantity", "dimension": "item_number", "period": "last_n", "date_range_days": 90, "sort": "desc", "limit": 5}}

Q: daily sales trend this month
{{"metric": "issue_quantity", "dimension": "sale_date", "period": "this_month", "date_range_days": null, "sort": "asc", "limit": null}}

Q: total orders this month
{{"metric": "order_count", "dimension": null, "period": "this_month", "date_range_days": null, "sort": "desc", "limit": null}}

Q: total orders previous month
{{"metric": "order_count", "dimension": null, "period": "last_month", "date_range_days": null, "sort": "desc", "limit": null}}

Q: total orders from the past 2 months
{{"metric": "order_count", "dimension": null, "period": "last_n_months", "date_range_days": 2, "sort": "desc", "limit": null}}

Q: forecast sales for next month
{{"metric": "forecast_sales", "dimension": null, "period": "all", "date_range_days": 365, "sort": "asc", "limit": 365}}

Q: what's our profit margin
{{"metric": "unsupported", "dimension": null, "period": "all", "date_range_days": null, "sort": "desc", "limit": null}}
"""

EXPLAIN_SYSTEM_PROMPT = """You explain a query result to a business user in plain language.
You are given the original question, the metric/dimension used, and the result rows.
State the answer plainly. Never invent a number that is not in the result.
If the result is empty, say there is no matching data. Do not guess.
If several rows came back, mention the top few rather than picking one arbitrarily.
Keep it to 2-4 short sentences.
"""


# ---------------------------------------------------------------------------
# Plan: LLM fill-in, then validate
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict:
    text = raw.strip()
    # DeepSeek-R1 may wrap reasoning in <think>…</think> before the JSON.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def _empty_plan(**overrides) -> dict:
    plan = {
        "metric": "unsupported",
        "dimension": None,
        "period": "all",
        "date_range_days": None,
        "sort": "desc",
        "limit": None,
    }
    plan.update(overrides)
    return plan


def _llm_query_plan(question: str, db_path: str = DB_PATH, model: str | None = None) -> dict:
    """LLM fills period tokens only. It does not compute SQL dates."""
    chosen_model = resolve_model(model)
    as_of = get_max_sale_date(db_path)
    as_of_s = _as_datetime(as_of).date().isoformat() if as_of is not None else "unknown"
    system = (
        PLAN_SYSTEM_PROMPT
        + f"\nThe loaded data's latest sale date is {as_of_s}. "
        "this_month / last_month / last_n_months are relative to that date, not today's clock. "
        "Never put calendar dates in the JSON. Python will compute the SQL date filter from your period token."
    )
    
    try:
        response = call_llm(
            model=chosen_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=256,
            format_json=True,
        )
        elapsed_ms = response["llm_ms"]
        print(f"LLM plan {chosen_model}: {elapsed_ms} ms")
        raw = response["content"]
        
        # Verbose: show the plan
        import os
        if os.getenv("LLM_VERBOSE", "").lower() in ("1", "true", "yes"):
            print("\n" + "="*80)
            print("PLAN JSON:")
            print("="*80)
            print(raw[:1500])
            if len(raw) > 1500:
                print(f"... ({len(raw) - 1500} more characters) ...")
            print("="*80 + "\n")
    except Exception as e:
        print(f"LLM plan failed: {e}")
        return _empty_plan(_raw=str(e), _source="llm_error", _llm_ms=0)
    
    try:
        plan = _extract_json(raw)
    except (json.JSONDecodeError, TypeError):
        return _empty_plan(_raw=raw, _source="llm_parse_failed", _llm_ms=elapsed_ms)
    plan["_source"] = "llm"
    plan["_llm_ms"] = elapsed_ms
    return plan


def get_query_plan(question: str, db_path: str = DB_PATH) -> dict:
    """LLM fills the plan form. No per-question special cases."""
    return _llm_query_plan(question, db_path=db_path)


def validate_plan(plan: dict) -> tuple[bool, str | None]:
    if not isinstance(plan, dict):
        return False, "I don't have data for that"

    metric = plan.get("metric")
    if metric not in ALLOWED_METRICS:
        if metric in (None, "unsupported", "unknown"):
            return False, "I don't have data for that"
        return False, f"I don't have data for '{metric}'"

    dim = plan.get("dimension")
    if dim in ("", "null"):
        dim = None
        plan["dimension"] = None
    
    # Check if dimension is compatible with metric's source table
    if dim is not None:
        source = METRIC_SOURCE[metric]
        # Forecast doesn't use dimensions
        if source != "forecast" and source in DIMENSION_SQL:
            if dim not in DIMENSION_SQL[source]:
                return False, f"I can't group '{metric}' by '{dim}'"
    
    # Forecast metric requires null dimension
    if metric == "forecast_sales" and dim is not None:
        return False, "Forecasts cannot be grouped by dimension"

    sort = str(plan.get("sort") or "desc").lower()
    if sort not in ("asc", "desc"):
        return False, f"I can't sort '{plan.get('sort')}'"
    plan["sort"] = sort

    period = str(plan.get("period") or "").lower()
    if period in ("", "null", "none"):
        period = "last_n" if plan.get("date_range_days") else "all"
    if period not in (
        "all",
        "this_week",
        "last_week",
        "this_month",
        "last_month",
        "this_year",
        "last_year",
        "last_n",
        "last_n_months",
    ):
        return False, "I don't have data for that date range"
    plan["period"] = period

    days = plan.get("date_range_days")
    if days in ("", "null"):
        days = None
        plan["date_range_days"] = None
    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            return False, "I don't have data for that date range"
        if days <= 0:
            return False, "I don't have data for that date range"
        plan["date_range_days"] = days

    if period in ("last_n", "last_n_months") and not plan.get("date_range_days"):
        return False, "I don't have data for that date range"
    if period == "last_n_months" and plan.get("date_range_days"):
        plan["date_range_days"] = min(int(plan["date_range_days"]), 24)

    limit = plan.get("limit")
    if limit in ("", "null"):
        limit = None
        plan["limit"] = None
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return False, "I don't have data for that"
        if limit <= 0:
            return False, "I don't have data for that"
        plan["limit"] = min(limit, MAX_GROUP_LIMIT)

    return True, None


# ---------------------------------------------------------------------------
# Date window — relative to the data's own latest sale date, not wall-clock
# "today", so historical extracts still answer "this month" / "last 90 days".
# ---------------------------------------------------------------------------

def get_max_sale_date(db_path: str = DB_PATH):
    if SALE_DATE_FIELD not in ("physical_date", "financial_date"):
        raise ValueError(f"SALE_DATE_FIELD is not a known date column: {SALE_DATE_FIELD}")
    con = duckdb.connect(db_path, read_only=True)
    try:
        (max_date,) = con.execute(
            f"SELECT MAX({SALE_DATE_FIELD}) FROM inventory_transaction WHERE reference = ?",
            [REFERENCE_SALES_ORDER_VALUE],
        ).fetchone()
    finally:
        con.close()
    return max_date


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return pd.Timestamp(value).to_pydatetime()


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _shift_month(dt: datetime, months: int) -> datetime:
    dt = _month_start(dt)
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    return dt.replace(year=year, month=month)


def resolve_date_window(plan: dict, db_path: str = DB_PATH) -> tuple[datetime, datetime] | None:
    """Inclusive start, exclusive end, anchored to the latest sale date in the data."""
    max_date = get_max_sale_date(db_path)
    if max_date is None:
        raise ValueError("No inventory dates loaded")
    max_date = _as_datetime(max_date).replace(hour=0, minute=0, second=0, microsecond=0)
    end = max_date + timedelta(days=1)
    period = plan.get("period") or "all"

    if period == "this_month":
        start = _month_start(max_date)
    elif period == "last_month":
        this_start = _month_start(max_date)
        start = _shift_month(this_start, -1)
        end = this_start
    elif period == "this_week":
        start = max_date - timedelta(days=max_date.weekday())
    elif period == "last_week":
        this_start = max_date - timedelta(days=max_date.weekday())
        start = this_start - timedelta(days=7)
        end = this_start
    elif period == "this_year":
        start = max_date.replace(month=1, day=1)
    elif period == "last_year":
        start = max_date.replace(year=max_date.year - 1, month=1, day=1)
        end = max_date.replace(month=1, day=1)
    elif period == "last_n":
        days = plan.get("date_range_days")
        if not days:
            return None
        start = max_date - timedelta(days=int(days))
    elif period == "last_n_months":
        n = plan.get("date_range_days")
        if not n:
            return None
        this_start = _month_start(max_date)
        start = _shift_month(this_start, -(int(n) - 1))
    else:
        return None
    return start, end


def resolve_date_n_days_back(days: int, db_path: str = DB_PATH):
    max_date = get_max_sale_date(db_path)
    if max_date is None:
        raise ValueError("No inventory dates loaded")
    max_date = _as_datetime(max_date)
    return max_date - timedelta(days=int(days))


# ---------------------------------------------------------------------------
# Fixed compiler — does not change per question
# ---------------------------------------------------------------------------

def build_sql(plan: dict, db_path: str = DB_PATH) -> tuple[str, list]:
    """Compile the validated plan into SQL + params. Two query shapes:
    1. sales_order_only: for order_count (no inventory join)
    2. inventory_joined: for issue_quantity (with inventory join, Issue='Sold')
    """
    metric = plan["metric"]
    dim = plan.get("dimension")
    source = METRIC_SOURCE[metric]
    if source not in DIMENSION_SQL:
        raise ValueError(f"No SQL compiler for metric source '{source}'")
    metric_sql = ALLOWED_METRICS[metric]
    dims = DIMENSION_SQL[source]
    params: list = []

    select_cols = f"{dims[dim]} AS group_value, " if dim else ""

    cancelled_placeholders = ", ".join(["?"] * len(CANCELLED_STATUS_VALUES))

    if source == "sales_order_only":
        # Query sales_order table directly (all placed orders)
        sql = f"""
            SELECT {select_cols}{metric_sql} AS metric_value
            FROM sales_order so
            WHERE so.status NOT IN ({cancelled_placeholders})
              AND so.do_not_process != ?
        """
        params.extend([*CANCELLED_STATUS_VALUES, DO_NOT_PROCESS_YES_VALUE])
        date_col = "so.invoice_date"
    else:
        # Completed sales. Keep orphan Sales-order lines that have no header row.
        sql = f"""
            SELECT {select_cols}{metric_sql} AS metric_value
            FROM inventory_transaction it
            LEFT JOIN sales_order so ON it.number = so.sales_order_number
            WHERE it.reference = ?
              AND it.issue = ?
              AND it.quantity < 0
              AND (
                    so.sales_order_number IS NULL
                    OR (
                        so.status NOT IN ({cancelled_placeholders})
                        AND so.do_not_process != ?
                    )
                  )
        """
        params.extend([
            REFERENCE_SALES_ORDER_VALUE,
            ISSUE_SOLD_VALUE,
            *CANCELLED_STATUS_VALUES,
            DO_NOT_PROCESS_YES_VALUE
        ])
        date_col = f"it.{SALE_DATE_FIELD}"

    window = resolve_date_window(plan, db_path=db_path)
    if window:
        start, end = window
        sql += f" AND {date_col} >= ? AND {date_col} < ?"
        params.extend([start, end])

    # Add GROUP BY and ORDER BY if dimension specified
    if dim:
        sql += f" GROUP BY {dims[dim]}"
        # Time series is always chronological. Rankings use the plan's sort.
        if dim == "sale_date":
            sql += " ORDER BY group_value ASC"
        else:
            sort = plan.get("sort", "desc").upper()
            sql += f" ORDER BY metric_value {sort}"
            limit = plan.get("limit")
            if limit:
                sql += " LIMIT ?"
                params.append(int(limit))

    return sql.strip(), params


def infer_chart_type(plan: dict) -> str:
    if plan.get("dimension") == "sale_date":
        return "line"
    if plan.get("dimension"):
        return "bar"
    return "stat"


def validate_sql(sql: str) -> bool:
    """Minimal guard: single statement, SELECT/WITH-only, no write keywords.
    NOTE: still a keyword blocklist, not a real parser — consider upgrading
    to sqlglot + allowlist for production."""
    normalized = sql.strip().rstrip(";")
    if ";" in normalized:
        return False
    if not re.match(r"^\s*(SELECT|WITH)\b", normalized, re.IGNORECASE):
        return False
    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH", "COPY", "PRAGMA"]
    upper = normalized.upper()
    return not any(word in upper for word in forbidden)


def execute(sql: str, params=None, db_path: str = DB_PATH) -> pd.DataFrame:
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(sql, params or []).fetchdf()
    finally:
        con.close()
    return df


def explain_result(question: str, plan: dict, df: pd.DataFrame, model: str | None = None) -> str:
    chosen_model = resolve_model(model)
    result_text = df.to_string(index=False) if len(df) else "(no rows returned)"
    
    try:
        response = call_llm(
            model=chosen_model,
            messages=[
                {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n"
                        f"Metric: {plan.get('metric')}\n"
                        f"Dimension: {plan.get('dimension')}\n\n"
                        f"Result:\n{result_text}"
                    ),
                },
            ],
            temperature=0.0,
        )
        return response["content"]
    except Exception as e:
        print(f"LLM explain failed: {e}")
        return _fallback_answer(question, plan, df)


def _fallback_answer(question: str, plan: dict, df: pd.DataFrame) -> str:
    metric_label = METRIC_LABELS.get(plan["metric"], plan["metric"])
    dim = plan.get("dimension")
    if df is None or len(df) == 0:
        return "There is no matching data for that question."
    if not dim:
        value = df.iloc[0]["metric_value"]
        return f"{metric_label}: {value:,.0f}" if pd.notna(value) else f"{metric_label}: {value}"
    dim_label = DIMENSION_LABELS.get(dim, dim)
    parts = []
    for _, row in df.head(5).iterrows():
        parts.append(f"{row['group_value']} ({row['metric_value']:,.0f})")
    return f"{metric_label} by {dim_label}: " + "; ".join(parts) + "."


FORECAST_LOG_PATH = Path(__file__).resolve().parent / "forecasts_log.jsonl"


def _iso_date(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _log_forecast(question: str, plan: dict, forecast_result: dict, chart_data: dict) -> None:
    """Append a snapshot so later actuals can be compared (B4)."""
    winner = forecast_result.get("model")
    wape = None
    for c in forecast_result.get("candidates") or []:
        if c.get("name") == winner:
            wape = c.get("wape")
            break
    hist = forecast_result.get("historical") or []
    entry = {
        "logged_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "question": question,
        "period": plan.get("period"),
        "days_back": plan.get("date_range_days"),
        "days_ahead": plan.get("limit"),
        "model": winner,
        "wape": wape,
        "selection": forecast_result.get("selection"),
        "history_end": _iso_date(hist[-1]["date"]) if hist else None,
        "grain": chart_data.get("grain"),
        "forecast": [
            {
                "date": _iso_date(row.get("date")),
                "quantity": float(row.get("quantity", 0)),
                "lower": float(row.get("lower", 0)),
                "upper": float(row.get("upper", 0)),
            }
            for row in (forecast_result.get("forecast") or [])
        ],
    }
    try:
        with FORECAST_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        print(f"Could not write forecast log: {exc}")


_MONTH_ALIASES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_iso_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def _as_of_from_db(db_path: str) -> date:
    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            (latest,) = con.execute("SELECT MAX(sale_date) FROM v_sold").fetchone()
        finally:
            con.close()
        parsed = _parse_iso_date(latest)
        if parsed:
            return parsed
    except Exception:
        pass
    return date.today()


def _parse_named_month_range(question: str, as_of: date) -> tuple[date | None, date | None, str | None]:
    """Fallback if the LLM omits dates: pick named months from the question."""
    names = "|".join(sorted(_MONTH_ALIASES, key=len, reverse=True))
    q = question.lower()
    ranged = re.search(
        rf"({names})\s*(?:to|-|through|until|and)\s*({names})(?:\s+(\d{{4}}))?",
        q,
    )
    if ranged:
        m1, m2 = _MONTH_ALIASES[ranged.group(1)], _MONTH_ALIASES[ranged.group(2)]
        year = int(ranged.group(3)) if ranged.group(3) else as_of.year
        y2 = year + 1 if m2 < m1 and not ranged.group(3) else year
        return date(year, m1, 1), _month_end(y2, m2), "month"
    single = re.search(rf"({names})\s+(\d{{4}})", q)
    if single:
        month = _MONTH_ALIASES[single.group(1)]
        year = int(single.group(2))
        return date(year, month, 1), _month_end(year, month), "month"
    return None, None, None


def _extract_relative_forecast_span(question: str) -> tuple[int | None, str | None]:
    """Relative spans like 'next 30 days' — only used if no calendar window is given."""
    question_lower = question.lower()
    patterns = [
        (r"(\d+)\s*day", "day"),
        (r"(\d+)\s*week", "week"),
        (r"(\d+)\s*month", "month"),
        (r"(\d+)\s*quarter", "quarter"),
        (r"next\s*year", "year"),
    ]
    for pattern, unit in patterns:
        match = re.search(pattern, question_lower)
        if not match:
            continue
        if unit == "year":
            return 365, "month"
        number = int(match.group(1))
        if unit == "day":
            return number, "day" if number <= 90 else "week"
        if unit == "week":
            return number * 7, "week" if number <= 12 else "month"
        if unit == "month":
            return number * 30, "month"
        if unit == "quarter":
            return number * 90, "month"
    if "next month" in question_lower:
        return 30, "day"
    if "next quarter" in question_lower:
        return 90, "month"
    if "next year" in question_lower:
        return 365, "month"
    return None, None


def _resolve_forecast_window(question: str, llm_window: dict | None, as_of: date) -> dict:
    """Build the forecast window from the LLM reply, then fall back to the question text."""
    start = end = None
    grain = None
    source = "default"

    if llm_window:
        start = _parse_iso_date(llm_window.get("start"))
        end = _parse_iso_date(llm_window.get("end"))
        grain = llm_window.get("grain")
        if start and end:
            source = "llm"

    if not (start and end):
        start, end, parsed_grain = _parse_named_month_range(question, as_of)
        if start and end:
            grain = grain or parsed_grain
            source = "question_months"

    question_l = (question or "").lower()
    if not (start and end) and re.search(r"\bnext month\b", question_l) and not re.search(r"\bnext\s+\d+\s+months?\b", question_l):
        if as_of.month == 12:
            start = date(as_of.year + 1, 1, 1)
        else:
            start = date(as_of.year, as_of.month + 1, 1)
        end = date(start.year, start.month, monthrange(start.year, start.month)[1])
        grain = grain or "month"
        source = "next_calendar_month"

    if not (start and end):
        days, rel_grain = _extract_relative_forecast_span(question)
        if days:
            start = as_of + timedelta(days=1)
            end = as_of + timedelta(days=days)
            grain = grain or rel_grain
            source = "question_relative"

    if not (start and end):
        start = as_of + timedelta(days=1)
        end = as_of + timedelta(days=365)
        grain = grain or "month"
        source = "default"

    if end < start:
        start, end = end, start

    if grain not in ("day", "week", "month"):
        grain = "day" if (end - start).days <= 90 else "month"

    days_ahead = max(1, (end - as_of).days)
    return {
        "start": start,
        "end": end,
        "grain": grain,
        "days_ahead": days_ahead,
        "source": source,
    }


def _clip_chart_to_window(chart_data: dict, start: date, end: date) -> dict:
    """Keep only the dates the user asked for."""
    labels = chart_data.get("labels") or []
    start_s, end_s = start.isoformat(), end.isoformat()
    start_m, end_m = start.strftime("%Y-%m"), end.strftime("%Y-%m")

    keep = []
    for lab in labels:
        token = str(lab)
        if re.match(r"^\d{4}-\d{2}$", token):
            keep.append(start_m <= token <= end_m)
        else:
            keep.append(start_s <= token[:10] <= end_s)

    if not any(keep):
        return chart_data

    def clip(arr):
        if not arr:
            return arr
        return [arr[i] for i in range(len(arr)) if i < len(keep) and keep[i]]

    chart_data["labels"] = clip(labels)
    for key in ("historical", "forecast", "lower", "upper"):
        if key in chart_data:
            chart_data[key] = clip(chart_data[key])
    for model in chart_data.get("models") or []:
        for key in ("forecast", "lower", "upper"):
            if key in model:
                model[key] = clip(model[key])
    if chart_data.get("grain") == "month":
        hist = chart_data.get("historical") or []
        fc = chart_data.get("forecast") or []
        chart_data["history_months"] = sum(1 for v in hist if v is not None)
        chart_data["forecast_months"] = sum(1 for v in fc if v is not None)
    return chart_data


def _rank_limit_from_question(question: str) -> int:
    match = re.search(r"\btop\s+(\d+)\b", question or "", re.I)
    if match:
        return max(1, min(15, int(match.group(1))))
    return 5


def _handle_item_rank_forecast(question: str, window: dict, plan: dict, debug: dict) -> dict:
    from src.core.forecast_service import forecast_item_ranking
    from src.core.text_to_sql import is_item_rank_forecast

    if not is_item_rank_forecast(question):
        return None
    ranked = forecast_item_ranking(window["start"], window["end"], limit=_rank_limit_from_question(question))
    items = ranked.get("items") or []
    if window["start"].strftime("%Y-%m") == window["end"].strftime("%Y-%m"):
        period = window["start"].strftime("%b %Y")
    else:
        period = f"{window['start'].strftime('%b %Y')}–{window['end'].strftime('%b %Y')}"
    debug["forecast_config"] = {
        "style": "item_rank",
        "start": window["start"].isoformat(),
        "end": window["end"].isoformat(),
        "model": ranked.get("model"),
        "prior_start": ranked.get("prior_start"),
        "prior_end": ranked.get("prior_end"),
    }
    if not items:
        return {
            "answer_text": (
                f"No item-level history for the same period last year "
                f"({ranked.get('prior_start')} to {ranked.get('prior_end')}), so I cannot forecast {period}."
            ),
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": {**plan, "forecast_start": window["start"].isoformat(), "forecast_end": window["end"].isoformat()},
            "debug": debug,
        }
    top = items[0]
    answer_text = (
        f"For {period}, {top['item_number']} sold the most in the same period last year "
        f"({ranked.get('prior_start')} to {ranked.get('prior_end')}): "
        f"{top['forecast_qty']:,.0f} units. "
        f"That is a straight replay of history ({ranked['method']}) — not a growth forecast "
        f"and not invented by the chat model."
    )
    return {
        "answer_text": answer_text,
        "chart_type": "bar",
        "chart_data": {
            "labels": [row["item_number"] for row in items],
            "values": [row["forecast_qty"] for row in items],
        },
        "table_data": [
            {"item_number": row["item_number"], "forecast_qty": row["forecast_qty"]}
            for row in items
        ],
        "plan_used": {
            **plan,
            "forecast_start": window["start"].isoformat(),
            "forecast_end": window["end"].isoformat(),
            "model": ranked.get("model"),
        },
        "metric_label": "Forecast units",
        "dimension_label": "Item",
        "debug": debug,
    }


def _handle_forecast(question: str, plan: dict, debug: dict, skip_llm_explain: bool = False) -> dict:
    """Handle forecast requests using forecast_service."""
    try:
        from src.core.forecast_service import (
            build_demand_planning_view,
            generate_forecast,
            resolve_forecast_item,
        )
    except ImportError:
        return {
            "answer_text": "Forecasting module not available. Please install Prophet: pip install prophet",
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": plan,
            "debug": debug,
        }

    as_of = _as_of_from_db(DB_PATH)
    window = _resolve_forecast_window(question, plan.get("forecast_window"), as_of)
    ranked = _handle_item_rank_forecast(question, window, plan, debug)
    if ranked is not None:
        return ranked
    llm_item = (plan.get("forecast_window") or {}).get("item")
    scope = resolve_forecast_item(question, llm_item)
    if scope.get("error"):
        return {
            "answer_text": scope["error"],
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": plan,
            "debug": {**debug, "forecast_scope": scope},
        }

    days_back = None
    days_ahead = window["days_ahead"]
    grain = window["grain"]
    use_monthly = grain == "month" or (window["end"] - window["start"]).days > 90
    item_number = scope.get("item_number")
    scope_label = scope.get("label") or "all sold items"

    debug["forecast_config"] = {
        "as_of": as_of.isoformat(),
        "start": window["start"].isoformat(),
        "end": window["end"].isoformat(),
        "days_back": "all_available",
        "days_ahead": days_ahead,
        "grain": grain,
        "window_source": window["source"],
        "item_number": item_number,
        "scope": scope_label,
    }

    try:
        forecast_result = generate_forecast(
            days_back=days_back,
            days_ahead=days_ahead,
            horizon="month" if use_monthly else "day",
            item_number=item_number,
        )
        debug["forecast_config"]["history_start"] = forecast_result.get("history_start")
        debug["forecast_config"]["history_end"] = forecast_result.get("history_end")
        debug["forecast_config"]["history_days"] = forecast_result.get("history_days")

        if "error" in forecast_result:
            return {
                "answer_text": f"Forecast failed: {forecast_result['error']}",
                "chart_type": None,
                "chart_data": {"labels": [], "values": []},
                "table_data": [],
                "plan_used": plan,
                "debug": debug,
            }

        historical = forecast_result.get("historical", [])
        forecast = forecast_result.get("forecast", [])
        model = forecast_result.get("model", "Prophet")
        candidates = forecast_result.get("candidates") or []
        selection = forecast_result.get("selection") or ""
        model_forecasts = forecast_result.get("model_forecasts") or {model: forecast}

        winner_wape = None
        for c in candidates:
            if c.get("name") == model and c.get("wape") is not None:
                winner_wape = c["wape"]
                break

        chart_data = build_demand_planning_view(
            historical,
            forecast,
            window["start"],
            window["end"],
            grain="month" if use_monthly else "day",
            model=model,
            selection=selection,
            wape=winner_wape,
        )
        models = []
        winner_id = None
        for candidate in candidates:
            name = candidate.get("name")
            rows = model_forecasts.get(name)
            if not rows:
                continue
            series = build_demand_planning_view(
                historical,
                rows,
                window["start"],
                window["end"],
                grain="month" if use_monthly else "day",
                model=name,
            )
            model_id = candidate.get("id") or name
            is_winner = name == model
            if is_winner:
                winner_id = model_id
            models.append({
                "id": model_id,
                "name": name,
                "wape": candidate.get("wape"),
                "winner": is_winner,
                "baseline": bool(candidate.get("baseline")),
                "forecast": series.get("forecast") or [],
                "lower": series.get("lower") or [],
                "upper": series.get("upper") or [],
                "yoy": series.get("yoy_labels") or [],
                "peak": series.get("peak"),
                "forecast_label": series.get("forecast_label"),
                "table_forecast_label": (series.get("planning_table") or {}).get("forecast_label"),
            })

        chart_data["model"] = model
        chart_data["selection"] = selection
        chart_data["candidates"] = candidates
        chart_data["models"] = models
        chart_data["view"] = winner_id or "all"
        chart_data["scope_label"] = scope_label
        chart_data["item_number"] = item_number

        period_label = f"{window['start'].strftime('%b %Y')}–{window['end'].strftime('%b %Y')}"
        metric_label = "Units per month" if chart_data.get("grain") == "month" else "Units per day"

        fc_vals = [v for v in chart_data.get("forecast") or [] if v is not None]
        avg_forecast = sum(fc_vals) / len(fc_vals) if fc_vals else 0
        hist_start = forecast_result.get("history_start")
        hist_end = forecast_result.get("history_end")
        history_span = ""
        if hist_start and hist_end:
            history_span = f" from {hist_start[:7]} through {hist_end[:7]}"

        model_label = chart_data.get("forecast_label") or model
        wape_bit = f" Holdout WAPE {winner_wape:.0%}." if winner_wape is not None else ""
        drivers = chart_data.get("drivers") or []
        why_bit = " ".join(drivers[:3]) if drivers else ""
        answer_text = (
            f"Forecast for {scope_label} ({period_label}): {model_label}.{wape_bit} "
            f"Fitted on sold units{history_span}. {selection}. "
            f"Gray = same months last year (comparison only)."
            + (f" Why up/down: {why_bit}" if why_bit else "")
        )

        debug["forecast_summary"] = {
            "model": model,
            "selection": selection,
            "candidates": candidates,
            "historical_days": len(historical),
            "forecast_days": len(forecast),
            "avg_forecast": round(avg_forecast, 2),
            "grain": chart_data.get("grain"),
            "style": "demand_planning",
            "peak": chart_data.get("peak"),
            "item_number": item_number,
            "scope": scope_label,
        }

        if not skip_llm_explain:
            _log_forecast(question, plan, forecast_result, chart_data)

        return {
            "answer_text": answer_text,
            "chart_type": "forecast",
            "chart_data": chart_data,
            "table_data": _planning_table_payload(chart_data),
            "plan_used": {
                **plan,
                "forecast_start": window["start"].isoformat(),
                "forecast_end": window["end"].isoformat(),
                "grain": grain,
                "days_ahead": days_ahead,
                "window_source": window["source"],
                "item_number": item_number,
            },
            "metric_label": metric_label,
            "dimension_label": "Date",
            "debug": debug,
        }

    except Exception as e:
        return {
            "answer_text": f"Forecast generation failed: {str(e)}",
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": plan,
            "debug": {**debug, "error": str(e)},
        }


def _daily_forecast_chart(historical: list, forecast: list) -> dict:
    hist_dates = [_iso_date(row["date"]) for row in historical]
    fc_dates = [_iso_date(row["date"]) for row in forecast]
    return {
        "labels": hist_dates + fc_dates,
        "historical": [max(0.0, float(row["quantity"])) for row in historical],
        "forecast": [None] * len(historical) + [max(0.0, float(row["quantity"])) for row in forecast],
        "lower": [None] * len(historical) + [max(0.0, float(row.get("lower", row["quantity"]))) for row in forecast],
        "upper": [None] * len(historical) + [max(0.0, float(row.get("upper", row["quantity"]))) for row in forecast],
        "grain": "day",
    }


def _planning_table_payload(chart_data: dict) -> list[dict]:
    """Transposed monthly detail: Actual / Forecast / YoY as rows, months as columns."""
    table = chart_data.get("planning_table") or {}
    columns = table.get("columns") or chart_data.get("labels") or []
    actual = table.get("actual") or chart_data.get("actual") or []
    forecast = table.get("forecast") or chart_data.get("forecast") or []
    yoy = table.get("yoy") or chart_data.get("yoy_labels") or []

    def _cells(series):
        return {str(columns[i]): (series[i] if i < len(series) else None) for i in range(len(columns))}

    return [
        {"metric": table.get("actual_label") or "Actual", **_cells(actual)},
        {"metric": table.get("forecast_label") or "Forecast", **_cells(forecast)},
        {"metric": "YoY change", **_cells(yoy)},
    ]


def _chart_payload(df: pd.DataFrame) -> dict:
    if df is None or len(df) == 0 or "group_value" not in df.columns:
        return {"labels": [], "values": []}
    labels = []
    values = []
    for _, row in df.iterrows():
        label = row["group_value"]
        if hasattr(label, "strftime"):
            labels.append(label.strftime("%Y-%m-%d"))
        else:
            labels.append(str(label))
        val = row["metric_value"]
        values.append(None if pd.isna(val) else float(val))
    return {"labels": labels, "values": values}


def _table_payload(plan: dict, df: pd.DataFrame) -> list[dict]:
    if df is None or len(df) == 0:
        return []
    dim = plan.get("dimension")
    rows = []
    for _, row in df.iterrows():
        item: dict = {}
        if dim and "group_value" in df.columns:
            val = row["group_value"]
            if hasattr(val, "strftime"):
                val = val.strftime("%Y-%m-%d")
            else:
                val = val.item() if hasattr(val, "item") else val
            item[dim] = val
        metric_val = row["metric_value"]
        if pd.isna(metric_val):
            item["metric_value"] = None
        elif hasattr(metric_val, "item"):
            item["metric_value"] = metric_val.item()
        else:
            item["metric_value"] = metric_val
        rows.append(item)
    return rows


def _infer_smart_chart_type(
    df: pd.DataFrame, 
    label_col: str = None, 
    metric_col: str = None, 
    question: str = None,
    chart_hint: str = None
) -> str:
    """Intelligently select chart type based on data characteristics and LLM suggestion.
    
    Priority order:
    1. LLM's chart_hint (if provided and valid)
    2. Data shape heuristics (date columns, row count, etc.)
    """
    # Priority 1: Use LLM's suggestion if provided
    if chart_hint and chart_hint in ['pie', 'bar', 'line', 'stat']:
        # Validate that the hint makes sense for the data
        if chart_hint == 'pie' and 2 <= len(df) <= 10:
            return 'pie'
        elif chart_hint == 'stat' and len(df) == 1 and not label_col:
            return 'stat'
        elif chart_hint == 'line' and len(df) >= 2:
            return 'line'
        elif chart_hint == 'bar' and len(df) >= 2:
            return 'bar'
        # If hint doesn't match data, fall through to heuristics
    
    # Priority 2: Data shape heuristics
    if df is None or len(df) == 0:
        return "stat"

    # Single number with no category (item, customer, …) = stat card
    if len(df) == 1 and not label_col:
        return "stat"

    # Check if we have date-like column
    dateish = [
        c for c in df.columns
        if "date" in str(c).lower() or pd.api.types.is_datetime64_any_dtype(df[c])
    ]

    # Time series data = line chart
    if dateish and len(df) >= 3:
        return "line"

    # Few items (2-15) = bar chart
    if 2 <= len(df) <= 15:
        return "bar"

    # Many items (>15) but not time series = bar (truncated) or line
    if len(df) > 15:
        # If it's ordered/ranked data, keep as bar (will be truncated)
        # If it's dense continuous data, use line
        if dateish:
            return "line"
        return "bar"

    return "bar"  # Default fallback


_TRAILING_JUNK_RE = re.compile(r"[\s\-–—]+$")


def _clean_str(value: str) -> str:
    """Trim the trailing ' -' left over from D365 name exports (e.g. 'Oliver -').

    Display-only cleanup — the dash carries no data (nothing ever follows it
    in the source export), so this never removes real information.
    """
    cleaned = _TRAILING_JUNK_RE.sub("", value.strip()).strip()
    return cleaned or value


def _payloads_from_sql_df(df: pd.DataFrame, question: str = None, chart_hint: str = None) -> tuple[dict, list, str, str, str | None, bool]:
    """Turn a free-form SQL result into chart/table payloads.
    Returns: (chart_data, table_data, chart_type, metric_label, dim_label, chart_truncated)
    """
    if df is None or len(df) == 0:
        return {"labels": [], "values": []}, [], "stat", "Value", None, False

    table = []
    for _, row in df.iterrows():
        item: dict = {}
        for col in df.columns:
            val = row[col]
            if pd.isna(val):
                item[col] = None
            elif hasattr(val, "strftime"):
                item[col] = val.strftime("%Y-%m-%d")
            elif isinstance(val, str):
                item[col] = _clean_str(val)
            elif hasattr(val, "item"):
                item[col] = val.item()
            else:
                item[col] = val
        table.append(item)

    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    dateish = [
        c for c in df.columns
        if "date" in str(c).lower() or pd.api.types.is_datetime64_any_dtype(df[c])
    ]
    metric_label = str(numeric[0]) if numeric else "Value"

    # Bare total only when the result has no item/customer/site to show.
    # "Top product" is LIMIT 1 with item_number + qty — keep the item.
    if len(df) == 1 and numeric:
        label_cols = [c for c in df.columns if c not in numeric]
        if not label_cols:
            val = df.iloc[0][numeric[0]]
            metric = None if pd.isna(val) else float(val)
            return (
                {"labels": [], "values": []},
                [{"metric_value": metric}],
                "stat",
                metric_label,
                None,
                False,
            )

    label_col = None
    if dateish:
        label_col = dateish[0]
    else:
        non_num = [c for c in df.columns if c not in numeric]
        if non_num and numeric:
            label_col = non_num[0]

    # Smart chart type inference with question context
    value_col = numeric[0] if numeric else None
    chart_type = _infer_smart_chart_type(df, label_col, value_col, question, chart_hint)

    chart_truncated = False
    if label_col and numeric:
        # Auto-limit bar/pie charts to reasonable size
        max_items = 15 if chart_type in ["bar", "pie"] else len(df)
        if len(df) > max_items:
            df_chart = df.head(max_items)
            chart_truncated = True
        else:
            df_chart = df
        
        labels = []
        values = []
        for _, row in df_chart.iterrows():
            lab = row[label_col]
            if hasattr(lab, "strftime"):
                labels.append(lab.strftime("%Y-%m-%d"))
            elif isinstance(lab, str):
                labels.append(_clean_str(lab))
            else:
                labels.append(str(lab))
            val = row[numeric[0]]
            values.append(None if pd.isna(val) else float(val))
        
        chart_data = {"labels": labels, "values": values}
        
        # Add chart-specific configuration
        if chart_type == "pie":
            chart_data["chartType"] = "pie"
        elif chart_type == "line":
            chart_data["chartType"] = "line"
            chart_data["fill"] = True  # Area chart style
        
        return chart_data, table, chart_type, metric_label, str(label_col), chart_truncated

    return {"labels": [], "values": []}, table, "stat", metric_label, None, False


def _validate_answer(answer: str, df: pd.DataFrame) -> bool:
    """Validate that answer doesn't hallucinate values not in the data."""
    if df is None or len(df) == 0:
        return True  # Empty data is fine
    
    # Extract all numbers from the answer (formatted with commas)
    answer_numbers = set()
    for match in re.finditer(r'[\d,]+', answer):
        num_str = match.group().replace(',', '')
        if num_str.isdigit():
            answer_numbers.add(int(num_str))
    
    # Extract all numbers from the dataframe
    data_numbers = set()
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            for val in df[col].dropna():
                if pd.notna(val):
                    data_numbers.add(int(float(val)))
    
    # Check if answer contains numbers not in data (allowing row count)
    hallucinated = answer_numbers - data_numbers - {len(df)}
    
    # Allow small numbers that might be counts or indices
    hallucinated = {n for n in hallucinated if n > 10}
    
    return len(hallucinated) == 0


def _sql_answer_text(df: pd.DataFrame, question: str, model: str) -> str:
    """Generate natural language answer from SQL results using LLM."""
    if df is None or len(df) == 0:
        return "There is no matching data for that question."
    
    # Format the result data for the LLM
    if len(df) <= 10:
        result_text = df.to_string(index=False)
    else:
        result_text = df.head(10).to_string(index=False) + f"\n... ({len(df)} total rows)"
    
    # Let the LLM format the answer
    system = """You are explaining query results to a business user.

Given a question and the data that answers it, write a clear, concise answer in 1-2 sentences.

CRITICAL RULES TO PREVENT HALLUCINATIONS:
1. ONLY use numbers that appear in the Data section below
2. NEVER calculate, infer, or estimate values not shown
3. If Data shows "ITEM-123", write "ITEM-123" (use exact values)
4. If Data shows a number like 1234, format it as "1,234" with commas
5. If asked for "top N" but Data shows fewer, mention all that exist
6. NEVER add context like "in Q1" or "this month" unless the question explicitly mentions it
7. For single numbers, state the metric clearly: "Total sales: 1,234 units"
8. For lists/rankings, mention all shown items with their values
9. If Data is empty or shows 0, say "No data available for that period"
10. NEVER make up item names, customer names, or any values

Examples:
Q: which item sold the most last month?
Data: 
item_number  total
ITEM-A       1189
A: ITEM-A sold the most with 1,189 units.

Q: top 3 customers by orders
Data:
customer_name  orders
Alice Corp     45
Bob Inc        38
Carol Ltd      31
A: The top 3 customers are Alice Corp (45 orders), Bob Inc (38 orders), and Carol Ltd (31 orders).

Q: total sales this month
Data: 
total
15234
A: Total sales: 15,234 units.

Q: daily sales trend this week
Data:
sale_date    daily_total
2024-01-15   450
2024-01-16   523
2024-01-17   489
A: Daily sales ranged from 450 to 523 units between Jan 15-17, 2024.

Q: sales last month
Data:
(no rows)
A: No sales data available for that period."""
    
    try:
        response = call_llm(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Question: {question}\n\nData:\n{result_text}\n\nAnswer:"},
            ],
            temperature=0.0,
            max_tokens=150,
        )
        answer = response["content"]
        
        # Validate answer against data to prevent hallucinations
        if answer and not _validate_answer(answer, df):
            # LLM hallucinated - fall back to simple format
            if len(df) == 1 and len(df.columns) == 1:
                val = df.iloc[0, 0]
                return f"{df.columns[0]}: {float(val):,.0f}" if pd.notna(val) else f"{df.columns[0]}: —"
            return f"Found {len(df)} results."
        
        return answer if answer else f"Found {len(df)} results."
    except Exception as e:
        print(f"LLM answer generation failed: {e}")
        # Fallback to simple format if LLM fails
        if len(df) == 1 and len(df.columns) == 1:
            val = df.iloc[0, 0]
            return f"{df.columns[0]}: {float(val):,.0f}" if pd.notna(val) else f"{df.columns[0]}: —"
        return f"Found {len(df)} results."



def _answer_with_sql(question: str, db_path: str, debug: dict, model: str) -> dict:
    from src.core.text_to_sql import (
        FORECAST_RE,
        MAX_SQL_RETRIES,
        UNSUPPORTED_RE,
        apply_requested_limit,
        check_sql,
        enforce_limit,
        generate_sql,
        is_forecast_question,
        log_sql_rejection,
        remember_sql,
        rewrite_sql_after_error,
    )

    if UNSUPPORTED_RE.search(question or ""):
        return {
            "answer_text": "I don't have data for that",
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": {"engine": "text_to_sql", "sql": "UNSUPPORTED"},
            "debug": {**debug, "engine": "text_to_sql", "sql_guard": {"ok": False, "reason": "unsupported metric"}},
        }

    if is_forecast_question(question or ""):
        plan = {
            "metric": "forecast_sales",
            "dimension": None,
            "period": "requested",
            "date_range_days": None,
            "sort": "asc",
            "limit": None,
            "_source": "forecast_intent",
            "forecast_window": None,
        }
        return _handle_forecast(question, plan, {**debug, "engine": "forecast", "query_plan": plan})

    generated = generate_sql(question, model, db_path)
    debug["engine"] = "text_to_sql"
    debug["model"] = model
    debug["llm_ms"] = generated.get("llm_ms")
    debug["sql_raw"] = generated.get("raw")
    chart_hint = generated.get("chart_hint")
    extracted = generated.get("sql")

    if extracted == "FORECAST" or FORECAST_RE.search(question or ""):
        plan = {
            "metric": "forecast_sales",
            "dimension": None,
            "period": "requested",
            "date_range_days": None,
            "sort": "asc",
            "limit": None,
            "_source": "llm_forecast" if extracted == "FORECAST" else "forecast_intent",
            "forecast_window": generated.get("forecast_window"),
        }
        return _handle_forecast(question, plan, {**debug, "query_plan": plan})

    if extracted == "UNSUPPORTED" or extracted is None:
        log_sql_rejection(question, extracted, "unsupported or empty SQL from LLM")
        return {
            "answer_text": "I don't have data for that",
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": {"engine": "text_to_sql", "sql": extracted},
            "debug": debug,
        }

    extracted = apply_requested_limit(extracted, question)

    ok, reason = check_sql(extracted)
    debug["sql_query"] = extracted
    debug["sql_guard"] = {"ok": ok, "reason": reason}
    if not ok:
        print(f"SQL rejected: {reason}\n{extracted}")
        log_sql_rejection(question, extracted, reason)
        return {
            "answer_text": "I couldn't safely answer that question (query validation failed).",
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": {"engine": "text_to_sql", "sql": extracted, "rejected": reason},
            "debug": debug,
        }

    attempts = []
    sql = enforce_limit(extracted)
    debug["sql_query"] = sql
    
    # Verbose: show the SQL
    import os
    if os.getenv("LLM_VERBOSE", "").lower() in ("1", "true", "yes"):
        print("\n" + "="*80)
        print("GENERATED SQL:")
        print("="*80)
        print(sql)
        print("="*80 + "\n")
    
    df = None
    last_error = None
    for attempt in range(MAX_SQL_RETRIES + 1):
        try:
            df = execute(sql, db_path=db_path)
            last_error = None
            break
        except Exception as exc:
            last_error = str(exc)
            attempts.append({"sql": sql, "error": last_error})
            print(f"SQL execute failed (attempt {attempt + 1}): {last_error}")
            if attempt >= MAX_SQL_RETRIES:
                break
            rewritten = rewrite_sql_after_error(question, extracted, last_error, model)
            debug["llm_ms"] = (debug.get("llm_ms") or 0) + (rewritten.get("llm_ms") or 0)
            extracted = rewritten.get("sql")
            if extracted in (None, "FORECAST", "UNSUPPORTED"):
                break
            extracted = apply_requested_limit(extracted, question)
            ok, reason = check_sql(extracted)
            debug["sql_guard"] = {"ok": ok, "reason": reason}
            if not ok:
                print(f"SQL retry rejected: {reason}\n{extracted}")
                log_sql_rejection(question, extracted, f"retry rejected: {reason}")
                break
            sql = enforce_limit(extracted)
            debug["sql_query"] = sql

    debug["sql_retries"] = attempts
    if df is None:
        debug["execution_error"] = last_error or "rewrite failed safety check"
        log_sql_rejection(question, sql, last_error or "rewrite failed safety check")
        return {
            "answer_text": "I couldn't run that query against the loaded data.",
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": {"engine": "text_to_sql", "sql": sql, "retries": len(attempts)},
            "debug": debug,
        }

    remember_sql(question, extracted)
    chart_data, table_data, chart_type, metric_label, dim_label, chart_truncated = _payloads_from_sql_df(df, question, chart_hint)
    debug["execution"] = {"rows_returned": int(len(df)), "columns": list(df.columns), "chart_truncated": chart_truncated}
    
    answer_text = _sql_answer_text(df, question, model)
    if chart_truncated and answer_text:
        answer_text += f" (Showing top 15 in chart; full {len(df)} results in table.)"
    
    return {
        "answer_text": answer_text,
        "chart_type": chart_type if (chart_data.get("labels") or chart_type == "stat") else None,
        "chart_data": chart_data,
        "table_data": table_data,
        "plan_used": {"engine": "text_to_sql", "sql": sql},
        "debug": debug,
        "metric_label": metric_label,
        "dimension_label": dim_label,
    }


def _topline_from_result(result: dict) -> str | None:
    if not result:
        return None
    text = result.get("answer_text")
    if text:
        return str(text)[:500]
    table = result.get("table_data") or []
    if table:
        return str(table[0])[:500]
    return None


def _run_old_engine_snapshot(question: str, db_path: str) -> dict | None:
    """Best-effort plan-compiler snapshot for parallel comparison. Never raises to caller."""
    try:
        plan = get_query_plan(question, db_path=db_path)
        ok, message = validate_plan(plan)
        if not ok:
            return {"ok": False, "reason": message, "plan": plan}
        if plan.get("metric") == "forecast_sales":
            return {
                "ok": True,
                "engine": "forecast",
                "plan": plan,
                "note": "forecast path — skipped SQL execute",
            }
        sql, params = build_sql(plan, db_path=db_path)
        if not validate_sql(sql):
            return {"ok": False, "reason": "validate_sql failed", "plan": plan, "sql": sql}
        df = execute(sql, params, db_path=db_path)
        chart_type = infer_chart_type(plan)
        return {
            "ok": True,
            "plan": {
                "metric": plan.get("metric"),
                "dimension": plan.get("dimension"),
                "period": plan.get("period"),
                "date_range_days": plan.get("date_range_days"),
                "limit": plan.get("limit"),
            },
            "sql": sql,
            "chart_type": chart_type,
            "rows": int(len(df)),
            "answer_text": _fallback_answer(question, plan, df)[:500],
            "table_head": df.head(3).to_dict(orient="records") if len(df) else [],
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def _log_parallel_comparison(question: str, result: dict, db_path: str) -> None:
    """Fire-and-forget comparison log. Failures never affect the user response."""

    def _worker():
        try:
            plan_used = result.get("plan_used") or {}
            new_sql = plan_used.get("sql") or (result.get("debug") or {}).get("sql_query")
            row = {
                "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "question": question,
                "new_engine_sql": new_sql,
                "new_engine_answer": _topline_from_result(result),
                "new_engine_chart_type": result.get("chart_type"),
                "old_engine_result": _run_old_engine_snapshot(question, db_path),
                "note": (
                    "Cross-check new_engine_* vs old_engine_result during Phase 2. "
                    "Old engine uses get_query_plan+build_sql; disagreements are expected "
                    "when the free-form SQL path is richer than the plan compiler."
                ),
            }
            with PARALLEL_COMPARISON_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            print(f"parallel_comparison_log skipped: {exc}")

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception as exc:
        print(f"parallel_comparison_log thread failed: {exc}")


def answer_question(
    question: str,
    db_path: str = DB_PATH,
    plan: dict | None = None,
    skip_llm_explain: bool = False,
    model: str | None = None,
) -> dict:
    """
    Chat uses text-to-SQL. Pass `plan` to skip the LLM (eval / compiler tests).
    """
    debug: dict = {}
    chosen = resolve_model(model)
    debug["model"] = chosen
    if plan is None:
        result = _answer_with_sql(question, db_path, debug, chosen)
        _log_parallel_comparison(question, result, db_path)
        return result

    used_plan = plan
    debug["query_plan"] = used_plan

    ok, message = validate_plan(used_plan)
    if not ok:
        return {
            "answer_text": message,
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": used_plan,
            "debug": debug,
        }

    # Check if this is a forecast request
    if used_plan.get("metric") == "forecast_sales":
        return _handle_forecast(question, used_plan, debug, skip_llm_explain)

    sql, params = build_sql(used_plan, db_path=db_path)
    debug["sql_query"] = sql
    debug["sql_params"] = [str(p) for p in params]

    if not validate_sql(sql):
        return {
            "answer_text": "I couldn't safely answer that question (query validation failed).",
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": used_plan,
            "debug": debug,
        }

    df = execute(sql, params, db_path=db_path)
    debug["execution"] = {
        "rows_returned": int(len(df)),
        "columns": list(df.columns),
    }

    chart_type = infer_chart_type(used_plan)
    # Numbers always come from the result table, never from the LLM.
    answer_text = _fallback_answer(question, used_plan, df)

    return {
        "answer_text": answer_text,
        "chart_type": chart_type,
        "chart_data": _chart_payload(df) if chart_type != "stat" else {"labels": [], "values": []},
        "table_data": _table_payload(used_plan, df),
        "plan_used": {
            "metric": used_plan.get("metric"),
            "dimension": used_plan.get("dimension"),
            "period": used_plan.get("period"),
            "date_range_days": used_plan.get("date_range_days"),
            "sort": used_plan.get("sort"),
            "limit": used_plan.get("limit"),
        },
        "debug": debug,
        "metric_label": METRIC_LABELS.get(used_plan["metric"], used_plan["metric"]),
        "dimension_label": DIMENSION_LABELS.get(used_plan.get("dimension"), used_plan.get("dimension")),
    }
