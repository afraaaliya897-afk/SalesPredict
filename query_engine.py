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
import pandas as pd

from chat_pipeline import MODEL, validate_sql

# ---------------------------------------------------------------------------
# Config — change these in one place once real data is confirmed
# ---------------------------------------------------------------------------

DB_PATH = str(Path(__file__).resolve().parent / "sales_inventory.duckdb")

# Exact string in inventory_transaction.reference that means "from a sales order".
# Source column name in exports is typically "Reference".
REFERENCE_SALES_ORDER_VALUE = "Sales order"

# Loaded column name for "sale happened on this date".
# Source export is typically "Physical date" (alternative: "Financial date").
SALE_DATE_FIELD = "physical_date"

CANCELLED_STATUS_VALUE = "Cancelled"
DO_NOT_PROCESS_YES_VALUE = "Yes"

DEFAULT_GROUP_LIMIT = 10
MAX_GROUP_LIMIT = 100

# ---------------------------------------------------------------------------
# Schema — fed to the LLM so it knows what exists, not so it searches
# ---------------------------------------------------------------------------

SCHEMA_DESCRIPTION = f"""
Table: sales_order
- sales_order_number: unique order id
- customer_account, customer_name: who ordered
- order_type: type of order
- channel: Retail Store / Online / Wholesale
- status: order status (Cancelled orders should never be counted)
- do_not_process: Yes/No flag (Yes orders should never be counted)
- site, warehouse: location
- invoice_date: when it was invoiced

Table: inventory_transaction
- item_number: which product
- reference: what kind of document created this row (only "{REFERENCE_SALES_ORDER_VALUE}" rows are sales)
- number: matches sales_order.sales_order_number
- receipt: quantity received (not a sale)
- issue: quantity issued out (this is a sale quantity)
- physical_date, financial_date: dates on the transaction

Join: inventory_transaction.number = sales_order.sales_order_number,
      only where inventory_transaction.reference = '{REFERENCE_SALES_ORDER_VALUE}'
"""

# ---------------------------------------------------------------------------
# Allowed vocabulary — the ONLY things the LLM may pick. New countable thing
# later = one new line here. Compiler does not change.
# ---------------------------------------------------------------------------

ALLOWED_METRICS = {
    "issue_quantity": "SUM(it.issue)",
    "receipt_quantity": "SUM(it.receipt)",
    "order_count": "COUNT(DISTINCT so.sales_order_number)",
}

ALLOWED_DIMENSIONS = {
    "item_number": "it.item_number",
    "sale_date": f"CAST(it.{SALE_DATE_FIELD} AS DATE)",
    "customer_account": "so.customer_account",
    "channel": "so.channel",
    "site": "so.site",
    "warehouse": "so.warehouse",
}

METRIC_LABELS = {
    "issue_quantity": "Quantity sold",
    "receipt_quantity": "Quantity received",
    "order_count": "Order count",
}

DIMENSION_LABELS = {
    "item_number": "Item",
    "sale_date": "Date",
    "customer_account": "Customer",
    "channel": "Channel",
    "site": "Site",
    "warehouse": "Warehouse",
}

PLAN_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {
            "type": "string",
            "enum": list(ALLOWED_METRICS.keys()) + ["unsupported"],
        },
        "dimension": {
            "type": ["string", "null"],
            "enum": list(ALLOWED_DIMENSIONS.keys()) + [None],
        },
        "date_range_days": {"type": ["integer", "null"]},
        "sort": {"type": "string", "enum": ["asc", "desc"]},
        "limit": {"type": ["integer", "null"]},
    },
    "required": ["metric", "dimension", "date_range_days", "sort", "limit"],
}

