"""
Chat pipeline — all 13 intents RECOGNIZED, 4 fully IMPLEMENTED.

Implemented for real (need only sales_lines/items, which exist):
    TOP_SELLING_ITEM   - which item sold the most (optionally in a date range)
    ITEM_SALES_LOOKUP  - sales for a specific item (matched by name/keyword)
    SALES_TREND        - which items are growing / declining recently
    SLOW_MOVING_ITEMS  - which items have the lowest recent sales activity

Recognized but NOT implemented yet — these return an honest explanation of
what's missing instead of guessing or crashing (see NOT_YET_AVAILABLE below):
    CATEGORY_REVENUE_RANKING  - no category field exists in this dataset
    SEASONAL_DEMAND           - needs the forecasting pipeline (not built)
    SALES_FORECAST            - needs the forecasting pipeline (not built)
    CURRENT_STOCK_LOOKUP      - needs an inventory table (not built)
    LOW_STOCK_ITEMS           - needs an inventory table (not built)
    OVERSTOCK_ITEMS           - needs an inventory table (not built)
    EXCESS_STOCK_FLAG         - needs an inventory table (not built)
    PURCHASE_RECOMMENDATION   - needs inventory + forecast (not built)
    STOCKOUT_PROJECTION       - needs inventory + forecast (not built)

Why this split instead of stubbing all 13 with fake logic: implementing an
intent against data that doesn't exist means either a crash (querying a table
that isn't there) or a fabricated answer (guessing at stock levels that were
never loaded). Neither is acceptable — recognizing the intent and saying
plainly what's missing is the honest middle ground, and it's exactly the
"I don't have enough reliable data" behavior called for in the original brief.

Flow (unchanged), shared across all implemented intents:
    question -> Ollama (intent + entities) -> structured plan (dict)
             -> plan_to_sql()   [deterministic, NOT the LLM] -> (sql, params)
             -> validate_sql()  [must be a single read-only SELECT]
             -> execute()       [DuckDB, read-only, PARAMETERIZED]
             -> Ollama (explain result in plain language)

Requires: duckdb, ollama   (pip install duckdb ollama)
Requires Ollama running locally with a pulled model, e.g.:
    ollama pull qwen2.5-coder:7b
"""

import json
import re
import datetime
import duckdb
import ollama

DB_PATH = "sales_inventory.duckdb"
MODEL = "llama3.2:3b"  # change to whatever you pulled

# Confirmed from your real data (sparsity analysis) — non-product codes to
# always exclude from sales/forecasting logic.
EXCLUDED_ITEM_CODES = ["POST", "DOT", "M", "C2", "BANK CHARGES", "PADS", "AMAZONFEE", "S"]
_excluded_sql = ",".join(f"'{c}'" for c in EXCLUDED_ITEM_CODES)

# Windowing defaults for SALES_TREND / SLOW_MOVING_ITEMS, and the minimum
# prior-period volume for SALES_TREND (avoids a tiny item going from 1 unit to
# 3 units showing up as a "+200% trend" — noise, not signal).
DEFAULT_WINDOW_DAYS = 30
MIN_UNITS_FOR_TREND = 5
MIN_HISTORICAL_UNITS_TO_BE_SLOW_MOVING = 5  # must have real history to call it "slow", not "never sold"

# Intents the system recognizes but can't answer yet, and exactly why —
# shown to the user instead of a query attempt.
NOT_YET_AVAILABLE = {
    "CATEGORY_REVENUE_RANKING": "This needs a product category field, which doesn't exist in the current data (only item-level detail is loaded).",
    "SEASONAL_DEMAND": "This needs the forecasting pipeline (model backtesting across the year), which hasn't been built yet.",
    "SALES_FORECAST": "This needs the forecasting pipeline, which hasn't been built yet — no forecast has been generated.",
    "CURRENT_STOCK_LOOKUP": "This needs an inventory table, which doesn't exist yet — only sales transactions are loaded so far.",
    "LOW_STOCK_ITEMS": "This needs an inventory table (stock levels), which hasn't been built yet.",
    "OVERSTOCK_ITEMS": "This needs an inventory table (stock levels), which hasn't been built yet.",
    "EXCESS_STOCK_FLAG": "This needs an inventory table (stock levels), which hasn't been built yet.",
    "PURCHASE_RECOMMENDATION": "This needs both a forecast and an inventory table, neither of which has been built yet.",
    "STOCKOUT_PROJECTION": "This needs both a forecast and an inventory table, neither of which has been built yet.",
}

