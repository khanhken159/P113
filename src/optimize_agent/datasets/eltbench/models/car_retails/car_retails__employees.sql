-- origin: viết tay theo tasks/car_retails/data_model.yaml, kiểm chứng với ground truth
WITH emp AS (
    SELECT *, CAST(employeeNumber AS BIGINT) AS emp_no FROM {{ source('car_retails', 'employees') }}
),
cust AS (
    SELECT salesRepEmployeeNumber AS emp_no, count(*) AS n, max(creditLimit) AS max_credit
    FROM {{ source('car_retails', 'customers') }}
    GROUP BY salesRepEmployeeNumber
),
order_amounts AS (
    SELECT c.salesRepEmployeeNumber AS emp_no, p.productName,
        row_number() OVER (PARTITION BY c.salesRepEmployeeNumber
                           ORDER BY d.quantityOrdered * d.priceEach DESC, p.productName ASC) AS rn
    FROM {{ source('car_retails', 'customers') }} AS c
    JOIN {{ source('car_retails', 'orders') }} AS o ON c.customerNumber = o.customerNumber
    JOIN {{ source('car_retails', 'orderdetails') }} AS d ON o.orderNumber = d.orderNumber
    JOIN {{ source('car_retails', 'products') }} AS p ON d.productCode = p.productCode
),
top_payment AS (
    SELECT c.salesRepEmployeeNumber AS emp_no, c.customerName,
        row_number() OVER (PARTITION BY c.salesRepEmployeeNumber ORDER BY pay.amount DESC, c.customerName DESC) AS rn
    FROM {{ source('car_retails', 'customers') }} AS c
    JOIN {{ source('car_retails', 'payments') }} AS pay ON c.customerNumber = pay.customerNumber
)
SELECT
    e.employeeNumber AS employeenumber,
    e.officeCode AS officecode,
    e.firstName || ' ' || e.lastName AS name,
    coalesce(cust.n, 0) AS number_of_customers,
    CASE WHEN o.city = 'Sydney' THEN 1 ELSE 0 END AS is_in_sydney,
    oa.productName AS product_name_of_the_highest_amount_of_order,
    tp.customerName AS customer_made_highest_payment,
    cust.max_credit AS highest_customer_credit_limit
FROM emp AS e
LEFT JOIN {{ source('car_retails', 'offices') }} AS o ON e.officeCode = o.officeCode
LEFT JOIN cust ON e.emp_no = cust.emp_no
LEFT JOIN order_amounts AS oa ON e.emp_no = oa.emp_no AND oa.rn = 1
LEFT JOIN top_payment AS tp ON e.emp_no = tp.emp_no AND tp.rn = 1
