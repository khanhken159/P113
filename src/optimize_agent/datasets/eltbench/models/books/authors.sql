-- origin: viết tay theo tasks/books/data_model.yaml, kiểm chứng với ground truth
-- Tie: ground truth dùng RANK() -> khi hòa cả khóa phá hòa, sinh nhiều dòng giống nhau (giữ nguyên hành vi này).
WITH ab AS (
    SELECT ba.author_id, bk.*
    FROM {{ source('books', 'book_author') }} AS ba
    JOIN {{ source('books', 'book') }} AS bk ON ba.book_id = bk.book_id
),
sales AS (
    SELECT ab.author_id, ab.title, ol.price
    FROM ab
    JOIN {{ source('books', 'order_line') }} AS ol ON ab.book_id = ol.book_id
),
stats AS (
    SELECT author_id,
        100.0 * count(*) FILTER (WHERE year(publication_date) = 1992) / count(*) AS per_1992,
        sum(num_pages) AS total_pages
    FROM ab
    GROUP BY author_id
),
first_book AS (
    SELECT author_id, title,
        rank() OVER (PARTITION BY author_id ORDER BY publication_date ASC, title ASC) AS rk
    FROM ab
),
expensive AS (
    SELECT author_id, title,
        rank() OVER (PARTITION BY author_id ORDER BY price DESC, title DESC) AS rk
    FROM sales
),
avg_price AS (
    SELECT author_id, avg(price) AS avg_price FROM sales GROUP BY author_id
)
SELECT
    a.author_id,
    a.author_name,
    f.title AS first_book,
    s.per_1992 AS per_book_published_1992,
    CASE WHEN s.total_pages < (SELECT avg(total_pages) FROM stats) THEN 1 ELSE 0 END AS wrote_fewer_total_pages_than_the_average,
    e.title AS most_expensive_book,
    CASE WHEN ap.avg_price > 19 THEN 1 ELSE 0 END AS avg_book_price_over_19
FROM {{ source('books', 'author') }} AS a
LEFT JOIN stats AS s ON a.author_id = s.author_id
LEFT JOIN first_book AS f ON a.author_id = f.author_id AND f.rk = 1
LEFT JOIN expensive AS e ON a.author_id = e.author_id AND e.rk = 1
LEFT JOIN avg_price AS ap ON a.author_id = ap.author_id
