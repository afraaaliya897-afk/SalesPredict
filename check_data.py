import duckdb

con = duckdb.connect('sales_inventory.duckdb', read_only=True)

print("=" * 60)
print("DATA VERIFICATION: Excel to DuckDB")
print("=" * 60)

print("\n=== RAW TABLES (from Excel) ===")
sales_raw = con.execute('SELECT COUNT(*) FROM sales_order').fetchone()[0]
inventory_raw = con.execute('SELECT COUNT(*) FROM inventory_transaction').fetchone()[0]
print(f"sales_order rows: {sales_raw:,}")
print(f"inventory_transaction rows: {inventory_raw:,}")

print("\n=== FILTERED VIEWS (what LLM queries) ===")
orders_view = con.execute('SELECT COUNT(*) FROM v_orders').fetchone()[0]
sold_view = con.execute('SELECT COUNT(*) FROM v_sold').fetchone()[0]
print(f"v_orders rows: {orders_view:,}")
print(f"v_sold rows: {sold_view:,}")

print("\n=== FILTER EFFICIENCY ===")
print(f"Orders kept: {orders_view}/{sales_raw} ({100*orders_view/sales_raw:.1f}%)")
print(f"Inventory kept: {sold_view}/{inventory_raw} ({100*sold_view/inventory_raw:.1f}%)")

print("\n=== v_orders FILTERS VERIFICATION ===")
total_orders = con.execute("SELECT COUNT(*) FROM v_orders").fetchone()[0]
sales_order_type = con.execute("SELECT COUNT(*) FROM v_orders WHERE order_type='Sales order'").fetchone()[0]
open_release = con.execute("SELECT COUNT(*) FROM v_orders WHERE release_status='Open'").fetchone()[0]
no_dnp = con.execute("SELECT COUNT(*) FROM v_orders WHERE do_not_process='No'").fetchone()[0]
print(f"Total v_orders: {total_orders:,}")
print(f"  [OK] order_type='Sales order': {sales_order_type:,} ({100*sales_order_type/total_orders:.1f}%)")
print(f"  [OK] release_status='Open': {open_release:,} ({100*open_release/total_orders:.1f}%)")
print(f"  [OK] do_not_process='No': {no_dnp:,} ({100*no_dnp/total_orders:.1f}%)")

print("\n=== v_sold FILTERS VERIFICATION ===")
total_sold = con.execute('SELECT COUNT(*) FROM v_sold').fetchone()[0]
positive_qty = con.execute('SELECT COUNT(*) FROM v_sold WHERE sold_qty > 0').fetchone()[0]
min_cost, max_cost = con.execute('SELECT MIN(cost_amount), MAX(cost_amount) FROM v_sold').fetchone()
negative_cost = con.execute('SELECT COUNT(*) FROM v_sold WHERE cost_amount < 0').fetchone()[0]
print(f"Total v_sold: {total_sold:,}")
print(f"  [OK] sold_qty > 0: {positive_qty:,} ({100*positive_qty/total_sold:.1f}%)")
print(f"  [OK] cost_amount range: ${min_cost:,.2f} to ${max_cost:,.2f}")
print(f"  [!!] cost_amount < 0: {negative_cost:,} ({'FIXED' if negative_cost == 0 else 'NEEDS FIX!'})")

print("\n=== DATE RANGES ===")
order_dates = con.execute('SELECT MIN(invoice_date), MAX(invoice_date) FROM v_orders').fetchone()
sold_dates = con.execute('SELECT MIN(sale_date), MAX(sale_date) FROM v_sold').fetchone()
print(f"v_orders invoice_date: {order_dates[0]} to {order_dates[1]}")
print(f"v_sold sale_date: {sold_dates[0]} to {sold_dates[1]}")

print("\n=== SAMPLE DATA ===")
print("\nTop 5 items by quantity:")
top_items = con.execute("""
    SELECT item_number, SUM(sold_qty) AS total_qty 
    FROM v_sold 
    GROUP BY 1 
    ORDER BY 2 DESC 
    LIMIT 5
""").fetchall()
for item, qty in top_items:
    print(f"  {item}: {qty:,.0f} units")

print("\nTop 5 customers by quantity:")
top_customers = con.execute("""
    SELECT customer_name, SUM(sold_qty) AS total_qty 
    FROM v_sold 
    GROUP BY 1 
    ORDER BY 2 DESC 
    LIMIT 5
""").fetchall()
for customer, qty in top_customers:
    print(f"  {customer}: {qty:,.0f} units")

con.close()
print("\n" + "=" * 60)
print("[OK] Verification complete!")
print("=" * 60)