INTENT_SYSTEM_PROMPT = """You turn a business question about sales, inventory, or purchasing
into a JSON query plan. Respond with ONLY a JSON object, no other text.

You recognize these intents (some can be fully answered today, others are recognized but not
yet supported by the system — that's fine, just classify honestly):

{"intent": "TOP_SELLING_ITEM", "date_from": "YYYY-MM-DD" or null, "date_to": "YYYY-MM-DD" or null}
  -> which item sold the most, optionally in a date range

{"intent": "ITEM_SALES_LOOKUP", "item_query": "<product name/keyword>", "date_from": "YYYY-MM-DD" or null, "date_to": "YYYY-MM-DD" or null}
  -> sales for a specific, named product

{"intent": "SALES_TREND", "direction": "GROWING" or "DECLINING", "period_days": <int, default 30>}
  -> which items are trending up or down recently

{"intent": "SLOW_MOVING_ITEMS", "period_days": <int, default 30>}
  -> which items have the lowest recent sales activity

{"intent": "CATEGORY_REVENUE_RANKING"}
  -> highest-revenue product category

{"intent": "SEASONAL_DEMAND"}
  -> which products show seasonal demand patterns

{"intent": "SALES_FORECAST"}
  -> expected sales in a future period

{"intent": "CURRENT_STOCK_LOOKUP"}
  -> current inventory / stock on hand

{"intent": "LOW_STOCK_ITEMS"}
  -> items low in stock / at risk of stockout

{"intent": "OVERSTOCK_ITEMS"}
  -> items overstocked / excess inventory

{"intent": "EXCESS_STOCK_FLAG"}
  -> items to avoid purchasing due to excess stock

{"intent": "PURCHASE_RECOMMENDATION"}
  -> what to purchase, and how much

{"intent": "STOCKOUT_PROJECTION"}
  -> items that will run out / need urgent replenishment

If the question doesn't match any of these, respond with:
{"intent": "UNKNOWN"}

Do not invent dates, item names, or a direction that weren't mentioned or clearly implied by
the question. If no date range is mentioned where one is optional, use null (all history).
"""

EXPLAIN_SYSTEM_PROMPT = """You explain a database query result to a business user in plain
language. You are given the user's original question and the result row(s). State the answer
plainly, then a one-line "Data used:" note. Never state a number that isn't in the result you
were given. If the result is empty, say plainly that there's no matching data — do not guess.
If several rows came back (e.g. more than one matching item), mention each of them rather than
picking one arbitrarily.
"""


def get_query_plan(question: str) -> dict:
    """Ollama step 1: understand the question, produce a structured plan.
    This is the ONLY place the LLM's output is treated as intent, never as SQL."""
    response = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        options={"temperature": 0.0},
    )
    raw = response["message"]["content"].strip()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        return {"intent": "UNKNOWN", "_raw": raw}
    return plan


def _get_max_sale_date():
    """This dataset is historical (ends 2011-12-09, not 'today'), so 'recent'
    has to mean relative to the data's own latest date, not the real-world
    calendar. Used by SALES_TREND and SLOW_MOVING_ITEMS."""
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        (max_date,) = con.execute("SELECT MAX(sale_date) FROM sales_lines").fetchone()
    finally:
        con.close()
    return max_date


