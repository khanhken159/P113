-- origin: viết tay theo tasks/books/data_model.yaml, kiểm chứng với ground truth
WITH orders AS (
    SELECT o.*, sm.method_name
    FROM {{ source('books', 'cust_order') }} AS o
    LEFT JOIN {{ source('books', 'shipping_method') }} AS sm ON o.shipping_method_id = CAST(sm.method_id AS BIGINT)
),
lines AS (
    SELECT o.customer_id, ol.book_id, ol.price
    FROM orders AS o
    JOIN {{ source('books', 'order_line') }} AS ol ON o.order_id = ol.order_id
),
line_stats AS (
    SELECT customer_id,
        100.0 * count(*) FILTER (WHERE price > 13) / count(*) AS per_over_13,
        max(CASE WHEN book_id IN (SELECT book_id FROM {{ source('books', 'book') }}
                 WHERE publication_date = (SELECT min(publication_date) FROM {{ source('books', 'book') }}))
            THEN 1 ELSE 0 END) AS has_oldest
    FROM lines
    GROUP BY customer_id
),
order_stats AS (
    SELECT customer_id,
        count(*) AS num_orders,
        100.0 * count(*) FILTER (WHERE method_name = 'International') / count(*) AS per_international
    FROM orders
    GROUP BY customer_id
),
preferred AS (
    SELECT customer_id, method_name,
        row_number() OVER (PARTITION BY customer_id ORDER BY count(*) DESC, method_name ASC) AS rn
    FROM orders
    GROUP BY customer_id, method_name
)
SELECT
    c.customer_id,
    c.first_name || ' ' || c.last_name AS customer_name,
    c.email,
    ls.per_over_13 AS per_books_over_13,
    coalesce(os.num_orders, 0) AS num_orders,
    p.method_name AS preferred_shipping_method,
    coalesce(ls.has_oldest, 0) AS has_ordered_the_oldest_book,
    os.per_international AS per_order_shipped_internationally
FROM {{ source('books', 'customer') }} AS c
LEFT JOIN line_stats AS ls ON c.customer_id = ls.customer_id
LEFT JOIN order_stats AS os ON c.customer_id = os.customer_id
LEFT JOIN preferred AS p ON c.customer_id = p.customer_id AND p.rn = 1
