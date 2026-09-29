-- origin: viết tay theo tasks/car_retails/data_model.yaml, kiểm chứng với ground truth
WITH pay AS (
    SELECT customerNumber, sum(amount) AS total_2003, count(*) AS n_2003
    FROM {{ source('car_retails', 'payments') }}
    WHERE year(paymentDate) = 2003
    GROUP BY customerNumber
),
lines AS (
    SELECT o.customerNumber, o.shippedDate, o.status, d.quantityOrdered, d.priceEach, p.buyPrice
    FROM {{ source('car_retails', 'orders') }} AS o
    JOIN {{ source('car_retails', 'orderdetails') }} AS d ON o.orderNumber = d.orderNumber
    JOIN {{ source('car_retails', 'products') }} AS p ON d.productCode = p.productCode
),
profit AS (
    SELECT customerNumber,
        sum(priceEach - buyPrice) AS actual_profit,
        sum(quantityOrdered * priceEach) FILTER (WHERE status = 'Shipped' AND year(shippedDate) = 2003) AS shipped_2003
    FROM lines
    GROUP BY customerNumber
)
SELECT
    c.customerNumber AS customernumber,
    c.customerName AS customername,
    c.country,
    CASE WHEN c.creditLimit <= 100000 THEN 1 ELSE 0 END AS has_credit_limit_not_more_than_100000,
    coalesce(pay.total_2003, 0) AS total_payments_2003,
    CASE WHEN pay.n_2003 > 3 THEN 1 ELSE 0 END AS has_paid_more_than_three_times_in_2003,
    coalesce(profit.actual_profit, 0) AS total_actual_profit_gained,
    coalesce(profit.shipped_2003, 0) AS total_price_of_product_shipped_in_2003
FROM {{ source('car_retails', 'customers') }} AS c
LEFT JOIN pay ON c.customerNumber = pay.customerNumber
LEFT JOIN profit ON c.customerNumber = profit.customerNumber
