"""
worker.py — completion-tracking background worker

Runs as its own always-on process (a Render "Background Worker" service, NOT
the web service that renders images), so a multi-day crawl never competes
with or blocks image rendering. It does two things, forever:

  1. Beatmap fetch (via Bancho, i.e. the official osu.ppy.sh API): pages
     through /beatmapsets/search for each configured status ("ranked",
     "approved") and year, and upserts every beatmap difficulty it finds
     into the `beatmaps` table. Resumable across restarts via a cursor
     saved in `crawl_state` per (status, year).

  2. Score check, for two independently-tracked accounts:
       - "official_osu"  — your regular osu.ppy.sh account, mode=osu
       - "private_osurx" — your account on the private server, mode=osurx
     For each account, repeatedly asks the database for beatmap ids it
     hasn't checked yet, and calls that account's server to see whether a
     score exists on each one. This resumes naturally on restart — there's
     no separate cursor, it just keeps asking "what's still unchecked?".

There is no player/session concept here (this isn't Roblox) — the closest
equivalent to Luau's `playerGeneration` cancel-on-disconnect check is simply
that this process can be stopped and restarted at any time without losing
progress, since every unit of work is committed to Postgres as it completes.

Required environment variables:
    DATABASE_URL            postgresql://... (Neon, e.g.)

    OSU_CLIENT_ID            Bancho (official) OAuth app — same one app.py uses
    OSU_CLIENT_SECRET

    OFFICIAL_USERNAME        the account to check on Bancho, mode=osu

    PRIVATE_BASE_URL         e.g. https://lazer-api.shikkesora.com
    PRIVATE_CLIENT_ID        OAuth app registered on the private server
    PRIVATE_CLIENT_SECRET
    PRIVATE_USERNAME         the account to check on the private server, mode=osurx

Optional:
    TRACKER_START_YEAR (default 2007)
    TRACKER_END_YEAR   (default 2026)
    SCORE_CHECK_DELAY  (default 1.0 seconds between score-check requests)
"""

import os
import time
import random

import requests

import db

BANCHO_URL = "https://osu.ppy.sh"
OSU_CLIENT_ID = os.environ.get("OSU_CLIENT_ID")
OSU_CLIENT_SECRET = os.environ.get("OSU_CLIENT_SECRET")
OFFICIAL_USERNAME = os.environ.get("OFFICIAL_USERNAME")

PRIVATE_BASE_URL = os.environ.get("PRIVATE_BASE_URL", "").rstrip("/")
PRIVATE_CLIENT_ID = os.environ.get("PRIVATE_CLIENT_ID")
PRIVATE_CLIENT_SECRET = os.environ.get("PRIVATE_CLIENT_SECRET")
PRIVATE_USERNAME = os.environ.get("PRIVATE_USERNAME")

START_YEAR = int(os.environ.get("TRACKER_START_YEAR", 2007))
END_YEAR = int(os.environ.get("TRACKER_END_YEAR", 2026))
STATUSES = ["ranked", "approved"]

SCORE_CHECK_DELAY = float(os.environ.get("SCORE_CHECK_DELAY", 1.0))
MAX_RETRIES = 5

ACCOUNTS = {
    "official_osu": {
        "base_url": BANCHO_URL,
        "client_id": OSU_CLIENT_ID,
        "client_secret": OSU_CLIENT_SECRET,
        "username": OFFICIAL_USERNAME,
        "api_mode": "osu",
    },
    "private_osurx": {
        "base_url": PRIVATE_BASE_URL,
        "client_id": PRIVATE_CLIENT_ID,
        "client_secret": PRIVATE_CLIENT_SECRET,
        "username": PRIVATE_USERNAME,
        "api_mode": "osurx",
    },
}

_token_cache = {}


def get_token(base_url, client_id, client_secret):
    now = time.time()
    cached = _token_cache.get(base_url)
    if cached and now < cached[1] - 60:
        return cached[0]
    resp = requests.post(
        f"{base_url}/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "public",
        },
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    _token_cache[base_url] = (token, now + payload.get("expires_in", 86400))
    return token


