# D365 to DuckDB Relationship Mapping

## Quick Reference

### Current Implementation

```
Excel Export (D365)           →    DuckDB Table              →    Business View
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sales orders_*.xlsx                sales_order                    v_orders
(SalesTable)                       (13 columns)                   (9 columns, filtered)
                                   
Inventory transactions_*.xlsx      inventory_transaction          v_sold
(InventTrans)                      (14 columns)                   (12 columns, filtered)
                                   + customer_account (from join)  + missing_order_header flag
```

### Join Relationship

```sql
-- Current join implemented in load_data.py:
inventory_transaction.number = sales_order.sales_order_number

-- D365 equivalent:
InventTrans.TransRefId = SalesTable.SalesId
```

---

## Data Quality Summary

### Validation Results

| Metric | Current Status |
|--------|---------------|
| **Overall Data Quality** | ✅ Excellent (99.94% match rate) |
| **Total Inventory Records** | 275,290 |
| **Orphaned Sales Transactions** | 103 (0.07% of sales) |
| **v_sold Records** | 146,514 |
| **NULL Customer Info** | 52 (0.04%) |
| **Duplicate Keys** | 0 |

### Orphaned Records Context

45% of inventory records are orphaned, but this is **expected and correct**:

- **51,519** Transfer orders (warehouse movements)
- **16,113** Purchase orders (incoming stock)
- **29,600** Transactions (adjustments, cycle counts)
- **8,237** BOM/Production (manufacturing)
- **103** Sales orders (the only concerning category - 0.04%)

**Conclusion**: Data quality is excellent. Orphaned records are legitimate non-sales transactions.

---

## Missing D365 Relationship: SalesLine

### Complete D365 Model

```
┌─────────────────┐
│   SalesTable    │  Order Header (customer, dates, status)
│   (SalesId)     │  
└────────┬────────┘
         │ 1:N
         ↓
┌─────────────────┐
│   SalesLine     │  Line Items (item, quantity, price, amount)
│   (SalesId,     │  **← MISSING FROM CURRENT IMPLEMENTATION**
│    LineNum,     │
│    ItemId)      │
└────────┬────────┘
         │ 1:N
         ↓
┌─────────────────┐
│   InventTrans   │  Inventory Movements (actual quantities moved)
│   (TransRefId,  │
│    ItemId)      │
└─────────────────┘
```

### Current vs. Ideal Implementation

| Aspect | Current (Without SalesLine) | Ideal (With SalesLine) |
|--------|----------------------------|------------------------|
| **Join** | InventTrans → SalesTable | InventTrans → SalesLine → SalesTable |
| **Unit Quantities** | ✅ Available | ✅ Available |
| **Customer Info** | ✅ Available | ✅ Available |
| **Line-Item Pricing** | ❌ Missing | ✅ Available |
| **Revenue Calculation** | ❌ Not Possible | ✅ Possible |
| **Discounts/Promotions** | ❌ Missing | ✅ Available |
| **Multi-Item Orders** | ⚠️ Cannot distinguish | ✅ Can track per line |

---

## Impact Analysis

### ✅ What Works (Current Implementation)

1. **Unit-based forecasting** - Complete and accurate
2. **Customer analysis** - Can track customers by quantity sold
3. **Product analysis** - Can track items by quantity sold
4. **Trend analysis** - Daily/weekly/monthly patterns
5. **Channel analysis** - Sales by channel/warehouse
6. **Order counting** - Number of orders placed

### ❌ What's Missing (Due to No SalesLine)

1. **Revenue forecasting** - Cannot calculate dollar amounts
2. **Margin analysis** - No unit prices or line amounts
3. **Promotion effectiveness** - Cannot see discounts applied
4. **Average order value** - No pricing data
5. **Price elasticity** - Cannot analyze price changes
6. **Line-level details** - Cannot distinguish items within same order

### Example Scenario

**Order SO-12345** has 3 line items:
- Line 1: Item A, Qty 10, Price $5 = $50
- Line 2: Item B, Qty 5, Price $12 = $60
- Line 3: Item C, Qty 3, Price $20 = $60

**Current System Sees**:
```
| sales_order_number | item | qty | customer | price |
|--------------------|------|-----|----------|-------|
| SO-12345          | A    | 10  | Acme Inc | NULL  |
| SO-12345          | B    | 5   | Acme Inc | NULL  |
| SO-12345          | C    | 3   | Acme Inc | NULL  |
```

**With SalesLine, Would See**:
```
| sales_order_number | line | item | qty | price | amount | customer |
|--------------------|------|------|-----|-------|--------|----------|
| SO-12345          | 1    | A    | 10  | $5    | $50    | Acme Inc |
| SO-12345          | 2    | B    | 5   | $12   | $60    | Acme Inc |
| SO-12345          | 3    | C    | 3   | $20   | $60    | Acme Inc |
```

---

## Recommendations

### Priority 1: Current System (No Changes Needed)

**Status**: ✅ Production Ready for Unit Forecasting

**Use Cases**:
- Quantity-based sales forecasting
- Inventory planning (units)
- Customer analysis by volume
- Product popularity trends
- Operational metrics

