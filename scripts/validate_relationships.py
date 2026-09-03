"""Validate D365 relationship integrity and detect data quality issues."""
from pathlib import Path
import duckdb

DB_PATH = Path(__file__).resolve().parent / "sales_inventory.duckdb"

def run_validation():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    
    print("="*80)
    print("D365 RELATIONSHIP VALIDATION REPORT")
    print("="*80)
    print()
    
    # 1. Check orphaned inventory records
    print("1. ORPHANED INVENTORY RECORDS CHECK")
    print("-" * 80)
    orphaned = con.execute("""
        SELECT COUNT(*) AS orphaned_count
        FROM inventory_transaction
        WHERE number IS NULL 
           OR number NOT IN (SELECT sales_order_number FROM sales_order)
    """).fetchone()[0]
    
    total_inventory = con.execute("SELECT COUNT(*) FROM inventory_transaction").fetchone()[0]
    orphan_pct = (orphaned / total_inventory * 100) if total_inventory > 0 else 0
    
    print(f"Total inventory records: {total_inventory:,}")
    print(f"Orphaned records (no matching sales_order): {orphaned:,} ({orphan_pct:.2f}%)")
    print()
    
    # 2. Break down orphaned records by reference type
    print("2. ORPHANED RECORDS BY REFERENCE TYPE")
    print("-" * 80)
    orphan_breakdown = con.execute("""
        SELECT 
            reference,
            issue,
            COUNT(*) AS cnt
        FROM inventory_transaction
        WHERE number IS NULL 
           OR number NOT IN (SELECT sales_order_number FROM sales_order)
        GROUP BY reference, issue
        ORDER BY cnt DESC
        LIMIT 15
    """).fetchall()
    
    if orphan_breakdown:
        print(f"{'Reference':<25} {'Issue Status':<20} {'Count':>10}")
        print("-" * 60)
        for row in orphan_breakdown:
            print(f"{str(row[0]):<25} {str(row[1]):<20} {row[2]:>10,}")
    else:
        print("No orphaned records found!")
    print()
    
    # 3. Sales order coverage
    print("3. SALES ORDER COVERAGE ANALYSIS")
    print("-" * 80)
    coverage = con.execute("""
        SELECT 
            COUNT(DISTINCT it.number) AS orders_in_inventory,
            (SELECT COUNT(DISTINCT sales_order_number) FROM sales_order) AS orders_in_sales_table,
            COUNT(DISTINCT CASE 
                WHEN it.number IN (SELECT sales_order_number FROM sales_order) 
                THEN it.number 
            END) AS matched_orders
        FROM inventory_transaction it
        WHERE it.reference = 'Sales order'
    """).fetchone()
    
    print(f"Unique orders in inventory_transaction (ref='Sales order'): {coverage[0]:,}")
    print(f"Total orders in sales_order table: {coverage[1]:,}")
    print(f"Matched orders (inventory & sales_order): {coverage[2]:,}")
    
    if coverage[0] > coverage[1]:
        unmatched = coverage[0] - coverage[2]
        print(f"WARNING: {unmatched:,} sales orders in inventory have NO header data!")
    print()
    
    # 4. Check for NULL customer_account in v_sold
    print("4. DATA COMPLETENESS IN v_sold VIEW")
    print("-" * 80)
    null_customers = con.execute("""
        SELECT COUNT(*) 
        FROM v_sold 
        WHERE customer_account IS NULL OR customer_name IS NULL
    """).fetchone()[0]
    
    total_sold = con.execute("SELECT COUNT(*) FROM v_sold").fetchone()[0]
    null_pct = (null_customers / total_sold * 100) if total_sold > 0 else 0
    
    print(f"Total records in v_sold: {total_sold:,}")
    print(f"Records with NULL customer info: {null_customers:,} ({null_pct:.2f}%)")
    
    if null_customers > 0:
        print("WARNING: Some sold units have no customer information!")
        print("   This means inventory transactions exist without matching sales orders.")
    print()
    
    # 5. Check for duplicate keys
    print("5. REFERENTIAL INTEGRITY CHECKS")
    print("-" * 80)
    
    # Check if sales_order_number is unique
    dup_orders = con.execute("""
        SELECT COUNT(*) 
        FROM (
            SELECT sales_order_number, COUNT(*) as cnt
            FROM sales_order
            GROUP BY sales_order_number
            HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    
    print(f"Duplicate sales_order_numbers in sales_order: {dup_orders}")
    
    if dup_orders > 0:
        print("WARNING: sales_order table has duplicate keys!")
        samples = con.execute("""
            SELECT sales_order_number, COUNT(*) as cnt
            FROM sales_order
            GROUP BY sales_order_number
            HAVING COUNT(*) > 1
            LIMIT 5
        """).fetchall()
        print("   Sample duplicates:")
        for so_num, cnt in samples:
            print(f"   - {so_num}: {cnt} records")
    print()
    
    # 6. Missing SalesLine relationship analysis
    print("6. MISSING SalesLine TABLE IMPACT")
    print("-" * 80)
    print("CRITICAL: The D365 data model uses this relationship:")
    print()
    print("   SalesTable (header)")
    print("        | (1:N)")
    print("        v")
    print("   SalesLine (line items with ItemId, Qty, Price)")
    print("        | (1:N)")
    print("        v")
    print("   InventTrans (inventory movements)")
    print()
    print("Current join: inventory_transaction.number = sales_order.sales_order_number")
    print()
    print("Issues with current approach:")
    print("  1. Cannot link inventory movements to specific sales line items")
    print("  2. Missing item-level price and line amount data")
    print("  3. Cannot validate ItemId matches between SalesLine and InventTrans")
    print("  4. No line-level business logic (discounts, promotions, etc.)")
    print()
    print("Recommendation: Export SalesLine table with fields:")
    print("  - SalesId, LineNum, ItemId, SalesQty, SalesPrice, LineAmount")
    print()
    
    # 7. Summary and recommendations
    print("="*80)
    print("SUMMARY & RECOMMENDATIONS")
    print("="*80)
    
    issues = []
    if orphaned > 0:
        issues.append(f"• {orphaned:,} orphaned inventory records need investigation")
    if null_customers > 0:
        issues.append(f"• {null_customers:,} sold units lack customer information")
    if dup_orders > 0:
        issues.append(f"• Duplicate sales order numbers detected")
    
    if issues:
        print("ISSUES FOUND:")
        for issue in issues:
            print(f"   {issue}")
        print()
    else:
        print("OK: No critical data quality issues detected")
        print()
    
    print("RECOMMENDED ACTIONS:")
    print("1. Add SalesLine table export to capture line-item relationships")
    print("2. Add data validation checks in load_data.py to report orphaned records")
    print("3. Update v_sold view to explicitly handle/report NULL customer cases")
    print("4. Consider adding referential integrity constraints")
    print("5. Document expected orphan rate for non-sales inventory transactions")
    print()
    
    con.close()

if __name__ == "__main__":
    run_validation()
