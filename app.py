#!/usr/bin/env python3
import io
import os
import time
from datetime import datetime, timezone

import requests
from flask import Flask, Response, abort, request
from PIL import Image, ImageDraw, ImageFont, ImageOps

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")

OSU_CLIENT_ID = os.environ.get("OSU_CLIENT_ID")
OSU_CLIENT_SECRET = os.environ.get("OSU_CLIENT_SECRET")

OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
OSU_USER_URL = "https://osu.ppy.sh/api/v2/users/{user}/{mode}"

CACHE_TTL_SECONDS = 3600  # 1 hour, matches the "updates once an hour" goal

# ---------------------------------------------------------------------------
# In-memory caches (fine for a single small app instance / personal use)
# ---------------------------------------------------------------------------
_token_cache = {"token": None, "expires_at": 0.0}
_image_cache = {}  # (user, mode) -> (expires_at, png_bytes)

# Tracks the first rank seen "today" per (user, mode), so later renders that
# day can show how much the rank has moved since then. Resets whenever the
# app restarts (e.g. on redeploy) or when a new UTC day begins — there's no
# "change since this morning" field in the osu! API, so this is our own
# lightweight approximation rather than true historical data.
_daily_snapshot_cache = {}  # (user, mode) -> {"date": str, "global_rank": int, "country_rank": int}


# ---------------------------------------------------------------------------
# osu! API
# ---------------------------------------------------------------------------

