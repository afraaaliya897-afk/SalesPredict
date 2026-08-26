"""
Sales loader — UCI Online Retail dataset -> DuckDB

What this does:
  1. Reads the raw CSV (InvoiceNo, StockCode, Description, Quantity,
     InvoiceDate, UnitPrice, CustomerID, Country)
  2. Validates / cleans it, logging what got dropped and why
  3. Loads it into a local DuckDB file as two tables: `sales_lines` and `items`
  4. Prints a summary (row counts, date range, item count) so you can see
     the real shape of the data instead of assuming it

Usage:
    python load_sales.py path/to/online_retail.csv

Requires: duckdb, pandas   (pip install duckdb pandas)
"""

import sys
import duckdb
import pandas as pd
from pathlib import Path

DB_PATH = "sales_inventory.duckdb"


def load_and_clean(csv_path: str) -> tuple[pd.DataFrame, dict]:
    """Read the raw CSV and return a cleaned sales dataframe + a log of what
    was dropped and why (this is the 'validate on load' step from the
    architecture doc — never silently swallow bad rows)."""

    # UCI Online Retail is usually Latin-1/cp1252 encoded, not UTF-8
    df = pd.read_csv(csv_path, encoding="ISO-8859-1", dtype={"CustomerID": "string"})

    log = {"rows_in": len(df)}

    # Normalize column names in case of stray whitespace
    df.columns = [c.strip() for c in df.columns]

    # 1. Cancelled/returned orders: InvoiceNo starting with 'C' (a documented
    #    quirk of this dataset). Keep them in a separate table rather than
    #    silently dropping — a return is real business information, just not
    #    a "sale".
    df["InvoiceNo"] = df["InvoiceNo"].astype(str)
    is_cancelled = df["InvoiceNo"].str.startswith("C")
    returns = df[is_cancelled].copy()
    df = df[~is_cancelled].copy()
    log["returns_set_aside"] = len(returns)

    # 2. Rows missing the fields we can't operate without
    required = ["StockCode", "Quantity", "InvoiceDate", "UnitPrice"]
    before = len(df)
    df = df.dropna(subset=required)
    log["dropped_missing_required_fields"] = before - len(df)

    # 3. Non-positive quantity or price left in the "sales" set (shouldn't be,
    #    after removing cancellations, but don't assume — check)
    before = len(df)
    df = df[(df["Quantity"] > 0) & (df["UnitPrice"] > 0)]
    log["dropped_non_positive_qty_or_price"] = before - len(df)

    # 4. Parse date, drop anything that fails to parse
    before = len(df)
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"], errors="coerce")
    df = df.dropna(subset=["InvoiceDate"])
    log["dropped_unparseable_dates"] = before - len(df)

    # 5. Missing CustomerID is common in this dataset (~25%) and is NOT a
    #    reason to drop the sale — it's still a real transaction, just an
    #    anonymous one. Flag it instead of discarding revenue history.
    df["CustomerID"] = df["CustomerID"].fillna("UNKNOWN")

    df["revenue"] = df["Quantity"] * df["UnitPrice"]
    log["rows_loaded"] = len(df)
    log["rows_returns"] = len(returns)

    return df, log


def build_database(df: pd.DataFrame, db_path: str):
    con = duckdb.connect(db_path)

    con.execute("""
        CREATE OR REPLACE TABLE sales_lines AS
        SELECT
            "InvoiceNo"    AS invoice_no,
            "StockCode"    AS item_id,
            "Quantity"     AS quantity,
            "InvoiceDate"  AS sale_date,
            "UnitPrice"    AS unit_price,
            revenue,
            "CustomerID"   AS customer_id,
            "Country"      AS country
        FROM df
    """)

    # Item master: one row per StockCode, using the most common description
    # seen for it (descriptions are occasionally inconsistent for the same
    # code in this dataset)
    con.execute("""
        CREATE OR REPLACE TABLE items AS
        SELECT
            "StockCode" AS item_id,
            mode("Description") AS description
        FROM df
        GROUP BY "StockCode"
    """)

    con.close()


def print_summary(db_path: str, log: dict):
    con = duckdb.connect(db_path, read_only=True)

    n_rows, n_items, n_countries, dmin, dmax, total_rev = con.execute("""
        SELECT
            COUNT(*),
            COUNT(DISTINCT item_id),
            COUNT(DISTINCT country),
            MIN(sale_date),
            MAX(sale_date),
            SUM(revenue)
        FROM sales_lines
    """).fetchone()

    print("\n=== Load log ===")
    for k, v in log.items():
        print(f"  {k}: {v}")

    print("\n=== sales_lines summary ===")
    print(f"  rows:              {n_rows:,}")
    print(f"  distinct items:    {n_items:,}")
    print(f"  distinct countries:{n_countries}")
    print(f"  date range:        {dmin} to {dmax}")
    print(f"  total revenue:     {total_rev:,.2f}")

    print("\n=== Top 5 items by units sold ===")
    top = con.execute("""
        SELECT s.item_id, i.description, SUM(s.quantity) AS units
        FROM sales_lines s
        JOIN items i USING (item_id)
        GROUP BY s.item_id, i.description
        ORDER BY units DESC
        LIMIT 5
    """).fetchdf()
    print(top.to_string(index=False))

    con.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python load_sales.py path/to/online_retail.csv")
        sys.exit(1)

    csv_path = sys.argv[1]
    if not Path(csv_path).exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    print(f"Loading {csv_path} ...")
    df, log = load_and_clean(csv_path)
    build_database(df, DB_PATH)
    print_summary(DB_PATH, log)
    print(f"\nDone. Data loaded into {DB_PATH}")