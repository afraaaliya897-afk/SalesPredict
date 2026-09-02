"""Test the updated v_sold view schema."""
from pathlib import Path
import duckdb

DB_PATH = Path(__file__).resolve().parent / "sales_inventory.duckdb"

def test_view_schema():
    from text_to_sql import VIEW_DDL, count_orphaned_sold_rows

    con = duckdb.connect(str(DB_PATH))
    for ddl in VIEW_DDL:
        con.execute(ddl)

    print("OK: Views recreated with updated definitions")
    print()

    print("v_sold schema:")
    print("-" * 60)
    for col_name, col_type, *_ in con.execute("DESCRIBE v_sold").fetchall():
        print(f"  {col_name:<30} {col_type}")
    print()

    rows, qty = con.execute("SELECT COUNT(*), ROUND(SUM(sold_qty), 2) FROM v_sold").fetchone()
    print(f"v_sold rows: {rows:,}  total sold_qty: {qty:,.2f}")
    print()

    orphans = count_orphaned_sold_rows(str(DB_PATH))
    print(f"Orphaned sold rows (excluded from v_sold): {orphans}")
    sample = con.execute("""
        SELECT it.number, it.item_number, CAST(it.physical_date AS DATE), -it.quantity
        FROM inventory_transaction it
        LEFT JOIN sales_order so ON it.number = so.sales_order_number
        WHERE it.reference = 'Sales order'
          AND it.issue = 'Sold'
          AND so.sales_order_number IS NULL
        LIMIT 5
    """).fetchall()
    if sample:
        print("Sample orphaned raw rows:")
        for row in sample:
            print(f"  Order: {row[0]}, Item: {row[1]}, Date: {row[2]}, Qty: {row[3]}")
    print()
    print("OK: Schema validation complete")
    con.close()


if __name__ == "__main__":
    test_view_schema()
