"""
Sparsity check — run AFTER load_sales.py has built sales_inventory.duckdb.

Answers the question the forecasting design depends on: how much real
history does a typical item actually have? Decides item-level vs.
category-level forecasting, and whether Croston's/TSB (intermittent demand)
is actually needed, using real numbers instead of assumption.

Usage:
    python analyze_sparsity.py
"""

import duckdb

DB_PATH = "sales_inventory.duckdb"

# Stock codes known to appear in this dataset that are NOT physical products
# (postage, discounts, manual adjustments, bank charges, etc.) — check for
# these before treating "top selling items" or forecasts as trustworthy.
NON_PRODUCT_CODES = ["POST", "D", "M", "C2", "DOT", "BANK CHARGES", "CRUK", "PADS", "S", "AMAZONFEE"]


def main():
    con = duckdb.connect(DB_PATH, read_only=True)

    # 1. How many rows belong to known non-product codes?
    placeholders = ",".join(f"'{c}'" for c in NON_PRODUCT_CODES)
    flagged = con.execute(f"""
        SELECT item_id, COUNT(*) AS rows, SUM(quantity) AS units
        FROM sales_lines
        WHERE item_id IN ({placeholders})
        GROUP BY item_id
        ORDER BY rows DESC
    """).fetchdf()

    print("=== Known non-product codes found in your data ===")
    if len(flagged) == 0:
        print("  None of the checked codes appear — good, but this list isn't exhaustive;")
        print("  worth a manual skim of items.description for anything that isn't a real item.")
    else:
        print(flagged.to_string(index=False))
        print(f"\n  -> Exclude these {len(flagged)} code(s) from top-seller / forecasting logic.")

    # 2. Per-item history depth: rows, distinct active days, first/last sale
    per_item = con.execute("""
        SELECT
            item_id,
            COUNT(*)                    AS n_rows,
            COUNT(DISTINCT CAST(sale_date AS DATE)) AS n_active_days,
            MIN(sale_date)               AS first_sale,
            MAX(sale_date)               AS last_sale,
            SUM(quantity)                AS total_units
        FROM sales_lines
        GROUP BY item_id
    """).fetchdf()

    total_span_days = con.execute(
        "SELECT DATE_DIFF('day', MIN(sale_date), MAX(sale_date)) FROM sales_lines"
    ).fetchone()[0]

    print(f"\n=== Per-item history depth ({len(per_item)} items, {total_span_days} total days in data) ===")
    print(per_item["n_active_days"].describe().to_string())

    # 3. Bucket items by how many distinct days they actually sold on —
    #    this is the real "is this item intermittent" signal
    bins = [0, 5, 20, 50, 100, 10_000]
    labels = ["1-5 days", "6-20 days", "21-50 days", "51-100 days", "100+ days"]
    per_item["bucket"] = pd_cut = __import__("pandas").cut(
        per_item["n_active_days"], bins=bins, labels=labels
    )
    bucket_counts = per_item["bucket"].value_counts().sort_index()

    print("\n=== Items grouped by number of distinct days they had a sale ===")
    for label, count in bucket_counts.items():
        pct = 100 * count / len(per_item)
        print(f"  {label:>12}: {count:>5} items ({pct:5.1f}%)")

    long_tail_pct = 100 * (per_item["n_active_days"] <= 20).sum() / len(per_item)
    print(f"\n{long_tail_pct:.1f}% of items sold on 20 or fewer distinct days out of {total_span_days} days in the dataset.")
    print("If that's a large share, category-level forecasting (or Croston's/TSB for those")
    print("specific items) is the honest choice — per-item ETS/ARIMA/Prophet on a handful")
    print("of data points would not be reliable, regardless of which algorithm is picked.")

    con.close()


if __name__ == "__main__":
    main()