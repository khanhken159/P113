-- origin: viết tay theo tasks/cs_semester/data_model.yaml, kiểm chứng với ground truth
WITH reg AS (
    SELECT r.*, s.type
    FROM {{ source('cs_semester', 'registration') }} AS r
    LEFT JOIN {{ source('cs_semester', 'student') }} AS s ON r.student_id = CAST(s.student_id AS BIGINT)
),
stats AS (
    SELECT
        course_id,
        count(*) FILTER (WHERE grade = 'A') AS num_a,
        100.0 * count(*) FILTER (WHERE sat = (SELECT max(sat) FROM reg)) / count(*) AS per_top_sat,
        count(*) FILTER (WHERE grade IS NULL) AS num_fail,
        100.0 * count(*) FILTER (WHERE type = 'UG') / count(*) AS per_ug,
        avg(sat) AS avg_sat
    FROM reg
    GROUP BY course_id
)
SELECT
    c.course_id,
    c.name,
    c.credit,
    coalesce(s.num_a, 0) AS num_students_got_a,
    s.per_top_sat AS per_of_students_highest_satisfaction_score_for_the_course,
    coalesce(s.num_fail, 0) AS number_of_students_fail,
    s.per_ug AS percentage_of_ug,
    s.avg_sat AS average_satisfying_degree
FROM {{ source('cs_semester', 'course') }} AS c
LEFT JOIN stats AS s ON c.course_id = s.course_id
