-- origin: viết tay theo tasks/shipping/data_model.yaml, kiểm chứng với ground truth
WITH ship AS (
    SELECT s.*, c.cust_name
    FROM {{ source('shipping', 'shipment') }} AS s
    LEFT JOIN {{ source('shipping', 'customer') }} AS c ON s.cust_id = c.cust_id
),
stats AS (
    SELECT
        truck_id,
        count(*) AS num_shipments,
        100.0 * count(*) FILTER (WHERE weight > 10000) / count(*) AS per_weight_exceed_10000,
        count(*) FILTER (WHERE year(ship_date) = 2017) - count(*) FILTER (WHERE year(ship_date) = 2016) AS num_change
    FROM ship
    GROUP BY truck_id
),
heaviest AS (
    SELECT truck_id, cust_name,
        row_number() OVER (PARTITION BY truck_id ORDER BY weight DESC, cust_name ASC) AS rn
    FROM ship
)
SELECT
    t.truck_id,
    t.make,
    t.model_year,
    CASE t.make
        WHEN 'Peterbilt' THEN 'Texas (TX)'
        WHEN 'Mack' THEN 'North Carolina (NC)'
        WHEN 'Kenworth' THEN 'Washington (WA)'
    END AS headquarter,
    coalesce(s.num_shipments, 0) AS num_shipments,
    h.cust_name AS person_receive_heaviest_shipment,
    s.per_weight_exceed_10000,
    coalesce(s.num_change, 0) AS num_shipments_change_from_2016_to_2017
FROM {{ source('shipping', 'truck') }} AS t
LEFT JOIN stats AS s ON t.truck_id = s.truck_id
LEFT JOIN heaviest AS h ON t.truck_id = h.truck_id AND h.rn = 1