def plan_to_sql(plan: dict):
    """Deterministic compiler: plan -> (sql, params) for the 4 implemented
    intents. The LLM never writes this SQL directly, and all user/LLM-
    influenced values are passed as bound parameters (?) rather than
    embedded into the SQL string."""
    intent = plan.get("intent")

    if intent == "TOP_SELLING_ITEM":
        where = [f"s.item_id NOT IN ({_excluded_sql})"]
        params = []
        if plan.get("date_from"):
            where.append("s.sale_date >= ?")
            params.append(plan["date_from"])
        if plan.get("date_to"):
            where.append("s.sale_date <= ?")
            params.append(plan["date_to"])
        sql = f"""
            SELECT s.item_id, i.description, SUM(s.quantity) AS units_sold
            FROM sales_lines s
            JOIN items i USING (item_id)
            WHERE {" AND ".join(where)}
            GROUP BY s.item_id, i.description
            ORDER BY units_sold DESC
            LIMIT 1
        """
        return sql.strip(), params

    if intent == "ITEM_SALES_LOOKUP":
        item_query = plan.get("item_query")
        if not item_query:
            raise ValueError("ITEM_SALES_LOOKUP plan missing item_query")

        where = [f"s.item_id NOT IN ({_excluded_sql})", "i.description ILIKE ?"]
        params = [f"%{item_query}%"]
        if plan.get("date_from"):
            where.append("s.sale_date >= ?")
            params.append(plan["date_from"])
        if plan.get("date_to"):
            where.append("s.sale_date <= ?")
            params.append(plan["date_to"])

        # Returns up to 10 matching items, not just 1 — if the search term is
        # ambiguous (matches several products), that should be visible in the
        # answer instead of silently picking one.
        sql = f"""
            SELECT s.item_id, i.description,
                   SUM(s.quantity) AS units_sold,
                   SUM(s.revenue) AS revenue,
                   COUNT(DISTINCT CAST(s.sale_date AS DATE)) AS days_with_sales
            FROM sales_lines s
            JOIN items i USING (item_id)
            WHERE {" AND ".join(where)}
            GROUP BY s.item_id, i.description
            ORDER BY units_sold DESC
            LIMIT 10
        """
        return sql.strip(), params

    if intent == "SALES_TREND":
        direction = plan.get("direction", "DECLINING")
        period_days = plan.get("period_days") or DEFAULT_WINDOW_DAYS
        max_date = _get_max_sale_date()
        if max_date is None:
            raise ValueError("No data in sales_lines to compute a trend from")

        recent_start = max_date - datetime.timedelta(days=period_days)
        prior_start = recent_start - datetime.timedelta(days=period_days)

        order = "ASC" if direction == "DECLINING" else "DESC"

        sql = f"""
            WITH recent AS (
                SELECT item_id, SUM(quantity) AS recent_units
                FROM sales_lines
                WHERE item_id NOT IN ({_excluded_sql}) AND sale_date > ?
                GROUP BY item_id
            ),
            prior AS (
                SELECT item_id, SUM(quantity) AS prior_units
                FROM sales_lines
                WHERE item_id NOT IN ({_excluded_sql}) AND sale_date > ? AND sale_date <= ?
                GROUP BY item_id
            )
            SELECT r.item_id, i.description, p.prior_units, r.recent_units,
                   ROUND((r.recent_units - p.prior_units) * 100.0 / p.prior_units, 1) AS pct_change
            FROM recent r
            JOIN prior p USING (item_id)
            JOIN items i USING (item_id)
            WHERE p.prior_units >= {MIN_UNITS_FOR_TREND}
            ORDER BY pct_change {order}
            LIMIT 10
        """
        params = [recent_start, prior_start, recent_start]
        return sql.strip(), params

    if intent == "SLOW_MOVING_ITEMS":
        period_days = plan.get("period_days") or DEFAULT_WINDOW_DAYS
        max_date = _get_max_sale_date()
        if max_date is None:
            raise ValueError("No data in sales_lines to compute this from")
        recent_start = max_date - datetime.timedelta(days=period_days)

        # LEFT JOIN so an item with ZERO recent sales still shows up (it's
        # the slowest-moving kind) — a plain GROUP BY on the recent window
        # alone would silently drop items that didn't sell at all recently.
        sql = f"""
            WITH history AS (
                SELECT item_id, SUM(quantity) AS total_units
                FROM sales_lines
                WHERE item_id NOT IN ({_excluded_sql})
                GROUP BY item_id
                HAVING SUM(quantity) >= {MIN_HISTORICAL_UNITS_TO_BE_SLOW_MOVING}
            ),
            recent AS (
                SELECT item_id, SUM(quantity) AS recent_units
                FROM sales_lines
                WHERE item_id NOT IN ({_excluded_sql}) AND sale_date > ?
                GROUP BY item_id
            )
            SELECT h.item_id, i.description, h.total_units,
                   COALESCE(r.recent_units, 0) AS recent_units
            FROM history h
            LEFT JOIN recent r USING (item_id)
            JOIN items i USING (item_id)
            ORDER BY recent_units ASC, h.total_units DESC
            LIMIT 10
        """
        return sql.strip(), [recent_start]

    raise ValueError(f"No SQL compiler for intent: {intent}")


