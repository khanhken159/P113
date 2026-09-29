-- origin: viết tay theo tasks/cs_semester/data_model.yaml, kiểm chứng với ground truth
WITH ra AS (
    SELECT CAST(r.prof_id AS BIGINT) AS prof_id, CAST(r.capability AS BIGINT) AS capability, r.salary, s.type
    FROM {{ source('cs_semester', 'RA') }} AS r
    LEFT JOIN {{ source('cs_semester', 'student') }} AS s ON r.student_id = s.student_id
),
stats AS (
    SELECT
        prof_id,
        count(*) AS num_ra,
        100.0 * count(*) FILTER (WHERE type = 'TPG') / count(*) AS per_tpg,
        count(*) FILTER (WHERE capability = (SELECT max(capability) FROM ra)) AS num_top,
        count(*) FILTER (WHERE salary = 'high') AS num_high
    FROM ra
    GROUP BY prof_id
)
SELECT
    p.prof_id,
    p.first_name,
    p.last_name,
    coalesce(s.num_ra, 0) AS num_ra,
    s.per_tpg AS percentage_of_ra_tpg,
    coalesce(s.num_top, 0) AS num_students_with_the_highest_research_ability,
    CASE WHEN p.teachingability > (SELECT avg(teachingability) FROM {{ source('cs_semester', 'prof') }})
          AND coalesce(s.num_ra, 0) > 1 THEN 1 ELSE 0 END AS has_more_than_average_teaching_ability_and_1_student,
    coalesce(s.num_high, 0) AS num_of_students_high_salary
FROM {{ source('cs_semester', 'prof') }} AS p
LEFT JOIN stats AS s ON p.prof_id = s.prof_id
