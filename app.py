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

def load_font(size, family="sans", weight="regular"):
    """family: 'sans' or 'serif'. weight: 'regular', 'bold', or 'italic'."""
    names = {
        ("sans", "regular"): "DejaVuSans.ttf",
        ("sans", "bold"): "DejaVuSans-Bold.ttf",
        ("serif", "regular"): "DejaVuSerif.ttf",
        ("serif", "bold"): "DejaVuSerif-Bold.ttf",
        ("serif", "italic"): "DejaVuSerif-Italic.ttf",
    }
    path = os.path.join(FONT_DIR, names.get((family, weight), "DejaVuSans.ttf"))
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

    return [
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


def rank_change_segments(delta, text_gray, green, red):
    if delta is None:
        return [("no change yet", text_gray)]
    if delta > 0:
        return [(f"\u25bc {delta}", green), (" this day", text_gray)]
    if delta < 0:
        return [(f"\u25b2 {abs(delta)}", red), (" this day", text_gray)]
    return [("\u2013 unchanged", text_gray)]


def make_gradient_background(width, height):
    """Dark navy (left) fading to a lighter blue (right), with a soft glow
    in the top-right corner, echoing the reference design."""
    grad_mask = Image.linear_gradient("L").rotate(90, expand=True).resize((width, height))
    left_color = (7, 14, 30)
    right_color = (26, 92, 148)
    solid_left = Image.new("RGB", (width, height), left_color)
    solid_right = Image.new("RGB", (width, height), right_color)
    bg = Image.composite(solid_right, solid_left, grad_mask).convert("RGBA")

    glow_d = int(min(width, height) * 2.2)
    radial = Image.radial_gradient("L").resize((glow_d, glow_d))
    glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_colored = Image.new("RGBA", (glow_d, glow_d), (110, 190, 235, 0))
    glow_colored.putalpha(radial.point(lambda p: int((255 - p) * 0.16)))
    glow_layer.paste(glow_colored, (width - glow_d // 2, -glow_d // 2), glow_colored)
    return Image.alpha_composite(bg, glow_layer)


def render_card_frames(data, mode, user):
    """Builds both animation frames (summary + detail views of the Daily
    Challenge box) as PIL RGBA images of identical size."""
    username = data.get("username", "Unknown")
    avatar_url = data.get("avatar_url")
    country = data.get("country") or {}
    country_code = country.get("code", "")
    country_name = country.get("name", "")
    stats = data.get("statistics", {}) or {}
    daily = data.get("daily_challenge_user_stats", {}) or {}

    main_items = build_stats_list(data)

    global_rank = stats.get("global_rank")
    country_rank = stats.get("country_rank")
    global_delta, country_delta = get_daily_rank_change(user, mode, global_rank, country_rank)

    # ---- fetch avatar + flag once, reused across both frames ----
    avatar_img = None
    if avatar_url:
        try:
            avatar_resp = requests.get(avatar_url, timeout=10)
            avatar_img = Image.open(io.BytesIO(avatar_resp.content)).convert("RGBA")
        except Exception:
            avatar_img = None
    flag_img = fetch_flag_image(country_code)

    # ---- palette ----
    glass_fill = (10, 22, 45, 165)
    border_color = (255, 255, 255)
    text_white = (240, 240, 245)
    text_gray = (185, 190, 205)
    green = (110, 235, 150)
    red = (255, 110, 110)

    # ---- layout ----
    width = 900
    padding = 16
    gap = 12
    cols = 3
    content_w = width - 2 * padding
    col_w = content_w / cols
    card_w = col_w - gap
    row_h_short = 62
    row_h_tall = 84

    left_w = 230
    hero_gap = 12
    hero_x0 = padding + left_w + hero_gap
    hero_area_w = content_w - left_w - hero_gap
    hero_w = (hero_area_w - 2 * hero_gap) / 3
    hero_h = 122
    header_h = 16 + hero_h + 16

    def row_heights_for(items):
        heights = []
        for row_start in range(0, len(items), cols):
            row_items = items[row_start:row_start + cols]
            has_sub = any(len(it) > 3 and it[3] for it in row_items)
            heights.append(row_h_tall if has_sub else row_h_short)
        return heights

    main_heights = row_heights_for(main_items)
    total_height = header_h + sum(main_heights) + 34 + padding

    font_username = load_font(22, "serif", "italic")
    font_country = load_font(13, "sans", "regular")
    font_hero_title = load_font(15, "serif", "italic")
    font_hero_value = load_font(26, "sans", "bold")
    font_hero_summary = load_font(19, "sans", "bold")
    font_hero_sub = load_font(13, "sans", "regular")
    font_daily_row_label = load_font(13, "sans", "regular")
    font_daily_row_value = load_font(14, "sans", "bold")
    font_label = load_font(13, "sans", "regular")
    font_value = load_font(20, "sans", "bold")
    font_sub = load_font(12, "sans", "regular")

    def compose(phase):
        img = make_gradient_background(width, total_height)
        draw = ImageDraw.Draw(img)

        # ---- header: avatar with white ring, username, flag + country ----
        avatar_size = 60
        text_x = padding
        avatar_cy = 16 + hero_h // 2
        if avatar_img:
            fitted = ImageOps.fit(avatar_img, (avatar_size, avatar_size))
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
            ax, ay = padding, avatar_cy - avatar_size // 2
            img.paste(fitted, (ax, ay), mask)
            draw.ellipse([ax - 2, ay - 2, ax + avatar_size + 2, ay + avatar_size + 2],
                         outline=border_color, width=3)
            text_x = padding + avatar_size + 16
        else:
            ax, ay = padding, avatar_cy - avatar_size // 2
            draw.ellipse([ax, ay, ax + avatar_size, ay + avatar_size], fill=(0, 0, 0, 200))
            draw.ellipse([ax - 2, ay - 2, ax + avatar_size + 2, ay + avatar_size + 2],
                         outline=border_color, width=3)
            text_x = padding + avatar_size + 16

        name_y = avatar_cy - 22
        max_name_w = hero_x0 - text_x - 12
        display_name = username
        if draw.textlength(display_name, font=font_username) > max_name_w:
            while display_name and draw.textlength(display_name + "\u2026", font=font_username) > max_name_w:
                display_name = display_name[:-1]
            display_name += "\u2026"
        draw.text((text_x, name_y), display_name, font=font_username, fill=text_white)
        flag_y = name_y + 30
        flag_x = text_x
        if flag_img:
            img.paste(flag_img, (flag_x, flag_y), flag_img)
            flag_x += flag_img.width + 6
        draw.text((flag_x, flag_y - 2), country_name or country_code, font=font_country, fill=text_gray)

        # ---- 3 header hero boxes ----
        def hero_box(index, title):
            x = hero_x0 + index * (hero_w + hero_gap)
            y = 16
            draw.rounded_rectangle([x, y, x + hero_w, y + hero_h], radius=8,
                                    fill=glass_fill, outline=border_color, width=2)
            draw.text((x + 14, y + 12), title, font=font_hero_title, fill=text_white)
            return x + 14, y

        x0, y0 = hero_box(0, "Global Ranking")
        draw.text((x0, y0 + 38), f"#{fmt_int(global_rank)}" if global_rank else "Unranked",
                   font=font_hero_value, fill=text_white)
        draw_segments(draw, (x0, y0 + 76), rank_change_segments(global_delta, text_gray, green, red), font_hero_sub)

        x0, y0 = hero_box(1, "Country Ranking")
        draw.text((x0, y0 + 38), f"#{fmt_int(country_rank)}" if country_rank else "Unranked",
                   font=font_hero_value, fill=text_white)
        draw_segments(draw, (x0, y0 + 76), rank_change_segments(country_delta, text_gray, green, red), font_hero_sub)

        x0, y0 = hero_box(2, "Daily Challenge")
        if not daily:
            draw.text((x0, y0 + 44), "No data", font=font_hero_sub, fill=text_gray)
        elif phase == "summary":
            participation = daily.get("playcount", 0)
            daily_cur = daily.get("daily_streak_current", 0)
            weekly_cur = daily.get("weekly_streak_current", 0)
            segs = [
                (f"{fmt_int(participation)}d", tier_color(participation, 0)), ("  ", text_gray),
                (f"{fmt_int(daily_cur)}d", tier_color(daily_cur, 1)), ("  ", text_gray),
                (f"{fmt_int(weekly_cur)}w", tier_color(weekly_cur, 2)),
            ]
            draw_segments(draw, (x0, y0 + 48), segs, font_hero_summary)
        else:
            rows = [
                ("Best Daily Streak", f"{fmt_int(daily.get('daily_streak_best', 0))}d",
                 tier_color(daily.get("daily_streak_best", 0), 1)),
                ("Best Weekly Streak", f"{fmt_int(daily.get('weekly_streak_best', 0))}w",
                 tier_color(daily.get("weekly_streak_best", 0), 2)),
                ("Top 10% Placements", fmt_int(daily.get("top_10p_placements", 0)), text_white),
                ("Top 50% Placements", fmt_int(daily.get("top_50p_placements", 0)), text_white),
            ]
            ry = y0 + 38
            for label, value, color in rows:
                draw.text((x0, ry), label, font=font_daily_row_label, fill=text_gray)
                value_w = draw.textlength(value, font=font_daily_row_value)
                draw.text((x0 + hero_w - 28 - value_w, ry - 1), value, font=font_daily_row_value, fill=color)
                ry += 18

        # ---- main stats grid ----
        y_cursor = header_h
        for row_idx, this_row_h in enumerate(main_heights):
            row_items = main_items[row_idx * cols:(row_idx + 1) * cols]
            this_card_h = this_row_h - gap
            for col, (label, value) in enumerate(row_items):
                x = padding + col * col_w
                y = y_cursor
                draw.rounded_rectangle([x, y, x + card_w, y + this_card_h], radius=8,
                                        fill=glass_fill, outline=border_color, width=2)
                inner_x = x + 14
                draw.text((inner_x, y + 10), label, font=font_label, fill=text_gray)
                draw.text((inner_x, y + 30), value, font=font_value, fill=text_white)
            y_cursor += this_row_h

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        draw.text((padding, y_cursor + 6), f"Last updated {timestamp} \u00b7 refreshes hourly",
                   font=font_sub, fill=text_gray)

        return img

    return compose("summary"), compose("detail")


def render_card_gif(data, mode, user):
    frame1, frame2 = render_card_frames(data, mode, user)
    p1 = frame1.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256)
    p2 = frame2.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256)
    buf = io.BytesIO()
    p1.save(
        buf, format="GIF", save_all=True, append_images=[p2],
        duration=[5000, 5000], loop=0, disposal=2,
    )
    return buf.getvalue()


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
        image_bytes = cached[1]
    else:
        data = get_user_stats(user, mode)
        image_bytes = render_card_gif(data, mode, user)
        _image_cache[cache_key] = (now + CACHE_TTL_SECONDS, image_bytes)

    return Response(
        image_bytes,
        mimetype="image/gif",
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
