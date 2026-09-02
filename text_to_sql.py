"""Governed text-to-SQL: LLM writes SELECT against views that already encode sold/orders."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

import duckdb

from llm_router import call_llm

ALLOWED_TABLES = frozenset({"v_orders", "v_sold"})
FORBIDDEN_FUNCS = frozenset({
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "read_json",
    "read_json_auto",
    "read_xlsx",
    "read_text",
    "glob",
    "query_table",
    "sqlite_scan",
    "postgres_scan",
    "iceberg_scan",
})
MAX_ROWS = 100
MAX_SQL_RETRIES = 2
MEMORY_PATH = Path(__file__).resolve().parent / "sql_memory.jsonl"
REJECTION_PATH = Path(__file__).resolve().parent / "sql_rejections.jsonl"
ORPHAN_MONITOR_PATH = Path(__file__).resolve().parent / "orphaned_sold_monitor.jsonl"
FORECAST_RE = re.compile(r"\b(forecast|predict|prediction|projection)\b", re.I)
LIKELY_RE = re.compile(
    r"\b(most likely|likely to (be )?sold|expected (to )?sell|will sell|going to sell)\b",
    re.I,
)
FUTURE_PERIOD_RE = re.compile(
    r"\b(next\s+(\d+\s+)?(day|week|month|quarter|year)s?|upcoming|coming month)\b",
    re.I,
)
ITEM_RANK_FORECAST_RE = re.compile(
    r"(\b(which|what|top)\b.{0,80}\bitems?\b)|"
    r"(\bitems?\b.{0,50}\b(most likely|likely|will sell|next month|next year)\b)|"
    r"(\bmost likely\b.{0,50}\bitems?\b)",
    re.I,
)


def is_forecast_question(question: str) -> bool:
    """Route forecast-intent questions away from SQL.

    This is a hand-written pattern match, not an LLM decision — a question
    phrased outside these patterns will be misrouted. If you add a new
    phrasing style your users actually use, add it here.
    """
    text = question or ""
    if FORECAST_RE.search(text) or LIKELY_RE.search(text):
        return True
    if ITEM_RANK_FORECAST_RE.search(text) and FUTURE_PERIOD_RE.search(text):
        return True
    if FUTURE_PERIOD_RE.search(text) and re.search(
        r"\b(item|items|sales|sold|demand|units)\b", text, re.I
    ):
        return True
    return False


def is_item_rank_forecast(question: str) -> bool:
    return bool(ITEM_RANK_FORECAST_RE.search(question or ""))


# Hand-written pattern match, not an LLM decision — a question phrased outside
# these patterns will be misrouted. If you add a new phrasing style your users
# actually use, add it here.
UNSUPPORTED_RE = re.compile(
    r"\b(profit|margin|gross margin|stock on hand|on-hand|cogs|cost of goods)\b",
    re.I,
)

_TOP_N_RE = re.compile(r"\b(?:top|first|last|bottom)\s+(\d{1,4})\b", re.I)
_N_NOUN_RE = re.compile(
    r"\b(\d{1,4})\s+(?:items?|products?|skus?|customers?|warehouses?|channels?|sites?|orders?)\b",
    re.I,
)


def requested_row_limit(question: str) -> int | None:
    """Explicit row count named in the question ('top 5', '5 customers').

    Small local models (llama3.2:3b) sometimes ignore the number entirely, or
    copy a different LIMIT straight from a few-shot memory example. This gives
    Python a deterministic override so 'top 5' always returns exactly 5 rows,
    regardless of what the LLM put in the SQL.
    """
    match = _TOP_N_RE.search(question or "") or _N_NOUN_RE.search(question or "")
    if not match:
        return None
    n = int(match.group(1))
    if n <= 0:
        return None
    return min(n, MAX_ROWS)


def apply_requested_limit(sql: str, question: str) -> str:
    """Force the SQL's LIMIT to match a row count named in the question, if any."""
    n = requested_row_limit(question)
    if n is None or not sql:
        return sql
    if re.search(r"\bLIMIT\s+\d+\b", sql, re.IGNORECASE):
        return re.sub(r"\bLIMIT\s+\d+\b", f"LIMIT {n}", sql, count=1, flags=re.IGNORECASE)
    return f"{sql.rstrip()}\nLIMIT {n}"


