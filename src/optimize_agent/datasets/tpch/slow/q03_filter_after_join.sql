-- reference: tpch:3
-- pattern: (đối chứng) lọc sau JOIN trong CTE MATERIALIZED — DuckDB vẫn đẩy filter xuống
WITH joined AS MATERIALIZED (
    SELECT * FROM customer JOIN orders ON c_custkey = o_custkey JOIN lineitem ON l_orderkey = o_orderkey
)
SELECT
    l_orderkey,
    sum(l_extendedprice * (1 - l_discount)) AS revenue,
    o_orderdate,
    o_shippriority
FROM joined
WHERE c_mktsegment = 'BUILDING'
  AND o_orderdate < CAST('1995-03-15' AS date)
  AND l_shipdate > CAST('1995-03-15' AS date)
GROUP BY l_orderkey, o_orderdate, o_shippriority
ORDER BY revenue DESC, o_orderdate
LIMIT 10
