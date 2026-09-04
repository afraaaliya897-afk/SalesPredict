-- Run this query in your database to check data quality
-- This shows monthly sales patterns and variability

SELECT 
    DATE_TRUNC('month', sale_date) AS month,
    COUNT(DISTINCT item_number) AS unique_items,
    SUM(sold_qty) AS total_units,
    COUNT(DISTINCT sales_order_number) AS order_count,
    AVG(sold_qty) AS avg_per_sale,
    STDDEV(sold_qty) AS stddev_qty
FROM v_sold
WHERE sale_date >= DATE '2024-01-01'
GROUP BY 1
ORDER BY 1;

-- Also check top items stability
SELECT 
    item_number,
    COUNT(DISTINCT DATE_TRUNC('month', sale_date)) AS months_active,
    SUM(sold_qty) AS total_qty,
    AVG(sold_qty) AS avg_per_day,
    STDDEV(sold_qty) AS variability
FROM v_sold
WHERE sale_date >= DATE '2024-01-01'
GROUP BY 1
ORDER BY 3 DESC
LIMIT 20;