VIEW_DDL = [
    """
    -- v_orders: Clean sales order headers
    -- Filters: Non-canceled, processable orders only
    -- Source: sales_order (SalesTable from D365)
    CREATE OR REPLACE VIEW v_orders AS
    SELECT
        sales_order_number,
        customer_account,
        customer_name,
        order_type,
        channel,
        status,
        site,
        warehouse,
        CAST(invoice_date AS DATE) AS invoice_date
    FROM sales_order
    WHERE status NOT IN ('Canceled', 'Cancelled')
      AND do_not_process != 'Yes'
    """,
    """
    -- v_sold: Completed unit sales with customer context
    -- Source: inventory_transaction (InventTrans from D365)
    --         INNER JOIN sales_order (SalesTable from D365)
    --
    -- D365 Relationship: InventTrans.TransRefId -> SalesTable.SalesId
    -- Current join: inventory_transaction.number = sales_order.sales_order_number
    --
    -- Orphaned sold rows (no matching sales_order header) are excluded so they
    -- cannot bypass Cancelled / Do-Not-Process checks. Monitor via
    -- count_orphaned_sold_rows() / orphaned_sold_monitor.jsonl.
    --
    -- Filters:
    --   - reference = 'Sales order'
    --   - issue = 'Sold'
    --   - quantity < 0 (outbound/issue in D365 convention)
    --   - Non-canceled, processable order headers only
    CREATE OR REPLACE VIEW v_sold AS
    SELECT
        it.item_number,
        it.product_number,
        CAST(it.physical_date AS DATE) AS sale_date,
        CAST(it.financial_date AS DATE) AS financial_date,
        it.number AS sales_order_number,
        it.site,
        it.warehouse,
        so.customer_account,
        so.customer_name,
        so.channel,
        (-it.quantity) AS sold_qty
    FROM inventory_transaction it
    JOIN sales_order so ON it.number = so.sales_order_number
    WHERE it.reference = 'Sales order'
      AND it.issue = 'Sold'
      AND it.quantity < 0
      AND so.status NOT IN ('Canceled', 'Cancelled')
      AND so.do_not_process != 'Yes'
    """,
]


_VIEW_LOCK = threading.Lock()


def _views_look_current(db_path: str) -> bool:
    """True when v_sold/v_orders exist and match the INNER-JOIN sold definition."""
    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            sold_cols = {r[0] for r in con.execute("DESCRIBE v_sold").fetchall()}
            order_cols = {r[0] for r in con.execute("DESCRIBE v_orders").fetchall()}
            if "sold_qty" not in sold_cols or "invoice_date" not in order_cols:
                return False
            # Old LEFT-JOIN definition exposed this flag; new view must not.
            if "missing_order_header" in sold_cols:
                return False
            return True
        finally:
            con.close()
    except Exception:
        return False


def ensure_views(db_path: str) -> None:
    """Create/replace governed views. Skips rewrite when already current to avoid
    DuckDB read/write connection conflicts with parallel readers.
    """
    with _VIEW_LOCK:
        if not _views_look_current(db_path):
            con = duckdb.connect(db_path)
            try:
                for ddl in VIEW_DDL:
                    con.execute(ddl)
            finally:
                con.close()
    # Periodic data-quality signal (throttled — not every chat question).
    try:
        log_orphaned_sold_monitor(db_path, min_interval_hours=6)
    except Exception:
        pass


def count_orphaned_sold_rows(db_path: str) -> int:
    """Sold inventory rows with no matching sales_order header (excluded from v_sold)."""
    con = duckdb.connect(db_path, read_only=True)
    try:
        (n,) = con.execute(
            """
            SELECT COUNT(*)
            FROM inventory_transaction it
            LEFT JOIN sales_order so ON it.number = so.sales_order_number
            WHERE it.reference = 'Sales order'
              AND it.issue = 'Sold'
              AND so.sales_order_number IS NULL
            """
        ).fetchone()
        return int(n or 0)
    finally:
        con.close()


