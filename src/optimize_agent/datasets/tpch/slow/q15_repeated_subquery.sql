-- reference: tpch:15
-- pattern: (đối chứng) CTE tính lặp — cùng subquery doanh thu viết 2 lần; DuckDB tự tái dùng subplan
SELECT
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    r.total_revenue
FROM supplier,
    (SELECT l_suppkey AS supplier_no, sum(l_extendedprice * (1 - l_discount)) AS total_revenue
              FROM lineitem
              WHERE l_shipdate >= CAST('1996-01-01' AS date) AND l_shipdate < CAST('1996-04-01' AS date)
              GROUP BY l_suppkey) AS r
WHERE s_suppkey = r.supplier_no
  AND r.total_revenue = (
        SELECT max(total_revenue)
        FROM (SELECT l_suppkey AS supplier_no, sum(l_extendedprice * (1 - l_discount)) AS total_revenue
              FROM lineitem
              WHERE l_shipdate >= CAST('1996-01-01' AS date) AND l_shipdate < CAST('1996-04-01' AS date)
              GROUP BY l_suppkey)
    )
ORDER BY s_suppkey
