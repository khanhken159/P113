-- reference: tpch:3
-- pattern: (đối chứng) JOIN qua CAST sang VARCHAR — DuckDB vẫn nhanh vì lọc mạnh trước JOIN
SELECT
    l_orderkey,
    sum(l_extendedprice * (1 - l_discount)) AS revenue,
    o_orderdate,
    o_shippriority
FROM customer, orders, lineitem
WHERE c_mktsegment = 'BUILDING'
  AND CAST(c_custkey AS VARCHAR) = CAST(o_custkey AS VARCHAR)
  AND CAST(l_orderkey AS VARCHAR) = CAST(o_orderkey AS VARCHAR)
  AND o_orderdate < CAST('1995-03-15' AS date)
  AND l_shipdate > CAST('1995-03-15' AS date)
GROUP BY l_orderkey, o_orderdate, o_shippriority
ORDER BY revenue DESC, o_orderdate
LIMIT 10
