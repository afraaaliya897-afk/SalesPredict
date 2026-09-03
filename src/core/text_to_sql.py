"""Governed text-to-SQL: LLM writes SELECT against views that already encode sold/orders."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

import duckdb

from src.core.llm_router import call_llm

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
MEMORY_PATH = Path(__file__).resolve().parent.parent.parent / "sql_memory.jsonl"
REJECTION_PATH = Path(__file__).resolve().parent.parent.parent / "sql_rejections.jsonl"
ORPHAN_MONITOR_PATH = Path(__file__).resolve().parent.parent.parent / "orphaned_sold_monitor.jsonl"
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
    -- v_orders: Sales order headers the chat is allowed to see.
    -- Filters: Order type = Sales order, Release = Open, Do not process = No,
    --          not Canceled. Channel may be GC001 or NULL — both are valid.
    CREATE OR REPLACE VIEW v_orders AS
    SELECT
        sales_order_number,
        customer_account,
        customer_name,
        order_type,
        invoice_account,
        channel,
        status,
        release_status,
        do_not_process,
        sales_taker,
        site,
        warehouse,
        CAST(invoice_date AS DATE) AS invoice_date
    FROM sales_order
    WHERE order_type = 'Sales order'
      AND release_status = 'Open'
      AND do_not_process = 'No'
      AND status NOT IN ('Canceled', 'Cancelled')
    """,
    """
    -- v_sold: Inventory lines that are actual sales, joined to the order header.
    -- Join: inventory.number = sales_order.sales_order_number
    --       only when inventory.reference = 'Sales order'
    -- Issue/Sold is applied here so the LLM never sees On-order / reserved rows.
    -- Item number and product number are the same SKU (tiny mismatches exist).
    CREATE OR REPLACE VIEW v_sold AS
    SELECT
        it.item_number,
        it.product_number,
        CAST(it.physical_date AS DATE) AS sale_date,
        it.number AS sales_order_number,
        it.site,
        it.warehouse,
        it.unit,
        it.cost_amount,
        so.customer_account,
        so.customer_name,
        so.invoice_account,
        so.channel,
        so.sales_taker,
        (-it.quantity) AS sold_qty
    FROM inventory_transaction it
    JOIN sales_order so ON it.number = so.sales_order_number
    WHERE it.reference = 'Sales order'
      AND it.issue = 'Sold'
      AND it.quantity < 0
      AND so.order_type = 'Sales order'
      AND so.release_status = 'Open'
      AND so.do_not_process = 'No'
      AND so.status NOT IN ('Canceled', 'Cancelled')
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
            needed_sold = {"sold_qty", "item_number", "product_number", "unit", "cost_amount"}
            needed_ord = {
                "invoice_date",
                "invoice_account",
                "sales_taker",
                "order_type",
                "release_status",
            }
            if not needed_sold.issubset(sold_cols) or not needed_ord.issubset(order_cols):
                return False
            if "missing_order_header" in sold_cols or "financial_date" in sold_cols:
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
                ch = [
                    ("(blank)" if r[0] is None else r[0])
                    for r in con.execute(
                        "SELECT channel, COUNT(*) c FROM v_orders GROUP BY 1 ORDER BY c DESC LIMIT 8"
                    ).fetchall()
                ]
                lines.append(f"  invoice_date range: {dmin} .. {dmax}")
                lines.append(f"  channel values (GC001 and blank/NULL are both valid): {ch}")
                lines.append("  order_type is already 'Sales order' only. Returned orders are excluded.")
                lines.append("  Count orders with COUNT(DISTINCT sales_order_number). No items on this view.")
            else:
                dmin = _scalar(con, "SELECT MIN(sale_date) FROM v_sold")
                dmax = _scalar(con, "SELECT MAX(sale_date) FROM v_sold")
                lines.append(f"  sale_date range: {dmin} .. {dmax}")
                lines.append("  sale_date = Physical date. item_number and product_number are the same SKU.")
                lines.append("  sold_qty is already positive units sold. Never SUM(quantity). Never join raw tables.")
                lines.append("  Join already done: Number (inventory) = Sales order (header), Reference = Sales order only.")
                lines.append("  unit = UOM. cost_amount = inventory cost, not customer price or revenue.")
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
    """Disabled as few-shot source: chronological JSONL taught wrong year filters
    (e.g. 'in 2025' → only from Sept). Curated examples in generate_sql are safer.
    Kept for API compatibility / future embedding retrieval.
    """
    return ""


def _curated_few_shots(as_of_date: str) -> str:
    """Teach date/metric patterns. LIMIT only appears when the question names N."""
    return f"""
