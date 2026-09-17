#!/usr/bin/env python3
"""
app.py — live osu! stats card renderer

Serves a PNG image at:
    /renders/profile-basics?user=<id_or_username>&mode=osu

The image is regenerated from the osu! API at most once per hour per
(user, mode) pair; requests within that hour are served instantly from an
in-memory cache. Embed the URL in your osu! "me!" page like:

    [url=https://YOUR-APP-URL/renders/profile-basics?user=25752151]
    [img]https://YOUR-APP-URL/renders/profile-basics?user=25752151[/img]
    [/url]

Every viewer's browser fetches the image URL directly, so this app needs to
be reachable 24/7 (see DEPLOY.md for hosting steps + a free way to keep it
from sleeping).

Required environment variables:
    OSU_CLIENT_ID       - from https://osu.ppy.sh/home/account/edit (OAuth)
    OSU_CLIENT_SECRET   - same page
"""

import io
import os
import time

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


def render_card(data, mode):
    username = data.get("username", "Unknown")
    avatar_url = data.get("avatar_url")
    rows = build_stats_list(data)

    width = 900
    cols = 3
    padding = 20
    gap = 12
    content_w = width - 2 * padding
    col_w = content_w / cols
    row_h = 90
    header_h = 110
    n_rows = (len(rows) + cols - 1) // cols
    height = header_h + n_rows * row_h + padding

    bg_color = (20, 22, 34)
    card_color = (30, 33, 48)
    accent = (255, 46, 99)
    text_white = (240, 240, 245)
    text_gray = (150, 152, 168)

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    font_title = load_font(True, 26)
    font_mode = load_font(False, 16)
    font_value = load_font(True, 22)
    font_label = load_font(False, 14)
    font_sub = load_font(False, 13)

    avatar_size = 64
    text_x = padding
    if avatar_url:
        try:
            avatar_resp = requests.get(avatar_url, timeout=10)
            avatar_img = Image.open(io.BytesIO(avatar_resp.content)).convert("RGBA")
            avatar_img = ImageOps.fit(avatar_img, (avatar_size, avatar_size))
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
            img.paste(avatar_img, (padding, 22), mask)
            text_x = padding + avatar_size + 16
        except Exception:
            pass

    draw.text((text_x, 30), username, font=font_title, fill=text_white)
    draw.text((text_x, 62), f"osu! {mode} stats", font=font_mode, fill=text_gray)

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
        img = render_card(data, mode)
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