def log_orphaned_sold_monitor(db_path: str, min_interval_hours: float = 6) -> None:
    """Append orphan count so growth is visible without putting orphans back in v_sold."""
    try:
        if ORPHAN_MONITOR_PATH.exists() and min_interval_hours > 0:
            lines = ORPHAN_MONITOR_PATH.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    prev = json.loads(line)
                    ts = prev.get("logged_at") or prev.get("timestamp")
                    if ts:
                        prev_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
                        age_h = (datetime.utcnow() - prev_dt).total_seconds() / 3600.0
                        if age_h < min_interval_hours:
                            return
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
                break
        n = count_orphaned_sold_rows(db_path)
        with ORPHAN_MONITOR_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "logged_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "orphaned_sold_rows": n,
                "db_path": str(db_path),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _scalar(con, sql: str):
    row = con.execute(sql).fetchone()
    return row[0] if row else None


def build_schema_card(db_path: str) -> str:
    """Live catalog so the model sees real columns, dates, and example values."""
    ensure_views(db_path)
    con = duckdb.connect(db_path, read_only=True)
    try:
        lines = []
        as_of = _scalar(con, "SELECT MAX(sale_date) FROM v_sold")
        as_of_s = as_of.isoformat()[:10] if as_of is not None else "unknown"
        lines.append(f"Latest sale_date in v_sold (use this as 'today' / this month): {as_of_s}")
        lines.append("Do not use the wall-clock date. Relative periods are vs that as-of date.")
        lines.append("")
        for view in ("v_orders", "v_sold"):
            n = _scalar(con, f"SELECT COUNT(*) FROM {view}")
            lines.append(f"VIEW {view} ({n} rows)")
            for row in con.execute(f"DESCRIBE {view}").fetchall():
                lines.append(f"  - {row[0]} {row[1]}")
            if view == "v_orders":
                dmin = _scalar(con, "SELECT MIN(invoice_date) FROM v_orders")
                dmax = _scalar(con, "SELECT MAX(invoice_date) FROM v_orders")
                ch = [r[0] for r in con.execute(
                    "SELECT channel, COUNT(*) c FROM v_orders GROUP BY 1 ORDER BY c DESC LIMIT 8"
                ).fetchall()]
                lines.append(f"  invoice_date range: {dmin} .. {dmax}")
                lines.append(f"  channel examples: {ch}")
            else:
                dmin = _scalar(con, "SELECT MIN(sale_date) FROM v_sold")
                dmax = _scalar(con, "SELECT MAX(sale_date) FROM v_sold")
                lines.append(f"  sale_date range: {dmin} .. {dmax}")
                lines.append("  sold_qty is already positive units sold. Never SUM(quantity). Never join raw tables.")
                # Orphans are excluded from v_sold; count comes from raw tables.
                try:
                    orphan_n = count_orphaned_sold_rows(db_path)
                    lines.append(
                        f"  orphaned sold rows excluded from this view (no order header): {orphan_n}"
                    )
                except Exception:
                    pass
            lines.append("")
        return "\n".join(lines)
    finally:
        con.close()


def _memory_shots(limit: int = 8) -> str:
    if not MEMORY_PATH.exists():
        return ""
    rows = []
    for line in MEMORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows = rows[-limit:]
    if not rows:
        return ""
    bits = ["Previous accepted queries (copy the pattern when the question is similar):"]
    for row in rows:
        bits.append(f"Q: {row.get('question')}\n{row.get('sql')}")
    return "\n\n".join(bits)


