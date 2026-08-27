"""
Generalized query engine for sales-order + inventory-transaction chat.

The LLM never writes SQL. It fills a small fixed plan; this module validates
that plan against allowlists and compiles it with one SQL template.

Config at the top — two values are still unconfirmed against real data:
  REFERENCE_SALES_ORDER_VALUE  — Reference field value that means "this row
                                 came from a sales order"
  SALE_DATE_FIELD              — which inventory date is "the sale happened on"

Requires: duckdb, ollama, pandas
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import ollama
import pandas as pd

# ---------------------------------------------------------------------------
# Config — change these in one place once real data is confirmed
# ---------------------------------------------------------------------------

DB_PATH = str(Path(__file__).resolve().parent / "sales_inventory.duckdb")

# Ollama model for query planning. deepseek-r1:14b ran 100% CPU on this
# machine (no NVIDIA GPU) and took ~157s; with think disabled it returned
# empty JSON. llama3.2:3b is the local model that can plan in a few seconds.
MODEL = "llama3.2:3b"

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
Table: sales_order (all placed orders)
- sales_order_number: unique order id
- customer_account, customer_name: who ordered
- order_type: type of order
- channel: Retail Store / Online / Wholesale
- status: order status (Cancelled orders should never be counted)
- do_not_process: Yes/No flag (Yes orders should never be counted)
- site, warehouse: location
- invoice_date: when it was invoiced
- NOTE: This table has NO item-level detail (no item_number)

Table: inventory_transaction (actual inventory movements)
- item_number: which product was moved
- reference: what kind of document (only "{REFERENCE_SALES_ORDER_VALUE}" rows are sales)
- quantity: inventory change (negative = issued out/sold, positive = received)
- number: matches sales_order.sales_order_number
- physical_date, financial_date: dates on the transaction
- issue: movement status (only "{ISSUE_SOLD_VALUE}" means completed sale; "On order" is reserved, not sold)

IMPORTANT DISTINCTION:
- "order_count" metric: counts orders from sales_order table (all placed orders, no item detail)
- "issue_quantity" metric: counts actual sold items from inventory_transaction (has item detail)
- You CANNOT group "order_count" by "item_number" because sales_order has no items
- You CAN group "issue_quantity" by "item_number" because inventory_transaction has items

Join: inventory_transaction.number = sales_order.sales_order_number,
      only where inventory_transaction.reference = '{REFERENCE_SALES_ORDER_VALUE}'
      AND inventory_transaction.issue = '{ISSUE_SOLD_VALUE}'
      AND inventory_transaction.quantity < 0
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


def _llm_query_plan(question: str, db_path: str = DB_PATH) -> dict:
    """LLM fills period tokens only. It does not compute SQL dates."""
    as_of = get_max_sale_date(db_path)
    as_of_s = _as_datetime(as_of).date().isoformat() if as_of is not None else "unknown"
    system = (
        PLAN_SYSTEM_PROMPT
        + f"\nThe loaded data's latest sale date is {as_of_s}. "
        "this_month / last_month / last_n_months are relative to that date, not today's clock. "
        "Never put calendar dates in the JSON. Python will compute the SQL date filter from your period token."
    )
    kwargs = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "options": {"temperature": 0.0, "num_predict": 256},
        "format": "json",
    }
    started = datetime.utcnow()
    try:
        response = ollama.chat(**kwargs)
    except TypeError:
        kwargs.pop("format", None)
        response = ollama.chat(**kwargs)

    elapsed_ms = round((datetime.utcnow() - started).total_seconds() * 1000, 1)
    print(f"LLM plan {MODEL}: {elapsed_ms} ms")
    raw = response["message"]["content"].strip()
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


def explain_result(question: str, plan: dict, df: pd.DataFrame) -> str:
    result_text = df.to_string(index=False) if len(df) else "(no rows returned)"
    response = ollama.chat(
        model=MODEL,
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
        options={"temperature": 0.0},
    )
    return response["message"]["content"].strip()


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


def _handle_forecast(question: str, plan: dict, debug: dict, skip_llm_explain: bool = False) -> dict:
    """Handle forecast requests using forecast_service."""
    try:
        from forecast_service import FORECAST_MODEL_IDS, build_monthly_chart, generate_forecast
    except ImportError:
        return {
            "answer_text": "Forecasting module not available. Please install Prophet: pip install prophet",
            "chart_type": None,
            "chart_data": {"labels": [], "values": []},
            "table_data": [],
            "plan_used": plan,
            "debug": debug,
        }

    days_back = plan.get("date_range_days") or 365
    days_ahead = plan.get("limit") or 365
    use_monthly = days_ahead >= 180

    debug["forecast_config"] = {
        "days_back": days_back,
        "days_ahead": days_ahead,
        "grain": "month" if use_monthly else "day",
    }

    try:
        forecast_result = generate_forecast(days_back=days_back, days_ahead=days_ahead)

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

        if use_monthly:
            chart_data = build_monthly_chart({"historical": historical, "forecast": forecast})
            monthly_vals = [v for v in chart_data.get("forecast") or [] if v is not None]
            avg_forecast = sum(monthly_vals) / len(monthly_vals) if monthly_vals else 0
            grain_text = (
                f"Predicted average: {avg_forecast:.0f} units/month over the next "
                f"{chart_data.get('forecast_months', 12)} months, "
                f"using {chart_data.get('history_months', 12)} months of history. "
            )
            metric_label = "Quantity (units/month)"
        else:
            chart_data = _daily_forecast_chart(historical, forecast)
            avg_forecast = sum(row["quantity"] for row in forecast) / len(forecast) if forecast else 0
            grain_text = (
                f"Predicted average: {avg_forecast:.1f} units/day for the next {days_ahead} days, "
                f"using {len(historical)} days of history. "
            )
            metric_label = "Quantity (units/day)"

        models = []
        for c in candidates:
            name = c.get("name")
            rows = model_forecasts.get(name)
            if not rows:
                continue
            series = (
                build_monthly_chart({"historical": historical, "forecast": rows})
                if use_monthly
                else _daily_forecast_chart(historical, rows)
            )
            models.append({
                "id": c.get("id") or FORECAST_MODEL_IDS.get(name, name),
                "name": name,
                "wape": c.get("wape"),
                "winner": name == model,
                "forecast": series.get("forecast") or [],
                "lower": series.get("lower") or [],
                "upper": series.get("upper") or [],
            })

        chart_data["model"] = model
        chart_data["selection"] = selection
        chart_data["candidates"] = candidates
        chart_data["models"] = models
        chart_data["view"] = "all"

        score_bits = []
        for c in candidates:
            wape = c.get("wape")
            mark = " (best)" if c.get("name") == model else ""
            if wape is None:
                score_bits.append(f"{c['name']}: n/a{mark}")
            else:
                score_bits.append(f"{c['name']}: {wape:.1%} WAPE{mark}")
        scores_text = "; ".join(score_bits)
        answer_text = (
            f"All three models are on the chart. Best backtest: {model} ({selection}). "
            f"{grain_text}"
            f"Click a model to focus it. Backtest: {scores_text}."
        )

        debug["forecast_summary"] = {
            "model": model,
            "selection": selection,
            "candidates": candidates,
            "historical_days": len(historical),
            "forecast_days": len(forecast),
            "avg_forecast": round(avg_forecast, 2),
            "grain": chart_data.get("grain"),
        }

        if not skip_llm_explain:
            _log_forecast(question, plan, forecast_result, chart_data)

        return {
            "answer_text": answer_text,
            "chart_type": "forecast",
            "chart_data": chart_data,
            "table_data": _forecast_table_payload(chart_data, forecast, view="all"),
            "plan_used": plan,
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


def _forecast_table_payload(chart_data: dict, daily_forecast: list, view: str = "all") -> list[dict]:
    """One row per chart point so View as table matches the chart grain."""
    labels = chart_data.get("labels") or []
    hist = chart_data.get("historical") or []
    models = chart_data.get("models") or []

    def _num(series, idx):
        if idx >= len(series):
            return None
        val = series[idx]
        if val is None:
            return None
        return round(float(val), 1)

    if labels and (chart_data.get("grain") == "month" or models):
        focused = next((m for m in models if m.get("id") == view), None) if view and view != "all" else None
        rows = []
        for i, label in enumerate(labels):
            row = {"date": str(label), "actual": _num(hist, i)}
            if focused:
                row["quantity"] = _num(focused.get("forecast") or [], i)
                row["lower"] = _num(focused.get("lower") or [], i)
                row["upper"] = _num(focused.get("upper") or [], i)
            elif models:
                for m in models:
                    row[m.get("id") or m.get("name")] = _num(m.get("forecast") or [], i)
            else:
                row["quantity"] = _num(chart_data.get("forecast") or [], i)
                row["lower"] = _num(chart_data.get("lower") or [], i)
                row["upper"] = _num(chart_data.get("upper") or [], i)
            rows.append(row)
        return rows

    rows = []
    for row in daily_forecast or []:
        rows.append({
            "date": _iso_date(row.get("date")),
            "quantity": round(float(row.get("quantity") or 0), 1),
            "lower": round(float(row.get("lower") or 0), 1),
            "upper": round(float(row.get("upper") or 0), 1),
        })
    return rows


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


def answer_question(
    question: str,
    db_path: str = DB_PATH,
    plan: dict | None = None,
    skip_llm_explain: bool = False,
) -> dict:
    """
    Full pipeline. Pass `plan` to skip the LLM (tests). Returns the chat JSON shape.
    """
    used_plan = plan if plan is not None else get_query_plan(question)
    debug = {"query_plan": used_plan}

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