Examples (learn the PATTERN — recompute dates and LIMITs from THIS question only):

Q: Top items in 2025
CHART:bar
SELECT item_number, SUM(sold_qty) AS qty
FROM v_sold
WHERE sale_date >= DATE '2025-01-01' AND sale_date < DATE '2026-01-01'
GROUP BY 1 ORDER BY 2 DESC

Q: top 5 items in 2025
CHART:bar
SELECT item_number, SUM(sold_qty) AS qty
FROM v_sold
WHERE sale_date >= DATE '2025-01-01' AND sale_date < DATE '2026-01-01'
GROUP BY 1 ORDER BY 2 DESC LIMIT 5

Q: total sales done in August 2026
CHART:stat
SELECT SUM(sold_qty) AS total
FROM v_sold
WHERE sale_date >= DATE '2026-08-01' AND sale_date < DATE '2026-09-01'

Q: top 5 customers by quantity this year
CHART:bar
SELECT customer_name, SUM(sold_qty) AS qty
FROM v_sold
WHERE sale_date >= date_trunc('year', DATE '{as_of_date}')
  AND sale_date < date_trunc('year', DATE '{as_of_date}') + INTERVAL '1 year'
GROUP BY 1 ORDER BY 2 DESC LIMIT 5

Q: how many orders last month
CHART:stat
SELECT COUNT(DISTINCT sales_order_number) AS orders
FROM v_orders
WHERE invoice_date >= date_trunc('month', DATE '{as_of_date}') - INTERVAL '1 month'
  AND invoice_date < date_trunc('month', DATE '{as_of_date}')

Q: daily sales trend this month
CHART:line
SELECT sale_date, SUM(sold_qty) AS daily_total
FROM v_sold
WHERE sale_date >= date_trunc('month', DATE '{as_of_date}')
GROUP BY 1 ORDER BY 1

Q: total cost in 2025
CHART:stat
SELECT SUM(cost_amount) AS cost
FROM v_sold
WHERE sale_date >= DATE '2025-01-01' AND sale_date < DATE '2026-01-01'

Q: top 5 items by cost this year
CHART:bar
SELECT item_number, SUM(cost_amount) AS cost
FROM v_sold
WHERE sale_date >= date_trunc('year', DATE '{as_of_date}')
  AND sale_date < date_trunc('year', DATE '{as_of_date}') + INTERVAL '1 year'
GROUP BY 1 ORDER BY 2 DESC LIMIT 5

Q: what is our profit margin
UNSUPPORTED
"""


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
    """UI safety only: if the LLM left no LIMIT on a list query, cap at MAX_ROWS.

    Totals (one SUM/COUNT, no GROUP BY) stay uncapped.
    User-named LIMITs (top 5, etc.) are applied earlier by apply_requested_limit —
    this does not invent a business top-N.
    """
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

    as_of_date = "current-date"
    for line in card.split("\n"):
        if "Latest sale_date in v_sold" in line and ":" in line:
            as_of_date = line.split(":")[-1].strip()
            break

    system = f"""You are a DuckDB analyst for a sales & inventory semantic layer.
Write ONE safe SELECT/WITH that answers the user's question in their own words.
Any phrasing is fine — interpret intent, then write SQL. Do not invent columns.

RESPONSE FORMAT (exactly):
Line 1: CHART:pie|bar|line|stat
  pie = share/breakdown  bar = ranking/top-N  line = time series  stat = one number
Then: the SQL only (no markdown fences, no commentary).
OR reply with exactly UNSUPPORTED (only if the question needs profit, margin, selling price, revenue, or stock on hand).
OR reply FORECAST then a JSON window — only if the user asked to forecast/predict future demand.

LIVE SCHEMA (as-of / "today" for relative dates = {as_of_date}):
{card}

