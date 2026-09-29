-- origin: ELT-Bench-Verified example/retails/nations.sql (Snowflake, transpile bằng sqlglot)
WITH num_suppliers_in_debt_cte AS (
  SELECT
    s_nationkey,
    COUNT(s_suppkey) AS num_suppliers_in_debt
  FROM {{ source('retails', 'supplier') }}
  WHERE
    s_acctbal < 0
  GROUP BY
    s_nationkey
), num_customers_cte AS (
  SELECT
    c_nationkey,
    COUNT(c_custkey) AS num_customers
  FROM {{ source('retails', 'customer') }}
  GROUP BY
    c_nationkey
), balance_larger_thant_avg_balance_cte AS (
  SELECT
    c_nationkey
  FROM {{ source('retails', 'customer') }}
  GROUP BY
    c_nationkey
  HAVING
    AVG(c_acctbal) > (
      SELECT
        AVG(c_acctbal) AS avg_acctbal
      FROM {{ source('retails', 'customer') }}
    )
), total_price_of_all_orders_cte AS (
  SELECT
    T2.c_nationkey,
    SUM(T1.o_totalprice) AS total_price_of_all_orders
  FROM {{ source('retails', 'orders') }} AS T1
  JOIN {{ source('retails', 'customer') }} AS T2
    ON T1.o_custkey = T2.c_custkey
  GROUP BY
    T2.c_nationkey
), per_indebted_suppliers AS (
  SELECT
    s_nationkey,
    CAST(SUM(CASE WHEN s_acctbal < 0 THEN 1 ELSE 0 END) AS REAL) * 100 / COUNT(s_suppkey) AS per_indebted_suppliers
  FROM {{ source('retails', 'supplier') }}
  GROUP BY
    s_nationkey
)
SELECT
  T1.n_nationkey AS n_nationkey,
  T1.n_name AS n_name,
  T1.n_regionkey AS n_regionkey,
  CASE WHEN T2.num_suppliers_in_debt IS NULL THEN 0 ELSE T2.num_suppliers_in_debt END AS num_suppliers_in_debt,
  CASE WHEN T3.num_customers IS NULL THEN 0 ELSE T3.num_customers END AS num_customers,
  CASE WHEN T4.c_nationkey IS NULL THEN 0 ELSE 1 END AS balance_larger_thant_avg_balance,
  CASE
    WHEN T5.total_price_of_all_orders IS NULL
    THEN 0
    ELSE T5.total_price_of_all_orders
  END AS total_price_of_all_orders,
  T6.per_indebted_suppliers
FROM {{ source('retails', 'nation') }} AS T1
LEFT JOIN num_suppliers_in_debt_cte AS T2
  ON T1.n_nationkey = T2.s_nationkey
LEFT JOIN num_customers_cte AS T3
  ON T1.n_nationkey = T3.c_nationkey
LEFT JOIN balance_larger_thant_avg_balance_cte AS T4
  ON T1.n_nationkey = T4.c_nationkey
LEFT JOIN total_price_of_all_orders_cte AS T5
  ON T1.n_nationkey = T5.c_nationkey
LEFT JOIN per_indebted_suppliers AS T6
  ON T1.n_nationkey = T6.s_nationkey