def remember_sql(question: str, sql: str) -> None:
    try:
        with MEMORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "logged_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "question": question,
                "sql": sql,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log_sql_rejection(
    question: str,
    attempted_sql: str | None,
    reason: str,
) -> None:
    """Append-only log of rejected / failed SQL for human review (sql_rejections.jsonl)."""
    try:
        with REJECTION_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "question": question,
                "attempted_sql": attempted_sql,
                "reason": reason,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def extract_forecast_window(raw: str) -> dict | None:
    """Parse FORECAST + JSON window from the LLM reply."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.upper().startswith("CHART:"):
        parts = text.split("\n", 1)
        text = parts[1].strip() if len(parts) > 1 else ""
    if not re.match(r"^FORECAST\b", text, re.IGNORECASE):
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    grain = str(data.get("grain") or "").lower().strip()
    if grain not in ("day", "week", "month"):
        grain = None
    item = data.get("item")
    if item is not None:
        item = str(item).strip()
        if not item or item.lower() in ("null", "none", "all", "*"):
            item = None
    return {
        "start": data.get("start"),
        "end": data.get("end"),
        "grain": grain,
        "item": item,
    }


def extract_sql(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.upper().startswith("CHART:"):
        parts = text.split("\n", 1)
        text = parts[1].strip() if len(parts) > 1 else ""
        if not text:
            return None
    upper = text.upper().split()[0] if text.split() else ""
    if upper in ("FORECAST", "UNSUPPORTED"):
        return upper
    fence = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    if ";" in text:
        text = text.split(";")[0].strip()
    if not re.match(r"^\s*(SELECT|WITH)\b", text, re.IGNORECASE):
        start = re.search(r"\b(SELECT|WITH)\b", text, re.IGNORECASE)
        if not start:
            return None
        text = text[start.start():]
    return text.strip().rstrip(";")


def check_sql(sql: str) -> tuple[bool, str]:
    """Reject anything that is not a single SELECT/WITH over v_orders / v_sold.

    Does not scan the full SQL text for DDL/DML keywords — that false-rejects
    real names like 'Call Center Supplies' or 'Alter Ego'. Safety comes from
    requiring a Select/With AST root plus allowlisted table references only.
    """
    if not sql or not sql.strip():
        return False, "empty SQL"
    normalized = sql.strip().rstrip(";")
    if ";" in normalized:
        return False, "multiple statements"
    # Opening keyword only — not a full-body keyword scan.
    if not re.match(r"^\s*(SELECT|WITH)\b", normalized, re.IGNORECASE):
        return False, "not a SELECT"
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return False, "sqlglot is not installed"
    try:
        parsed = sqlglot.parse_one(normalized, read="duckdb")
    except Exception as exc:
        return False, f"parse error: {exc}"
    if parsed is None:
        return False, "parse error"

    def _is_selectish(node) -> bool:
        return isinstance(node, (exp.Select, exp.Union, exp.Intersect, exp.Except))

    if isinstance(parsed, exp.Select):
        pass
    elif isinstance(parsed, exp.With):
        if not _is_selectish(parsed.this):
            return False, "not a SELECT statement"
    elif _is_selectish(parsed):
        pass
    else:
        return False, f"not a SELECT statement ({type(parsed).__name__})"

    funcs = {f.sql(dialect="duckdb").split("(")[0].lower() for f in parsed.find_all(exp.Anonymous)}
    funcs |= {f.name.lower() for f in parsed.find_all(exp.Func) if getattr(f, "name", None)}
    bad_fn = funcs & FORBIDDEN_FUNCS
    if bad_fn:
        return False, f"forbidden function {sorted(bad_fn)}"
    cte_names = {c.alias_or_name.lower() for c in parsed.find_all(exp.CTE)}
    tables = set()
    for table in parsed.find_all(exp.Table):
        name = (table.name or "").lower()
        if name and name not in cte_names:
            tables.add(name)
    if not tables:
        return False, "no table"
    extra = tables - ALLOWED_TABLES
    if extra:
        return False, f"table not allowed: {sorted(extra)}"
    return True, "ok"


def _is_scalar_aggregate(sql: str) -> bool:
    """True when SQL is one total/count (no GROUP BY)."""
    if re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE):
        return False
    return bool(
        re.search(
            r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(",
            sql,
            re.IGNORECASE,
        )
    )


def _strip_trailing_limit(sql: str) -> str:
    return re.sub(
        r"\s+LIMIT\s+\d+\s*;?\s*$",
        "",
        (sql or "").strip().rstrip(";"),
        flags=re.IGNORECASE,
    )


def enforce_limit(sql: str) -> str:
    """Cap list/item queries at MAX_ROWS. Totals (one SUM/COUNT, no GROUP BY) stay uncapped."""
    if _is_scalar_aggregate(sql):
        return _strip_trailing_limit(sql)
    if re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        return sql
    return f"SELECT * FROM (\n{sql}\n) AS _safe LIMIT {MAX_ROWS}"


def sql_intent_mismatch(question: str, sql: str) -> str | None:
    """Kept so an old in-memory query_engine import does not crash. Always no-op."""
    return None


def generate_sql(question: str, model: str, db_path: str) -> dict:
    card = build_schema_card(db_path)
    memory = _memory_shots()
    
    # Extract as-of date from card for examples
    as_of_date = "current-date"
    for line in card.split('\n'):
        if 'Latest sale_date in v_sold' in line and ':' in line:
            as_of_date = line.split(':')[-1].strip()
            break
    
    system = f"""You are a DuckDB SQL writer for a sales database.

