-- reference: tpch:9
-- pattern: khử trùng bằng row_number() trên khóa vốn đã duy nhất trước khi JOIN
SELECT nation, o_year, sum(amount) AS sum_profit
FROM (
    SELECT
        n_name AS nation,
        extract(year FROM o_orderdate) AS o_year,
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity AS amount
    FROM part, supplier,
        (SELECT * EXCLUDE (rn)
         FROM (SELECT *, row_number() OVER (PARTITION BY l_orderkey, l_linenumber ORDER BY l_shipdate) AS rn FROM lineitem)
         WHERE rn = 1) AS lineitem,
        partsupp, orders, nation
    WHERE s_suppkey = l_suppkey
      AND ps_suppkey = l_suppkey
      AND ps_partkey = l_partkey
      AND p_partkey = l_partkey
      AND o_orderkey = l_orderkey
      AND s_nationkey = n_nationkey
      AND p_name LIKE '%green%'
) AS profit
GROUP BY nation, o_year
ORDER BY nation, o_year DESC
