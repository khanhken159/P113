-- reference: tpch:1
-- pattern: khử trùng bằng row_number() trên khóa vốn đã duy nhất
SELECT
    l_returnflag,
    l_linestatus,
    sum(l_quantity) AS sum_qty,
    sum(l_extendedprice) AS sum_base_price,
    sum(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
    sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
    avg(l_quantity) AS avg_qty,
    avg(l_extendedprice) AS avg_price,
    avg(l_discount) AS avg_disc,
    count(*) AS count_order
FROM (
    SELECT *
    FROM (
        SELECT *, row_number() OVER (PARTITION BY l_orderkey, l_linenumber ORDER BY l_shipdate) AS rn
        FROM lineitem
    )
    WHERE rn = 1
) AS l
WHERE l_shipdate <= CAST('1998-09-02' AS date)
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus
