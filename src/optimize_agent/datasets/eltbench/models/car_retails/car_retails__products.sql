-- origin: viết tay theo tasks/car_retails/data_model.yaml, kiểm chứng với ground truth
WITH lines AS (
    SELECT d.productCode, d.quantityOrdered, d.priceEach, o.orderDate, o.customerNumber
    FROM {{ source('car_retails', 'orderdetails') }} AS d
    JOIN {{ source('car_retails', 'orders') }} AS o ON d.orderNumber = o.orderNumber
),
stats AS (
    SELECT productCode,
        sum(quantityOrdered) AS n_ordered,
        count(customerNumber) AS n_customers,
        sum(quantityOrdered) FILTER (WHERE year(orderDate) = 2003) AS qty_2003,
        sum(quantityOrdered * priceEach) / sum(quantityOrdered) AS avg_price
    FROM lines
    GROUP BY productCode
)
SELECT
    p.productCode AS productcode,
    p.productName AS productname,
    p.buyPrice AS buyprice,
    coalesce(s.n_ordered, 0) AS number_of_product_ordered,
    p.MSRP - p.buyPrice AS expected_profit_per_piece,
    coalesce(s.n_customers, 0) AS num_of_customers,
    coalesce(s.qty_2003, 0) AS total_quantity_sold_in_2003,
    s.avg_price - p.buyPrice AS average_actual_profit  -- trung bình có trọng số theo số lượng
FROM {{ source('car_retails', 'products') }} AS p
LEFT JOIN stats AS s ON p.productCode = s.productCode
