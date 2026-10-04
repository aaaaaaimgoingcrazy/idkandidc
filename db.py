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


SCHEMA = ""

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
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def upsert_beatmaps(rows):
    if not rows:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
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
                (account_key, map_id, completed, score, grade, pp, accuracy),
            )
        conn.commit()
    finally:
        conn.close()


def get_completion_by_year(account_key):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
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