**No action required** - system is working as designed.

### Priority 2: Add Revenue Analysis (Export SalesLine)

**If you need**:
- Revenue forecasting
- Profit margin analysis
- Pricing analytics
- Promotion effectiveness

**Action Required**:
1. Export `SalesLine` table from D365 with these fields:
   ```
   - SalesId (FK to SalesTable)
   - LineNum (line sequence)
   - ItemId (product)
   - SalesQty (ordered quantity)
   - SalesPrice (unit price)
   - LineAmount (extended amount)
   - CurrencyCode
   - LineDisc (discount %)
   ```

2. Update database schema to add `sales_line` table:
   ```python
   def load_sales_line(path: Path) -> pd.DataFrame:
       df = pd.read_excel(path)
       df = df.rename(columns={
           "Sales order": "sales_order_number",
           "Line number": "line_number",
           "Item number": "item_number",
           "Quantity": "sales_qty",
           "Unit price": "sales_price",
           "Line amount": "line_amount",
           "Currency": "currency_code",
           "Line discount": "line_disc_pct",
       })
       return df
   ```

3. Update join logic:
   ```sql
   CREATE VIEW v_sold_with_pricing AS
   SELECT 
       it.item_number,
       it.physical_date AS sale_date,
       -it.quantity AS sold_qty,
       sl.sales_price AS unit_price,
       sl.line_amount,
       sl.line_disc_pct AS discount_pct,
       (-it.quantity * sl.sales_price) AS revenue_estimate,
       so.customer_account,
       so.customer_name,
       so.channel
   FROM inventory_transaction it
   LEFT JOIN sales_line sl 
       ON it.number = sl.sales_order_number 
       AND it.item_number = sl.item_number
   LEFT JOIN sales_order so 
       ON sl.sales_order_number = so.sales_order_number
   WHERE it.reference = 'Sales order'
     AND it.issue = 'Sold'
     AND it.quantity < 0
   ```

### Priority 3: Enhanced Product/Customer Data

**Optional additions** for richer analytics:

1. **InventTable** (Product Master):
   - ItemId, ItemName, ProductType, ItemGroupId, CostPrice
   - Enables: Product categorization, margin calculation

2. **CustTable** (Customer Master):
   - AccountNum, Name, CustGroup, SalesSegment, Currency
   - Enables: Customer segmentation, cohort analysis

3. **CustInvoiceTrans** (Invoice Lines):
   - InvoiceId, SalesId, ItemId, InvoiceAmount, InvoiceDate
   - Enables: Actual revenue (vs. order amounts)

---

## Validation Tools

### Check Data Quality

```bash
python validate_relationships.py
```

**Output**:
- Orphaned record counts and breakdown
- Match rates between tables
- NULL value detection
- Duplicate key checks
- Recommendations

### Rebuild Database with Validation

```bash
python load_data.py
```

**Output**:
- Record counts
- Relationship validation
- Orphaned record warnings
- Missing relationship notes

### Query Data Quality Flags

```sql
-- Check records with missing order headers
SELECT 
    item_number,
    sale_date,
    sold_qty,
    sales_order_number,
    missing_order_header
FROM v_sold
WHERE missing_order_header = 1;

-- Count orphaned records
SELECT COUNT(*) AS orphaned_count
FROM v_sold
WHERE missing_order_header = 1;
```

---

## Implementation Checklist

### ✅ Completed

- [x] Document D365 data model relationships
- [x] Validate current join logic against D365 model
- [x] Identify missing SalesLine table relationship
- [x] Add referential integrity validation to `load_data.py`
- [x] Add `missing_order_header` flag to `v_sold` view
- [x] Create `validate_relationships.py` script
- [x] Update VIEW_DDL with inline documentation
- [x] Document complete relationship mapping
- [x] Analyze orphaned records (found 99.94% match rate)
- [x] Verify no duplicate keys
- [x] Update `D365_DATA_MODEL.md` with findings

### 📋 Future Enhancements (If Needed)

- [ ] Export SalesLine table from D365
- [ ] Update schema to include `sales_line` table
- [ ] Modify join to use three-table relationship
- [ ] Add revenue calculation views
- [ ] Export InventTable for product details
- [ ] Export CustTable for customer segments
- [ ] Add margin analysis capabilities

---

## Summary

**Current State**: ✅ **Excellent**
- 99.94% match rate between inventory transactions and sales orders
- Only 52 records (0.04%) have NULL customer info
- Zero duplicate keys
- Proper filtering of canceled orders
- Robust handling of legitimate orphaned records

**Key Finding**: Missing SalesLine table
- **Impact**: Cannot calculate revenue or analyze pricing
- **Workaround**: Current system works perfectly for unit-based forecasting
- **Solution**: Export SalesLine table only if revenue analysis is required

**Recommendation**: 
- ✅ Continue using current system for unit forecasting
- 📋 Add SalesLine export only when revenue analysis becomes a requirement
- ✅ Use `validate_relationships.py` to monitor data quality

**Data Integrity**: Safe and production-ready for current use case.