def api_get(base_url, token, path, silent_statuses=None):
    """Mirrors the Luau script's osuGet: retries on 429/5xx, treats listed
    statuses (e.g. 404 = 'no score on this map') as an expected empty result
    rather than an error."""
    silent_statuses = silent_statuses or set()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                f"{base_url}/api/v2{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except requests.RequestException as e:
            print(f"[worker] network error on {path}: {e} (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(1.5 * attempt)
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return {}
        if resp.status_code in silent_statuses:
            return {}
        if resp.status_code == 429:
            time.sleep(5)
            continue
        if 500 <= resp.status_code < 600:
            print(f"[worker] transient {resp.status_code} on {path}, retrying ({attempt}/{MAX_RETRIES})")
            time.sleep(1.5 * attempt)
            continue
        print(f"[worker] request failed ({resp.status_code}) on {path}: {resp.text[:200]}")
        return {}
    print(f"[worker] exhausted retries on {path}")
    return {}


def fetch_status_year(token, status, year):
    cursor_key = f"beatmap_fetch:{status}:{year}"
    state = db.get_crawl_cursor(cursor_key) or {"cursor": None, "done": False}
    if state.get("done"):
        return 0

    fetched = 0
    cursor = state.get("cursor")
    while True:
        path = f"/beatmapsets/search?s={status}&sort=id_asc&limit=50&nsfw=true"
        query = f"status={status} ranked={year}"
        path += f"&q={requests.utils.quote(query)}"
        if cursor:
            path += f"&cursor_string={requests.utils.quote(cursor)}"

        data = api_get(BANCHO_URL, token, path)
        beatmapsets = data.get("beatmapsets") or []
        if not beatmapsets:
            db.set_crawl_cursor(cursor_key, {"cursor": None, "done": True})
            break

        rows = []
        for bs in beatmapsets:
            artist = bs.get("artist", "?")
            title = bs.get("title", "?")
            full_title = f"{artist} - {title}"
            for bm in bs.get("beatmaps", []):
                rows.append((
                    bm["id"], status, year, full_title,
                    bm.get("version", "?"), bm.get("difficulty_rating", 0.0),
                ))
        db.upsert_beatmaps(rows)
        fetched += len(rows)

        cursor = data.get("cursor_string")
        db.set_crawl_cursor(cursor_key, {"cursor": cursor, "done": cursor is None})
        if not cursor:
            break
        time.sleep(0.2)

    return fetched


def run_beatmap_fetch_phase():
    if not (OSU_CLIENT_ID and OSU_CLIENT_SECRET):
        print("[worker] OSU_CLIENT_ID/SECRET missing — skipping beatmap fetch phase.")
        return
    token = get_token(BANCHO_URL, OSU_CLIENT_ID, OSU_CLIENT_SECRET)
    print(f"[worker] Beatmap fetch: {STATUSES} \u00d7 {START_YEAR}-{END_YEAR}")
    for year in range(START_YEAR, END_YEAR + 1):
        for status in STATUSES:
            n = fetch_status_year(token, status, year)
            if n:
                print(f"[worker]   {status} {year}: +{n} maps")
    print("[worker] Beatmap fetch phase caught up.")


def check_one_map(account_key, cfg, token, map_id):
    path = f"/beatmaps/{map_id}/scores/users/{cfg['username']}?mode={cfg['api_mode']}"
    data = api_get(cfg["base_url"], token, path, silent_statuses={404})

    score_obj = None
    if isinstance(data, dict):
        score_obj = data.get("score") if isinstance(data.get("score"), dict) else (
            data if data.get("score") is not None else None
        )
    elif isinstance(data, list) and data:
        first = data[0]
        score_obj = first.get("score") if isinstance(first, dict) and isinstance(first.get("score"), dict) else first

    if score_obj and score_obj.get("score"):
        db.record_completion(
            account_key, map_id, True,
            score=score_obj.get("score"),
            grade=score_obj.get("rank"),
            pp=score_obj.get("pp"),
            accuracy=score_obj.get("accuracy"),
        )
    else:
        db.record_completion(account_key, map_id, False)


def run_score_check_round(account_key, batch_size=50):
    cfg = ACCOUNTS[account_key]
    if not (cfg["base_url"] and cfg["client_id"] and cfg["client_secret"] and cfg["username"]):
        return 0

    map_ids = db.get_unchecked_map_ids(account_key, limit=batch_size)
    if not map_ids:
        return 0

    token = get_token(cfg["base_url"], cfg["client_id"], cfg["client_secret"])
    for map_id in map_ids:
        check_one_map(account_key, cfg, token, map_id)
        time.sleep(SCORE_CHECK_DELAY + random.uniform(0, 0.2))
    return len(map_ids)


def main():
    db.init_schema()
    print("[worker] Schema ready.")

    run_beatmap_fetch_phase()

    print("[worker] Entering continuous score-check loop for both accounts.")
    idle_rounds = 0
    while True:
        did_work = False
        for account_key in ACCOUNTS:
            n = run_score_check_round(account_key)
            if n:
                did_work = True
                checked, total = db.get_crawl_progress(account_key)
                print(f"[worker] {account_key}: checked {n} this round \u2014 {checked}/{total} total")

        if not did_work:
            idle_rounds += 1
            sleep_for = min(300 * idle_rounds, 3600)
            print(f"[worker] Nothing to check right now \u2014 sleeping {sleep_for}s.")
            time.sleep(sleep_for)
        else:
            idle_rounds = 0


if __name__ == "__main__":
    main()
