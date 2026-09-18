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

    return [
        ("Global Rank", f"#{fmt_int(global_rank)}" if global_rank else "Unranked"),
        ("Country Rank", f"#{fmt_int(country_rank)}" if country_rank else "Unranked"),
        ("Performance", f"{stats.get('pp', 0):,.0f}pp"),
        ("Accuracy", f"{stats.get('hit_accuracy', 0):.2f}%"),
        ("Play Count", fmt_int(stats.get("play_count", 0))),
        ("Ranked Score", fmt_int(stats.get("ranked_score", 0))),
        ("SS", fmt_int(grades.get("ss", 0))),
        ("SSH", fmt_int(grades.get("ssh", 0))),
        ("S", fmt_int(grades.get("s", 0))),
        ("SH", fmt_int(grades.get("sh", 0))),
        ("A", fmt_int(grades.get("a", 0))),
    ]


# Daily Challenge tiers: name, participation-days, daily-streak-days,
# weekly-streak-weeks, and the color osu! uses for that tier's badge.
# Source: the in-game Daily Challenge tier table.
DAILY_CHALLENGE_TIERS = [
    ("Lustrous", 1080, 360, 53, (242, 152, 198)),
    ("Radiant", 720, 240, 36, (197, 173, 255)),
    ("Rhodium", 360, 120, 19, (188, 227, 179)),
    ("Platinum", 180, 60, 10, (118, 231, 230)),
    ("Gold", 90, 30, 6, (255, 229, 102)),
    ("Silver", 30, 10, 3, (188, 188, 211)),
    ("Bronze", 15, 5, 2, (154, 113, 92)),
    ("Iron", 0, 0, 0, (186, 179, 171)),
]


def tier_color(value, column):
    """column: 0 = total participation (days), 1 = daily streak (days),
    2 = weekly streak (weeks)."""
    for _name, p, d, w, color in DAILY_CHALLENGE_TIERS:
        threshold = (p, d, w)[column]
        if value >= threshold:
            return color
    return DAILY_CHALLENGE_TIERS[-1][4]


def build_daily_challenge_list(daily):
    participation = daily.get("playcount", 0)
    daily_cur = daily.get("daily_streak_current", 0)
    weekly_cur = daily.get("weekly_streak_current", 0)
    daily_best = daily.get("daily_streak_best", 0)
    weekly_best = daily.get("weekly_streak_best", 0)

    return [
        ("Total Participation", f"{fmt_int(participation)}d", tier_color(participation, 0)),
        ("Current Daily Streak", f"{fmt_int(daily_cur)}d", tier_color(daily_cur, 1)),
        ("Current Weekly Streak", f"{fmt_int(weekly_cur)}w", tier_color(weekly_cur, 2)),
        ("Best Daily Streak", f"{fmt_int(daily_best)}d", tier_color(daily_best, 1)),
        ("Best Weekly Streak", f"{fmt_int(weekly_best)}w", tier_color(weekly_best, 2)),
        ("Top 10% Placements", fmt_int(daily.get("top_10p_placements", 0)), None),
        ("Top 50% Placements", fmt_int(daily.get("top_50p_placements", 0)), None),
    ]


def rank_change_segments(delta, text_gray, green, red):
    if delta is None:
        return [("no change yet today", text_gray)]
    if delta > 0:
        return [(f"\u25bc {delta}", green), (" today", text_gray)]
    if delta < 0:
        return [(f"\u25b2 {abs(delta)}", red), (" today", text_gray)]
    return [("\u2013 unchanged today", text_gray)]