RESPONSE FORMAT:
First line: CHART:<type> where <type> is one of: pie, bar, line, stat
- pie: for distributions, breakdowns, percentages, proportions (2-10 categories)
- bar: for rankings, comparisons, top N lists (2-20 items)
- line: for time series, trends, daily/monthly patterns
- stat: for single aggregate values (total, count, average)

Second line onwards: ONE SQL statement. No markdown, no comments.
Read the user's question and write SQL that answers that question. Do not reuse a canned query.

{card}

Business rules already applied inside the views — do not re-filter Canceled/Sold:
- v_orders = non-canceled, processable sales orders. Date column: invoice_date.
  Count orders with COUNT(DISTINCT sales_order_number). This view has NO items.
- v_sold = completed unit sales. Date column: sale_date (physical date).
  Quantity column: sold_qty (already positive). SUM(sold_qty) for units sold.
  Use this view for items, daily sold trend, customers by units.

Never query sales_order or inventory_transaction.
Never use sold_qty from v_orders. Never group v_orders by item_number.

If the user asks to forecast or predict future sales, which item will sell, what is most likely to sell, or demand in a future / next period, reply with exactly this shape (no SQL). Do not query v_sold for future dates — that is history, not a forecast.
FORECAST
{{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","grain":"day|week|month","item":null}}
Rules for the window:
- Treat the as-of date in the schema card as "today" (latest sale in the data), not the real calendar today
- Use the dates the user named. "October to November 2026" → start 2026-10-01, end 2026-11-30
- A single month → first day through last day of that month
- "next N days/weeks/months" starts the day after as-of
- grain: day if the span is 90 days or less, month if longer, unless the user asked for daily or weekly
- item: the item_number the user named, or null for all sold units. Never invent an item code.
If the question needs profit, margin, cost, or stock on hand, reply with exactly UNSUPPORTED

Common question patterns (use these as templates):
- "total sales this month" → SELECT SUM(sold_qty) AS total FROM v_sold WHERE sale_date >= date_trunc('month', DATE '{as_of_date}')
- "total sales this year" / "sales done this year" → SELECT SUM(sold_qty) AS total FROM v_sold WHERE sale_date >= date_trunc('year', DATE '{as_of_date}') AND sale_date < date_trunc('year', DATE '{as_of_date}') + INTERVAL '1 year'
  That SUM covers EVERY sold row in the year. Never SELECT * and never LIMIT before aggregating.
- "top N items" → SELECT item_number, SUM(sold_qty) AS total FROM v_sold WHERE <time filter> GROUP BY 1 ORDER BY 2 DESC LIMIT N
- "list all products/items" → ALWAYS interpret as "top 15 items" and use LIMIT 15
- "all customers" → ALWAYS interpret as "top 15 customers" and use LIMIT 15
- "top N customers by orders" → SELECT customer_name, COUNT(DISTINCT sales_order_number) AS orders FROM v_orders WHERE <time filter> GROUP BY 1 ORDER BY 2 DESC LIMIT N
- "top N customers by quantity" → SELECT customer_name, SUM(sold_qty) AS total FROM v_sold WHERE <time filter> GROUP BY 1 ORDER BY 2 DESC LIMIT N
- "daily trend" → SELECT sale_date, SUM(sold_qty) AS daily_total FROM v_sold WHERE <time filter> GROUP BY 1 ORDER BY 1
- "orders by channel" → SELECT channel, COUNT(DISTINCT sales_order_number) AS orders FROM v_orders WHERE <time filter> GROUP BY 1 ORDER BY 2 DESC
- "compare months" → Use CASE WHEN with date_trunc('month', ...) to split periods, then SUM per group
- "top 5 customers in 2026" (bare year, no month named) → SELECT customer_name, SUM(sold_qty) AS total FROM v_sold WHERE sale_date >= DATE '2026-01-01' AND sale_date < DATE '2027-01-01' GROUP BY 1 ORDER BY 2 DESC LIMIT 5

CHART-FRIENDLY LIMITS:
- When asked for "all" or "list" without a specific number, ALWAYS add LIMIT 15
- Never return more than 20 rows for item/customer lists (charts become unreadable)
- If user wants more, they can view the full table, but chart should show top 15

CRITICAL ACCURACY RULES:
1. ALWAYS use the as-of date shown above as "today" - NEVER use current date
2. For "this month" use: WHERE sale_date >= date_trunc('month', DATE 'YYYY-MM-DD')
3. For "last month" use: WHERE sale_date >= date_trunc('month', DATE 'YYYY-MM-DD') - INTERVAL '1 month' AND sale_date < date_trunc('month', DATE 'YYYY-MM-DD')
4. For "last N days" use: WHERE sale_date >= DATE 'YYYY-MM-DD' - INTERVAL 'N days'
5. Use sold_qty ONLY from v_sold (never from v_orders)
6. Count orders with COUNT(DISTINCT sales_order_number)
7. NEVER make up columns that don't exist in the schema
8. ALWAYS ORDER BY the metric you're measuring (not by name)
9. If asking for "units" or "quantity", use SUM(sold_qty) from v_sold
10. If asking for "orders", use COUNT(DISTINCT sales_order_number) from v_orders
11. ALWAYS add LIMIT 15 for "all products", "all items", "all customers", "list products" queries
12. Maximum LIMIT for charts should be 20 - larger numbers are unreadable
13. A bare year with no month named ("in 2026", "for 2025", "this year") means the FULL calendar year:
    WHERE sale_date >= DATE 'YYYY-01-01' AND sale_date < DATE 'YYYY+1-01-01'
    Do NOT default to "this month" unless the question actually says "this month" or names no period at all.
14. Totals ("total sales", "how many units", "sales done this year"): one SUM/COUNT over the full filter.
    Never list rows. Never LIMIT the underlying sales before SUM. LIMIT is only for top-N / list questions.
15. The memory examples below show SQL SYNTAX PATTERNS ONLY. Never copy their LIMIT number or
    their date literals into your answer — always recompute both from THIS question's own wording,
    even when a memory example looks similar.

{memory}"""

    try:
        response = call_llm(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=400,
        )
        raw = response["content"]
        elapsed_ms = response["llm_ms"]
    except Exception as e:
        print(f"SQL generation failed: {e}")
        return {
            "raw": f"ERROR: {e}",
            "sql": None,
            "schema_card": card,
            "llm_ms": 0,
        }
    
    extracted = extract_sql(raw)
    
    # Extract chart type hint from response
    chart_hint = None
    if raw:
        first_line = raw.strip().split('\n')[0] if '\n' in raw else raw.strip()
        if first_line.upper().startswith('CHART:'):
            chart_hint = first_line.split(':', 1)[1].strip().lower()
            if chart_hint not in ['pie', 'bar', 'line', 'stat']:
                chart_hint = None
    
    return {
        "raw": raw,
        "sql": extracted,
        "chart_hint": chart_hint,
        "forecast_window": extract_forecast_window(raw),
        "schema_card": card,
        "llm_ms": elapsed_ms,
    }


def rewrite_sql_after_error(question: str, bad_sql: str, error: str, model: str) -> dict:
    """One short rewrite turn. Does not grow the original schema prompt."""
    system = (
        "You fix DuckDB SQL. Reply with ONE SELECT or WITH statement only. "
        "No markdown, no comments.\n"
        "Use only v_orders and v_sold. Never query sales_order or inventory_transaction.\n"
        "v_orders counts orders (invoice_date, COUNT DISTINCT sales_order_number). "
        "v_sold is units sold (sale_date, sold_qty already positive)."
    )
    user = (
        f"Question: {question}\n\n"
        f"This SQL failed:\n{bad_sql}\n\n"
        f"DuckDB error:\n{error}\n\n"
        "Write a corrected query for the same question."
    )
    
    try:
        response = call_llm(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=400,
        )
        raw = response["content"]
        elapsed_ms = response["llm_ms"]
    except Exception as e:
        print(f"SQL rewrite failed: {e}")
        return {"raw": f"ERROR: {e}", "sql": None, "llm_ms": 0}
    
    return {"raw": raw, "sql": extract_sql(raw), "llm_ms": elapsed_ms}
