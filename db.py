"""
db.py — shared Postgres access for the completion-tracking system.

Both app.py (read-only: renders the completion-by-year card) and worker.py
(read/write: crawls beatmaps and checks scores) import this module so the
schema and connection logic live in exactly one place.

Requires DATABASE_URL, e.g.:
    postgresql://user:password@ep-xxxx.aws.neon.tech/dbname?sslmode=require
"""

import os
import json
import time

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set.")
    last_err = None
    for _ in range(3):
        try:
            return psycopg2.connect(DATABASE_URL, connect_timeout=10)
        except psycopg2.OperationalError as e:
            last_err = e
            time.sleep(1.5)
    raise last_err


SCHEMA = """
CREATE TABLE IF NOT EXISTS beatmaps (
    id BIGINT PRIMARY KEY,
    status TEXT NOT NULL,
    year INT NOT NULL,
    title TEXT,
    diff_name TEXT,
    difficulty REAL
);
CREATE INDEX IF NOT EXISTS idx_beatmaps_year ON beatmaps(year);

-- One row per (account, map) once that map has been checked at least once.
-- account_key distinguishes the two tracked accounts, e.g. 'official_osu'
-- and 'private_osurx' — see ACCOUNTS in worker.py.
CREATE TABLE IF NOT EXISTS completions (
    account_key TEXT NOT NULL,
    map_id BIGINT NOT NULL REFERENCES beatmaps(id),
    completed BOOLEAN NOT NULL,
    score BIGINT,
    grade TEXT,
    pp REAL,
    accuracy REAL,
    checked_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account_key, map_id)
);
CREATE INDEX IF NOT EXISTS idx_completions_account ON completions(account_key);

-- Resume cursors for the beatmap-fetch phase only (status, year) — the
-- score-check phase resumes naturally by querying for unchecked map ids,
-- so it doesn't need its own cursor row.
CREATE TABLE IF NOT EXISTS crawl_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""


def init_schema():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_crawl_cursor(key):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM crawl_state WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def set_crawl_cursor(key, value):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO crawl_state (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def upsert_beatmaps(rows):
    """rows: list of (id, status, year, title, diff_name, difficulty)"""
    if not rows:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO beatmaps (id, status, year, title, diff_name, difficulty)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status, year = EXCLUDED.year,
                    title = EXCLUDED.title, diff_name = EXCLUDED.diff_name,
                    difficulty = EXCLUDED.difficulty
                """,
                rows,
            )
        conn.commit()
    finally:
        conn.close()


def get_unchecked_map_ids(account_key, limit=100):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.id FROM beatmaps b
                LEFT JOIN completions c
                    ON c.map_id = b.id AND c.account_key = %s
                WHERE c.map_id IS NULL
                ORDER BY b.id
                LIMIT %s
                """,
                (account_key, limit),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def record_completion(account_key, map_id, completed, score=None, grade=None, pp=None, accuracy=None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO completions (account_key, map_id, completed, score, grade, pp, accuracy, checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (account_key, map_id) DO UPDATE SET
                    completed = EXCLUDED.completed, score = EXCLUDED.score,
                    grade = EXCLUDED.grade, pp = EXCLUDED.pp, accuracy = EXCLUDED.accuracy,
                    checked_at = now()
                """,
                (account_key, map_id, completed, score, grade, pp, accuracy),
            )
        conn.commit()
    finally:
        conn.close()


def get_completion_by_year(account_key):
    """Returns [{"year": int, "completed": int, "total": int, "pct": float}, ...]
    ordered by year. A map only counts as "total" once it's been fetched into
    the beatmaps table; "completed" only counts maps that have actually been
    checked AND found completed — maps not yet checked are simply not counted
    as complete yet (no false positives while the crawl is still running)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.year,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE c.completed) AS completed
                FROM beatmaps b
                LEFT JOIN completions c
                    ON c.map_id = b.id AND c.account_key = %s
                GROUP BY b.year
                ORDER BY b.year
                """,
                (account_key,),
            )
            rows = cur.fetchall()
            return [
                {"year": year, "total": total, "completed": completed,
                 "pct": (completed / total * 100) if total else 0.0}
                for year, total, completed in rows
            ]
    finally:
        conn.close()


def get_crawl_progress(account_key):
    """Returns (checked_count, total_count) across all years, so the card can
    show e.g. 'scanning\u2026 42,381 / 118,204 maps checked' while incomplete."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM beatmaps")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM completions WHERE account_key = %s", (account_key,))
            checked = cur.fetchone()[0]
            return checked, total
    finally:
        conn.close()
