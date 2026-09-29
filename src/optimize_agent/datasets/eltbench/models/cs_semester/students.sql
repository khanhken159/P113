-- origin: viết tay theo tasks/cs_semester/data_model.yaml, kiểm chứng với ground truth
-- Lưu ý: ground truth để NULL cho num_courses_taken khi sinh viên không đăng ký môn nào (không COALESCE).
WITH mlt_b AS (
    SELECT DISTINCT r.student_id
    FROM {{ source('cs_semester', 'registration') }} AS r
    JOIN {{ source('cs_semester', 'course') }} AS c ON r.course_id = c.course_id
    WHERE c.name = 'Machine Learning Theory' AND r.grade = 'B'
),
top_prof_students AS (
    SELECT DISTINCT CAST(r.student_id AS BIGINT) AS student_id
    FROM {{ source('cs_semester', 'RA') }} AS r
    JOIN {{ source('cs_semester', 'prof') }} AS p ON CAST(r.prof_id AS BIGINT) = p.prof_id
    WHERE p.popularity = (SELECT max(popularity) FROM {{ source('cs_semester', 'prof') }})
),
courses AS (
    SELECT student_id, count(*) AS n FROM {{ source('cs_semester', 'registration') }} GROUP BY student_id
),
st AS (
    SELECT *, CAST(student_id AS BIGINT) AS sid FROM {{ source('cs_semester', 'student') }}
)
SELECT
    st.student_id,
    st.l_name AS last_name,
    st.f_name AS first_name,
    CASE WHEN st.sid IN (SELECT student_id FROM mlt_b) AND CAST(st.gpa AS DOUBLE) > 3 THEN 1 ELSE 0 END AS got_b_in_mlt_and_gpa_over3,
    CASE WHEN st.type = 'UG' AND CAST(st.intelligence AS BIGINT) =
        (SELECT max(CAST(intelligence AS BIGINT)) FROM st WHERE type = 'UG') THEN 1 ELSE 0 END AS has_the_highest_intelligence_ug,
    CASE WHEN st.sid IN (SELECT student_id FROM top_prof_students) THEN 1 ELSE 0 END AS is_under_supervision_of_most_popular_professor,
    c.n AS num_courses_taken
FROM st
LEFT JOIN courses AS c ON st.sid = c.student_id
