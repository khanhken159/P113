-- origin: viết tay theo tasks/books/data_model.yaml, kiểm chứng với ground truth
-- Tie: ground truth dùng RANK() -> khi hòa cả khóa phá hòa, sinh nhiều dòng giống nhau (giữ nguyên hành vi này).
WITH b AS (
    SELECT bk.*, l.language_name
    FROM {{ source('books', 'book') }} AS bk
    LEFT JOIN {{ source('books', 'book_language') }} AS l ON bk.language_id = l.language_id
),
stats AS (
    SELECT publisher_id,
        count(*) AS num_books,
        count(*) FILTER (WHERE num_pages > 0.7 * (SELECT avg(num_pages) FROM b)) AS num_long,
        100.0 * count(*) FILTER (WHERE language_name = 'Japanese') / count(*) AS per_japanese
    FROM b
    GROUP BY publisher_id
),
oldest AS (
    SELECT publisher_id, title,
        rank() OVER (PARTITION BY publisher_id ORDER BY publication_date ASC, title ASC) AS rk
    FROM b
),
most_pages AS (
    SELECT publisher_id, title,
        rank() OVER (PARTITION BY publisher_id ORDER BY num_pages DESC, title DESC) AS rk
    FROM b
)
SELECT
    p.publisher_id,
    p.publisher_name,
    coalesce(s.num_books, 0) AS num_books,
    coalesce(s.num_long, 0) AS num_books_pages_greater_than_70_per_of_avg,
    s.per_japanese AS per_japanese_books,
    o.title AS title_of_the_oldest_book,
    m.title AS book_title_with_the_most_pages
FROM {{ source('books', 'publisher') }} AS p
LEFT JOIN stats AS s ON p.publisher_id = s.publisher_id
LEFT JOIN oldest AS o ON p.publisher_id = o.publisher_id AND o.rk = 1
LEFT JOIN most_pages AS m ON p.publisher_id = m.publisher_id AND m.rk = 1