def render_card(data, mode, user):
    username = data.get("username", "Unknown")
    avatar_url = data.get("avatar_url")
    country = data.get("country") or {}
    country_code = country.get("code", "")
    country_name = country.get("name", "")
    stats = data.get("statistics", {}) or {}
    daily = data.get("daily_challenge_user_stats", {}) or {}

    main_items = build_stats_list(data)
    daily_items = build_daily_challenge_list(daily) if daily else []

    global_rank = stats.get("global_rank")
    country_rank = stats.get("country_rank")
    global_delta, country_delta = get_daily_rank_change(user, mode, global_rank, country_rank)

    # ---- palette ----
    bg_color = (0, 0, 0)
    card_fill = (0, 0, 0)
    border_color = (255, 255, 255)
    text_white = (240, 240, 245)
    text_gray = (140, 142, 155)
    green = (80, 220, 130)
    red = (235, 90, 90)

    # ---- layout ----
    width = 900
    padding = 16
    gap = 10
    cols = 3
    content_w = width - 2 * padding
    col_w = content_w / cols
    card_w = col_w - gap
    row_h_short = 62   # plain label+value cards
    row_h_tall = 84    # cards that also carry a sub-line (rank change, etc.)

    header_h = 88
    section_gap = 30  # room for a section title between grids

    def row_heights_for(items):
        heights = []
        for row_start in range(0, len(items), cols):
            row_items = items[row_start:row_start + cols]
            has_sub = any(len(it) > 3 and it[3] for it in row_items)
            heights.append(row_h_tall if has_sub else row_h_short)
        return heights

    # attach rank-change sub-lines to the first two main items
    main_items_with_sub = [
        ("Global Rank", main_items[0][1], None, rank_change_segments(global_delta, text_gray, green, red)),
        ("Country Rank", main_items[1][1], None, rank_change_segments(country_delta, text_gray, green, red)),
    ] + [(label, value) for label, value in main_items[2:]]

    main_heights = row_heights_for(main_items_with_sub)
    daily_heights = row_heights_for(daily_items) if daily_items else []

    height = header_h + sum(main_heights)
    if daily_items:
        height += section_gap + sum(daily_heights)
    height += 34 + padding  # room for the "last updated" footer line

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    font_title = load_font(True, 22)
    font_country = load_font(False, 13)
    font_section = load_font(True, 12)
    font_label = load_font(False, 13)
    font_value = load_font(True, 20)
    font_sub = load_font(False, 12)

    # ---- header: avatar, username, flag + country ----
    avatar_size = 52
    text_x = padding
    if avatar_url:
        try:
            avatar_resp = requests.get(avatar_url, timeout=10)
            avatar_img = Image.open(io.BytesIO(avatar_resp.content)).convert("RGBA")
            avatar_img = ImageOps.fit(avatar_img, (avatar_size, avatar_size))
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
            img.paste(avatar_img, (padding, 16), mask)
            text_x = padding + avatar_size + 14
        except Exception:
            pass

    draw.text((text_x, 16), username, font=font_title, fill=text_white)

    flag_img = fetch_flag_image(country_code)
    flag_x = text_x
    flag_y = 48
    if flag_img:
        img.paste(flag_img, (flag_x, flag_y), flag_img)
        flag_x += flag_img.width + 6
    draw.text((flag_x, flag_y - 2), country_name or country_code, font=font_country, fill=text_gray)

    # ---- generic card-grid drawer (dynamic per-row height) ----
    def draw_grid(items, y0):
        heights = row_heights_for(items)
        y_cursor = y0
        for row_idx, this_row_h in enumerate(heights):
            row_items = items[row_idx * cols:(row_idx + 1) * cols]
            this_card_h = this_row_h - gap
            for col, item in enumerate(row_items):
                label, value = item[0], item[1]
                value_color = item[2] if len(item) > 2 and item[2] else text_white
                sub_segments = item[3] if len(item) > 3 else None

                x = padding + col * col_w
                y = y_cursor
                draw.rounded_rectangle(
                    [x, y, x + card_w, y + this_card_h], radius=8,
                    fill=card_fill, outline=border_color, width=2,
                )
                inner_x = x + 14
                draw.text((inner_x, y + 10), label, font=font_label, fill=text_gray)
                draw.text((inner_x, y + 30), value, font=font_value, fill=value_color)
                if sub_segments:
                    draw_segments(draw, (inner_x, y + 54), sub_segments, font_sub)
            y_cursor += this_row_h
        return y_cursor

    cursor_y = draw_grid(main_items_with_sub, header_h)

    if daily_items:
        cursor_y += 10
        draw.text((padding, cursor_y), "DAILY CHALLENGE", font=font_section, fill=text_gray)
        cursor_y += 20
        cursor_y = draw_grid(daily_items, cursor_y)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    draw.text(
        (padding, cursor_y + 6),
        f"Last updated {timestamp} \u00b7 refreshes hourly",
        font=font_sub,
        fill=text_gray,
    )

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
    force_refresh = request.args.get("refresh") in ("1", "true", "yes")
    cached = None if force_refresh else _image_cache.get(cache_key)

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
