-- reference: tpch:18
-- pattern: subquery IN đọc lineitem qua UNION tự hợp với chính nó (khử trùng vô ích)
SELECT
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
FROM customer, orders, lineitem
WHERE o_orderkey IN (
        SELECT l_orderkey
        FROM (SELECT * FROM lineitem UNION SELECT * FROM lineitem)
        GROUP BY l_orderkey
        HAVING sum(l_quantity) > 300
    )
  AND c_custkey = o_custkey
  AND o_orderkey = l_orderkey
GROUP BY c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice
ORDER BY o_totalprice DESC, o_orderdate
LIMIT 100