SEMANTIC LAYER (business meaning — already encoded in the views):
v_sold = completed unit sales (inventory Reference = Sales order, Issue = Sold, joined to order header).
  Metrics: SUM(sold_qty) for units/volume/"sales"/"buying"; SUM(cost_amount) for cost/spend analysis (not revenue—we lack selling price).
  Dimensions: item_number (= product_number), sale_date, customer_*, channel, site, warehouse, sales_taker, unit.
v_orders = sales order headers (Order type = Sales order, Release = Open, Do not process = No, not Canceled).
  Metrics: COUNT(DISTINCT sales_order_number) for order counts.
  Dimensions: invoice_date, customer_*, channel, site, warehouse, sales_taker.
  No item/SKU columns — item questions always use v_sold.

INTENT → SQL (paraphrase any wording into these):
- items / products / SKUs / top selling / least selling → v_sold, GROUP BY item_number, SUM(sold_qty)
- customers by volume / buying → v_sold, GROUP BY customer_name, SUM(sold_qty)
- customers by orders / how many orders → v_orders, COUNT(DISTINCT sales_order_number)
- "sales" / "total sales" / "sales done" / "units sold" → SUM(sold_qty) on v_sold (ALWAYS units, never money)
- "cost" / "total cost" / "cost amount" / "spend" → SUM(cost_amount) on v_sold (what we paid, not selling price)
- warehouse / site / channel breakdowns → GROUP BY that dimension
- Always ORDER BY the metric (DESC for top/most, ASC for least).
- LIMIT: ONLY when the user names a number ("top 5", "10 customers"). If they did not name N, do NOT invent LIMIT.

DATE RULES (compute from the question — never copy a random month from examples):
- Bare year "2025" / "in 2025" / "for 2025" = FULL calendar year:
  sale_date >= DATE '2025-01-01' AND sale_date < DATE '2026-01-01'
  (same idea for any YYYY: start Jan 1 of that year, end Jan 1 of next year)
- Named month+year "August 2026" = that calendar month only (first day inclusive, next month exclusive)
- "this month" / "last month" / "this year" are relative to as-of date {as_of_date}, not wall-clock today
- If a period is empty in the data, still write the SQL (SUM may be NULL/0). That is correct — never UNSUPPORTED for empty periods.
- Never use FORECAST for historical years/months the user asked about as completed sales.

COLUMN WHITELIST (from 19 inventory + 14 sales Excel cols, only these made it to views):
✓ v_sold (from inventory "Sales order" reference + sales order header):
  item_number, product_number (same SKU), sale_date (=physical_date), sales_order_number (=number),
  sold_qty (=-quantity, already positive), unit, cost_amount, site, warehouse,
  customer_account, customer_name, invoice_account, channel, sales_taker
✓ v_orders (from sales order header where order_type='Sales order', release='Open', do_not_process='No'):
  sales_order_number, customer_account, customer_name, order_type, invoice_account,
  channel (GC001 or NULL), status, release_status, do_not_process, sales_taker, site, warehouse, invoice_date
✗ NOT in views (Excel cols that were filtered out or not included):
  financial_date, receipt, issue, reference, location, size, color, style, quantity, number, voucher, any other unmapped fields

HARD LIMITS:
- Only query v_sold or v_orders. Never sales_order / inventory_transaction.
- Never invent columns. If you need a column from the ✗ list above, return UNSUPPORTED.
{_curated_few_shots(as_of_date)}
Now answer THIS question with CHART + SQL (or UNSUPPORTED / FORECAST only when rules above say so)."""

    try:
        # DeepSeek needs room for a short think + full SQL; 300 truncated answers to empty/UNSUPPORTED.
        token_limit = 900 if "deepseek" in model.lower() else 500

        response = call_llm(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=token_limit,
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

    chart_hint = None
    if raw:
        first_line = raw.strip().split("\n")[0] if "\n" in raw else raw.strip()
        if first_line.upper().startswith("CHART:"):
            chart_hint = first_line.split(":", 1)[1].strip().lower()
            if chart_hint not in ["pie", "bar", "line", "stat"]:
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
        "v_sold is units sold (sale_date, sold_qty already positive). "
        "Channel may be GC001 or NULL. item_number = product_number. "
        "Columns: sales_taker, invoice_account, unit, cost_amount exist."
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