def get_osu_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    if not OSU_CLIENT_ID or not OSU_CLIENT_SECRET:
        abort(500, "Server is missing OSU_CLIENT_ID / OSU_CLIENT_SECRET env vars.")

    resp = requests.post(
        OSU_TOKEN_URL,
        data={
            "client_id": OSU_CLIENT_ID,
            "client_secret": OSU_CLIENT_SECRET,
            "grant_type": "client_credentials",
            "scope": "public",
        },
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    _token_cache["token"] = payload["access_token"]
    _token_cache["expires_at"] = now + payload.get("expires_in", 3600)
    return _token_cache["token"]


def get_user_stats(user, mode):
    token = get_osu_token()
    url = OSU_USER_URL.format(user=user, mode=mode)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code == 404:
        abort(404, f"osu! user '{user}' not found.")
    resp.raise_for_status()
    return resp.json()


def get_daily_rank_change(user, mode, global_rank, country_rank):
    """Returns (global_delta, country_delta) vs the first rank seen today.

    A positive delta means the rank improved (the number got smaller).
    Returns None for a value the first time it's checked on a given day,
    since there's nothing yet to compare against.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    key = (user, mode)
    snap = _daily_snapshot_cache.get(key)

    if snap is None or snap["date"] != today:
        _daily_snapshot_cache[key] = {
            "date": today,
            "global_rank": global_rank,
            "country_rank": country_rank,
        }
        return None, None

    global_delta = (
        snap["global_rank"] - global_rank
        if snap["global_rank"] is not None and global_rank is not None
        else None
    )
    country_delta = (
        snap["country_rank"] - country_rank
        if snap["country_rank"] is not None and country_rank is not None
        else None
    )
    return global_delta, country_delta


def fetch_flag_image(country_code, size=(24, 16)):
    if not country_code:
        return None
    try:
        url = f"https://flagcdn.com/w80/{country_code.lower()}.png"
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        flag = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        return ImageOps.fit(flag, size)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Image rendering
# ---------------------------------------------------------------------------

def load_font(bold, size):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = os.path.join(FONT_DIR, name)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def fmt_int(n):
    return f"{n:,}"


def draw_segments(draw, xy, segments, font):
    """Draws consecutive (text, color) pairs left-to-right, no gap trimming."""
    x, y = xy
    for text, color in segments:
        draw.text((x, y), text, font=font, fill=color)
        x += draw.textlength(text, font=font)
    return x


def build_stats_list(data):
    stats = data.get("statistics", {}) or {}
    grades = stats.get("grade_counts", {}) or {}

    global_rank = stats.get("global_rank")
    country_rank = stats.get("country_rank")
    country_code = (data.get("country") or {}).get("code", "")

    return [
        ("Global Rank", f"#{fmt_int(global_rank)}" if global_rank else "Unranked", ""),
        ("Country Rank", f"#{fmt_int(country_rank)}" if country_rank else "Unranked", country_code),
        ("Performance", f"{stats.get('pp', 0):,.0f}pp", ""),
        ("Accuracy", f"{stats.get('hit_accuracy', 0):.2f}%", ""),
        ("Play Count", fmt_int(stats.get("play_count", 0)), "plays"),
        ("Ranked Score", fmt_int(stats.get("ranked_score", 0)), ""),
        ("SS Count", fmt_int(grades.get("ssh", 0) + grades.get("ss", 0)), "incl. silver"),
        ("S Count", fmt_int(grades.get("sh", 0) + grades.get("s", 0)), "incl. silver"),
        ("A Count", fmt_int(grades.get("a", 0)), ""),
    ]


def render_card(data, mode, user):
    username = data.get("username", "Unknown")
    avatar_url = data.get("avatar_url")
    country = data.get("country") or {}
    country_code = country.get("code", "")
    country_name = country.get("name", "")
    stats = data.get("statistics", {}) or {}
    daily = data.get("daily_challenge_user_stats", {}) or {}

    rows = build_stats_list(data)

    global_rank = stats.get("global_rank")
    country_rank = stats.get("country_rank")
    global_delta, country_delta = get_daily_rank_change(user, mode, global_rank, country_rank)

    width = 900
    cols = 3
    padding = 20
    gap = 12
    content_w = width - 2 * padding
    col_w = content_w / cols
    row_h = 90
    header_h = 165
    n_rows = (len(rows) + cols - 1) // cols
    height = header_h + n_rows * row_h + padding

    bg_color = (20, 22, 34)
    card_color = (30, 33, 48)
    accent = (255, 46, 99)
    text_white = (240, 240, 245)
    text_gray = (150, 152, 168)
    green = (80, 220, 130)
    red = (235, 90, 90)
    yellow = (255, 205, 90)
    cyan = (110, 210, 235)

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    font_title = load_font(True, 24)
    font_mode = load_font(False, 14)
    font_hero_label = load_font(False, 11)
    font_hero_main = load_font(True, 20)
    font_hero_main_sm = load_font(True, 17)
    font_hero_sub = load_font(False, 13)
    font_value = load_font(True, 22)
    font_label = load_font(False, 14)
    font_sub = load_font(False, 13)

    # ---- header: avatar, username, flag + country (left side) ----
    avatar_size = 56
    text_x = padding
    if avatar_url:
        try:
            avatar_resp = requests.get(avatar_url, timeout=10)
            avatar_img = Image.open(io.BytesIO(avatar_resp.content)).convert("RGBA")
            avatar_img = ImageOps.fit(avatar_img, (avatar_size, avatar_size))
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
            img.paste(avatar_img, (padding, 18), mask)
            text_x = padding + avatar_size + 14
        except Exception:
            pass

    draw.text((text_x, 20), username, font=font_title, fill=text_white)

    flag_img = fetch_flag_image(country_code)
    flag_x = text_x
    if flag_img:
        img.paste(flag_img, (flag_x, 52), flag_img)
        flag_x += flag_img.width + 6
    draw.text((flag_x, 50), country_name or country_code, font=font_mode, fill=text_gray)
    draw.text((text_x, 74), f"osu! {mode} stats", font=font_mode, fill=text_gray)

    # ---- header: 3 hero boxes (right side) ----
    left_w = 250
    hero_gap = 12
    hero_x0 = padding + left_w + hero_gap
    hero_area_w = content_w - left_w - hero_gap
    hero_w = (hero_area_w - 2 * hero_gap) / 3
    hero_y = 14
    hero_h = header_h - hero_y - 14

    def draw_hero_box(index, label, main_text, main_font, sub_segments, main_color=text_white):
        x = hero_x0 + index * (hero_w + hero_gap)
        draw.rounded_rectangle([x, hero_y, x + hero_w, hero_y + hero_h], radius=10, fill=card_color)
        inner_x = x + 14
        draw.text((inner_x, hero_y + 10), label, font=font_hero_label, fill=text_gray)
        draw.text((inner_x, hero_y + 28), main_text, font=main_font, fill=main_color)
        if sub_segments:
            draw_segments(draw, (inner_x, hero_y + 56), sub_segments, font_hero_sub)

    def rank_change_segments(delta):
        if delta is None:
            return [("no change yet today", text_gray)]
        if delta > 0:
            return [(f"\u25bc {delta}", green), (" today", text_gray)]
        if delta < 0:
            return [(f"\u25b2 {abs(delta)}", red), (" today", text_gray)]
        return [("\u2013 unchanged today", text_gray)]

    draw_hero_box(
        0,
        "GLOBAL RANK",
        f"#{fmt_int(global_rank)}" if global_rank else "Unranked",
        font_hero_main,
        rank_change_segments(global_delta),
    )
    draw_hero_box(
        1,
        "COUNTRY RANK",
        f"#{fmt_int(country_rank)}" if country_rank else "Unranked",
        font_hero_main,
        rank_change_segments(country_delta),
    )

    daily_main = (
        f"{fmt_int(daily.get('playcount', 0))}d  "
        f"{fmt_int(daily.get('daily_streak_current', 0))}d  "
        f"{fmt_int(daily.get('weekly_streak_current', 0))}w"
    )
    daily_sub = [
        (f"{fmt_int(daily.get('daily_streak_best', 0))}d", cyan), ("  ", text_gray),
        (f"{fmt_int(daily.get('weekly_streak_best', 0))}w", cyan), ("  ", text_gray),
        (f"{fmt_int(daily.get('top_10p_placements', 0))}", cyan), ("  ", text_gray),
        (f"{fmt_int(daily.get('top_50p_placements', 0))}", cyan),
    ]
    draw_hero_box(
        2,
        "DAILY CHALLENGE",
        daily_main,
        font_hero_main_sm,
        daily_sub if daily else [("no data", text_gray)],
        main_color=yellow,
    )

    card_w = col_w - gap
    card_h = row_h - gap

    for i, (label, value, sub) in enumerate(rows):
        col = i % cols
        row = i // cols
        x = padding + col * col_w
        y = header_h + row * row_h
        draw.rounded_rectangle([x, y, x + card_w, y + card_h], radius=10, fill=card_color)
        inner_x = x + 16
        dot_r = 4
        dot_y = y + card_h - 18
        draw.ellipse([inner_x, dot_y - dot_r, inner_x + dot_r * 2, dot_y + dot_r], fill=accent)
        draw.text((inner_x, y + 12), label, font=font_label, fill=text_gray)
        draw.text((inner_x, y + 32), value, font=font_value, fill=text_white)
        if sub:
            value_w = draw.textlength(value, font=font_value)
            draw.text((inner_x + value_w + 10, y + 38), sub, font=font_sub, fill=text_gray)

    return img


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/renders/profile-basics")
def render_profile_basics():
    user = request.args.get("user") or request.args.get("user_id")
    if not user:
        abort(400, "Missing 'user' query parameter (osu! user id or username).")
    mode = request.args.get("mode", "osu")
    if mode not in ("osu", "taiko", "fruits", "mania"):
        abort(400, "Invalid 'mode'. Use osu, taiko, fruits, or mania.")

    cache_key = (user, mode)
    now = time.time()
    cached = _image_cache.get(cache_key)

    if cached and now < cached[0]:
        png_bytes = cached[1]
    else:
        data = get_user_stats(user, mode)
        img = render_card(data, mode, user)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
        _image_cache[cache_key] = (now + CACHE_TTL_SECONDS, png_bytes)

    return Response(
        png_bytes,
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.route("/")
@app.route("/ping")
def ping():
    # Used both as a health check and as the target for an external
    # "keep-alive" pinger (see DEPLOY.md) so the free host doesn't sleep.
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