def validate_sql(sql: str) -> bool:
    """Minimal guard for this MVP: single statement, SELECT/WITH-only, no
    write keywords. NOTE: still a keyword blocklist, not a real parser — flag
    stands from before: replace with sqlglot + an allowlist once this grows
    much further."""
    normalized = sql.strip().rstrip(";")
    if ";" in normalized:
        return False
    if not re.match(r"^\s*(SELECT|WITH)\b", normalized, re.IGNORECASE):
        return False
    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH", "COPY", "PRAGMA"]
    upper = normalized.upper()
    return not any(word in upper for word in forbidden)


def execute(sql: str, params=None):
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        df = con.execute(sql, params or []).fetchdf()
    finally:
        con.close()
    return df


def explain_result(question: str, df) -> str:
    """Ollama step 2: turn the (small, already-computed) result into a plain
    language answer. It never sees raw transaction rows, only this result."""
    result_text = df.to_string(index=False) if len(df) else "(no rows returned)"
    response = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
            {"role": "user", "content": f"Question: {question}\n\nResult:\n{result_text}"},
        ],
        options={"temperature": 0.0},
    )
    return response["message"]["content"].strip()


def answer(question: str) -> str:
    plan = get_query_plan(question)
    intent = plan.get("intent")

    if intent == "UNKNOWN":
        return ("I don't have enough reliable data to answer this. I understand questions "
                "about top sellers, item sales, sales trends, and slow-moving items so far.")

    if intent in NOT_YET_AVAILABLE:
        return (f"I understood that as a {intent.replace('_', ' ').title()} question, but I "
                f"can't answer it reliably yet: {NOT_YET_AVAILABLE[intent]} "
                "Rather than guess, I'm telling you it isn't available.")

    try:
        sql, params = plan_to_sql(plan)
    except ValueError:
        return "I understood the type of question, but couldn't build a safe query for it."

    if not validate_sql(sql):
        # Should never happen since we generated the SQL ourselves — but if
        # the compiler ever produces something unexpected, fail closed.
        return "I couldn't safely answer that question (query validation failed)."

    df = execute(sql, params)
    return explain_result(question, df)


if __name__ == "__main__":
    print("Chat pipeline (Ctrl+C to quit).")
    print("Fully answers: top seller / item sales / sales trend / slow-moving items.")
    print("Recognizes but honestly declines: category, seasonal, forecast, and all")
    print("inventory/purchasing questions (data for those isn't loaded yet).\n")
    while True:
        try:
            q = input("You: ")
        except (KeyboardInterrupt, EOFError):
            break
        print("\nAssistant:", answer(q), "\n")