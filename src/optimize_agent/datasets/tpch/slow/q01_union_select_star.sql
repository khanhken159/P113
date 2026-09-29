-- reference: tpch:1
-- pattern: UNION (khử trùng) thay vì đọc thẳng bảng + SELECT * kéo mọi cột của lineitem
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
    SELECT * FROM lineitem WHERE l_returnflag = 'R'
    UNION
    SELECT * FROM lineitem WHERE l_returnflag <> 'R'
) AS l
WHERE l_shipdate <= CAST('1998-09-02' AS date)
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus
