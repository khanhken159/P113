-- origin: viết tay theo tasks/shipping/data_model.yaml, kiểm chứng với ground truth
WITH ship AS (
    SELECT s.*, c.cust_name
    FROM {{ source('shipping', 'shipment') }} AS s
    LEFT JOIN {{ source('shipping', 'customer') }} AS c ON s.cust_id = c.cust_id
),
least_city AS (
    SELECT CAST(city_id AS BIGINT) AS city_id FROM {{ source('shipping', 'city') }}
    WHERE population = (SELECT min(population) FROM {{ source('shipping', 'city') }})
),
stats AS (
    SELECT
        driver_id,
        count(*) FILTER (WHERE year(ship_date) = 2017) AS num_2017,
        count(*) FILTER (WHERE city_id IN (SELECT city_id FROM least_city)) AS num_least,
        100.0 * count(*) FILTER (WHERE cust_name = 'Autoware Inc') / count(*) AS per_autoware,
        max(CASE WHEN weight > 0.95 * (SELECT avg(weight) FROM ship) THEN 1 ELSE 0 END) AS has_heavy
    FROM ship
    GROUP BY driver_id
),
first_ship AS (
    SELECT driver_id, weight,
        row_number() OVER (PARTITION BY driver_id ORDER BY ship_date ASC, weight DESC) AS rn
    FROM ship
)
SELECT
    d.driver_id,
    d.first_name,
    d.last_name,
    coalesce(s.num_2017, 0) AS num_shipments_2017,
    coalesce(s.num_least, 0) AS num_shipment_to_least_populated_city,
    s.per_autoware AS per_shipment_placed_by_autoware_inc,
    coalesce(s.has_heavy, 0) AS has_a_shipment_weight_greater_95_per_avg_across_all_shipments,
    f.weight AS weight_first_shipment
FROM {{ source('shipping', 'driver') }} AS d
LEFT JOIN stats AS s ON d.driver_id = s.driver_id
LEFT JOIN first_ship AS f ON d.driver_id = f.driver_id AND f.rn = 1