PLAN_SYSTEM_PROMPT = f"""You turn a business question into a JSON query plan.
Respond with ONLY a JSON object. No markdown, no explanation.

{SCHEMA_DESCRIPTION}

Allowed metric values: {", ".join(ALLOWED_METRICS.keys())}
Allowed dimension values: {", ".join(ALLOWED_DIMENSIONS.keys())} or null
- dimension null means one overall number (no grouping)
- date_range_days null means all history; otherwise look back that many days
- sort is "desc" for top/highest, "asc" for lowest or chronological trends
- limit is how many grouped rows to return; use null for a single number or a daily trend

If the question cannot be answered from these metrics and dimensions
(profit, margin, cost, forecast, current stock on hand, anything not listed),
return metric "unsupported" and the other fields null / "desc".

Examples:
Q: top 5 items by quantity sold in the last 90 days
{{"metric": "issue_quantity", "dimension": "item_number", "date_range_days": 90, "sort": "desc", "limit": 5}}

Q: daily sales trend this month
{{"metric": "issue_quantity", "dimension": "sale_date", "date_range_days": 30, "sort": "asc", "limit": null}}

Q: total orders this month
{{"metric": "order_count", "dimension": null, "date_range_days": 30, "sort": "desc", "limit": null}}

Q: top customers by order count
{{"metric": "order_count", "dimension": "customer_account", "date_range_days": null, "sort": "desc", "limit": 10}}

Q: what's our profit margin
{{"metric": "unsupported", "dimension": null, "date_range_days": null, "sort": "desc", "limit": null}}
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
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def get_query_plan(question: str) -> dict:
    """Ollama step 1: fill the fixed plan. Never treated as SQL."""
    import ollama

    kwargs = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "options": {"temperature": 0.0},
        "format": "json",
    }
    try:
        response = ollama.chat(**kwargs)
    except TypeError:
        kwargs.pop("format", None)
        response = ollama.chat(**kwargs)

    raw = response["message"]["content"].strip()
    try:
        plan = _extract_json(raw)
    except (json.JSONDecodeError, TypeError):
        return {
            "metric": "unsupported",
            "dimension": None,
            "date_range_days": None,
            "sort": "desc",
            "limit": None,
            "_raw": raw,
        }
    return plan


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
    if dim is not None and dim not in ALLOWED_DIMENSIONS:
        return False, f"I can't group by '{dim}'"

    sort = str(plan.get("sort") or "desc").lower()
    if sort not in ("asc", "desc"):
        return False, f"I can't sort '{plan.get('sort')}'"
    plan["sort"] = sort

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


def resolve_date_n_days_back(days: int, db_path: str = DB_PATH):
    max_date = get_max_sale_date(db_path)
    if max_date is None:
        raise ValueError("No inventory dates loaded")
    if not isinstance(max_date, datetime):
        max_date = pd.Timestamp(max_date).to_pydatetime()
    return max_date - timedelta(days=int(days))


# ---------------------------------------------------------------------------
# Fixed compiler — does not change per question
# ---------------------------------------------------------------------------

def build_sql(plan: dict, db_path: str = DB_PATH) -> tuple[str, list]:
    metric_sql = ALLOWED_METRICS[plan["metric"]]
    dim = plan.get("dimension")
    params: list = []

    select_cols = f"{ALLOWED_DIMENSIONS[dim]} AS group_value, " if dim else ""
    sql = f"""
        SELECT {select_cols}{metric_sql} AS metric_value
        FROM inventory_transaction it
        JOIN sales_order so ON it.number = so.sales_order_number
        WHERE it.reference = ?
          AND so.status != ?
          AND so.do_not_process != ?
    """
    params.extend(
        [REFERENCE_SALES_ORDER_VALUE, CANCELLED_STATUS_VALUE, DO_NOT_PROCESS_YES_VALUE]
    )

    if plan.get("date_range_days"):
        sql += f" AND it.{SALE_DATE_FIELD} >= ?"
        params.append(resolve_date_n_days_back(plan["date_range_days"], db_path=db_path))

    if dim:
        sql += f" GROUP BY {ALLOWED_DIMENSIONS[dim]}"
        # Time series is always chronological. Rankings use the plan's sort.
        if dim == "sale_date":
            sql += " ORDER BY group_value ASC"
        else:
            sort = plan.get("sort", "desc").upper()
            sql += f" ORDER BY metric_value {sort}"
            limit = plan.get("limit") or DEFAULT_GROUP_LIMIT
            sql += " LIMIT ?"
            params.append(int(limit))

    return sql.strip(), params


def infer_chart_type(plan: dict) -> str:
    if plan.get("dimension") == "sale_date":
        return "line"
    if plan.get("dimension"):
        return "bar"
    return "stat"


def execute(sql: str, params=None, db_path: str = DB_PATH) -> pd.DataFrame:
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(sql, params or []).fetchdf()
    finally:
        con.close()
    return df


def explain_result(question: str, plan: dict, df: pd.DataFrame) -> str:
    import ollama

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
    if skip_llm_explain:
        answer_text = _fallback_answer(question, used_plan, df)
    else:
        try:
            answer_text = explain_result(question, used_plan, df)
        except Exception:
            answer_text = _fallback_answer(question, used_plan, df)

    return {
        "answer_text": answer_text,
        "chart_type": chart_type,
        "chart_data": _chart_payload(df) if chart_type != "stat" else {"labels": [], "values": []},
        "table_data": _table_payload(used_plan, df),
        "plan_used": {
            "metric": used_plan.get("metric"),
            "dimension": used_plan.get("dimension"),
            "date_range_days": used_plan.get("date_range_days"),
            "sort": used_plan.get("sort"),
            "limit": used_plan.get("limit"),
        },
        "debug": debug,
        "metric_label": METRIC_LABELS.get(used_plan["metric"], used_plan["metric"]),
        "dimension_label": DIMENSION_LABELS.get(used_plan.get("dimension"), used_plan.get("dimension")),
    }
