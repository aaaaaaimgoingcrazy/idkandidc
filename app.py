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
_image_cache = {}  # (user, mode) -> (expires_at, image_bytes)


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


def fetch_flag_image(country_code, size=(22, 14)):
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
# Our own lightweight historical tracking
#
# The osu! API only gives real multi-day history for *global* rank
# (`rank_history`, last ~90 days). Everything else below has no official
# history endpoint, so this remembers one snapshot per UTC day per
# (user, mode). "Today's change" compares the current value to today's
# snapshot; the trend graph is built from the run of daily snapshots. Both
# reset if the app restarts, since it's all in memory.
# ---------------------------------------------------------------------------

MAX_LOG_DAYS = 90
LOWER_IS_BETTER = {"global_rank", "country_rank"}

_daily_log = {}  # (user, mode) -> {"YYYY-MM-DD": {field: value, ...}}


def extract_trackable_values(data):
    stats = data.get("statistics", {}) or {}
    grades = stats.get("grade_counts", {}) or {}
    achievements = data.get("user_achievements")
    medals = len(achievements) if isinstance(achievements, list) else None
    return {
        "global_rank": stats.get("global_rank"),
        "country_rank": stats.get("country_rank"),
        "pp": stats.get("pp"),
        "hit_accuracy": stats.get("hit_accuracy"),
        "play_count": stats.get("play_count"),
        "total_score": stats.get("total_score"),
        "ranked_score": stats.get("ranked_score"),
        "total_hits": stats.get("total_hits"),
        "maximum_combo": stats.get("maximum_combo"),
        "replays_watched_by_others": stats.get("replays_watched_by_others"),
        "play_time": stats.get("play_time"),
        "grade_ss": grades.get("ss"),
        "grade_ssh": grades.get("ssh"),
        "grade_s": grades.get("s"),
        "grade_sh": grades.get("sh"),
        "grade_a": grades.get("a"),
        "medals": medals,
    }


def update_daily_log(user, mode, values):
    today = datetime.now(timezone.utc).date().isoformat()
    key = (user, mode)
    log = _daily_log.setdefault(key, {})
    if today not in log:
        log[today] = dict(values)
        while len(log) > MAX_LOG_DAYS:
            del log[next(iter(log))]
    return log


def get_raw_diff(log, field, current_value):
    """current_value minus today's first-seen snapshot. None if unavailable."""
    if current_value is None:
        return None
    today = datetime.now(timezone.utc).date().isoformat()
    baseline = log.get(today, {}).get(field)
    if baseline is None:
        return None
    return current_value - baseline


def get_own_history(log, field):
    return [log[d][field] for d in sorted(log.keys()) if log.get(d, {}).get(field) is not None]


def delta_segments(raw_diff, field, text_gray, green, red, formatter=None):
    if raw_diff is None:
        return [("no change yet", text_gray)]
    if raw_diff == 0:
        return [("\u2013 unchanged", text_gray)]
    is_improvement = (raw_diff < 0) if field in LOWER_IS_BETTER else (raw_diff > 0)
    arrow = "\u25b2" if raw_diff > 0 else "\u25bc"
    color = green if is_improvement else red
    mag = formatter(abs(raw_diff)) if formatter else fmt_int(abs(raw_diff))
    return [(f"{arrow} {mag}", color), (" this day", text_gray)]


# ---------------------------------------------------------------------------
# Image rendering helpers
# ---------------------------------------------------------------------------

def load_font(size, family="sans", weight="regular"):
    """family: 'sans' or 'serif'. weight: 'regular', 'bold', or 'italic'."""
    names = {
        ("sans", "regular"): "DejaVuSans.ttf",
        ("sans", "bold"): "DejaVuSans-Bold.ttf",
        ("sans", "italic"): "DejaVuSans-Oblique.ttf",
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
    return f"{n:,.0f}"


def fmt_playtime(seconds):
    if not seconds:
        return "0m"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def draw_segments(draw, xy, segments, font):
    """Draws consecutive (text, color) pairs left-to-right."""
    x, y = xy
    for text, color in segments:
        draw.text((x, y), text, font=font, fill=color)
        x += draw.textlength(text, font=font)
    return x


def draw_labeled_rows(draw, x0, y0, w, rows, label_font, value_font, text_gray, right_pad=16):
    """rows: list of (label, value, color). Label left, value right-aligned."""
    ry = y0
    for label, value, color in rows:
        draw.text((x0, ry), label, font=label_font, fill=text_gray)
        value_w = draw.textlength(value, font=value_font)
        draw.text((x0 + w - right_pad - value_w, ry - 1), value, font=value_font, fill=color)
        ry += 17


def draw_line_graph(img, draw, box_x, box_y, box_w, box_h, history_data, line_color,
                     lower_is_better=False, top_pad=26):
    """Draws a small shaded trend line inside the given box. Returns False
    (drawing nothing) if there isn't enough history yet to plot."""
    if not history_data or len(history_data) < 2:
        return False

    gx0, gy0 = box_x + 8, box_y + top_pad
    gw, gh = box_w - 16, box_h - top_pad - 8
    gy1 = gy0 + gh

    min_val, max_val = min(history_data), max(history_data)
    n = len(history_data)
    points = []
    for i, val in enumerate(history_data):
        px = gx0 + (i / (n - 1)) * gw
        if max_val == min_val:
            py = gy0 + gh / 2
        else:
            norm = (val - min_val) / (max_val - min_val) if lower_is_better else (max_val - val) / (max_val - min_val)
            py = gy0 + norm * gh
        points.append((px, py))

    fill_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fill_draw = ImageDraw.Draw(fill_layer)
    poly = [(points[0][0], gy1)] + points + [(points[-1][0], gy1)]
    fill_rgba = (line_color[0], line_color[1], line_color[2], 45)
    fill_draw.polygon(poly, fill=fill_rgba)

    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [box_x, box_y, box_x + box_w, box_y + box_h], radius=8, fill=255
    )
    fill_layer.putalpha(Image.composite(fill_layer.getchannel("A"), Image.new("L", img.size, 0), mask))
    img.alpha_composite(fill_layer)

    draw.line(points, fill=line_color, width=2)
    lx, ly = points[-1]
    r = 3
    draw.ellipse([lx - r, ly - r, lx + r, ly + r], fill=line_color, outline=(255, 255, 255), width=1)
    return True


def make_translucent_black(width, height, alpha=77):
    """Flat black canvas at ~30% opacity (77/255), so it blends with
    whatever real background sits behind it once embedded. This only works
    because the card is exported as an animated PNG (APNG) — GIF
    transparency is strictly on/off per pixel and can't do partial alpha."""
    return Image.new("RGBA", (width, height), (0, 0, 0, alpha))


# Daily Challenge tiers: name, participation-days, daily-streak-days,
# weekly-streak-weeks, and the color osu! uses for that tier's badge.
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
        if value >= (p, d, w)[column]:
            return color
    return DAILY_CHALLENGE_TIERS[-1][4]


# Same tier color ramp, applied to rank percentile instead of Daily Challenge
# progress, so a strong rank pops with color instead of sitting in plain
# white. Player-count estimates are rough — they only need to put someone in
# the right neighborhood of a tier, not be exact.
ESTIMATED_GLOBAL_PLAYERS = 2_000_000
ESTIMATED_COUNTRY_PLAYERS = 50_000


def get_rank_tier_color(rank, estimated_pool):
    if not rank:
        return (240, 240, 245)
    if rank <= 100:
        return (242, 152, 198)  # Lustrous
    percent = rank / estimated_pool
    for threshold, color in (
        (0.0005, (197, 173, 255)),  # Radiant
        (0.0015, (188, 227, 179)),  # Rhodium
        (0.005, (118, 231, 230)),   # Platinum
        (0.015, (255, 229, 102)),   # Gold
        (0.05, (188, 188, 211)),    # Silver
        (0.15, (154, 113, 92)),     # Bronze
        (0.5, (186, 179, 171)),     # Iron
    ):
        if percent < threshold:
            return color
    return (240, 240, 245)


def render_card_frames(data, mode, user):
    """Builds both animation frames (summary + detail views) as PIL RGBA
    images of identical size."""
    username = data.get("username", "Unknown")
    avatar_url = data.get("avatar_url")
    country = data.get("country") or {}
    country_code = country.get("code", "")
    country_name = country.get("name", "")
    stats = data.get("statistics", {}) or {}
    grades = stats.get("grade_counts", {}) or {}
    daily = data.get("daily_challenge_user_stats", {}) or {}

    global_rank = stats.get("global_rank")
    country_rank = stats.get("country_rank")

    # ---- our own day-over-day tracking (real, self-collected) ----
    current_values = extract_trackable_values(data)
    log = update_daily_log(user, mode, current_values)

    def diff_for(field):
        return get_raw_diff(log, field, current_values.get(field))

    def history_for(field):
        return get_own_history(log, field)

    # Global rank gets the official 90-day history when the API provides it;
    # everything else (including country rank) falls back to our own log.
    api_rank_history = data.get("rank_history") or {}
    global_history = api_rank_history.get("data") or history_for("global_rank")
    country_history = history_for("country_rank")

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
    glass_fill = (0, 0, 0, 77)  # rgba(0,0,0,0.3) — same value for bg and card fills
    border_color = (255, 255, 255)
    text_white = (240, 240, 245)
    text_gray = (185, 190, 205)
    green = (110, 235, 150)
    red = (255, 110, 110)

    # ---- fonts ----
    font_username = load_font(22, "serif", "italic")
    font_country = load_font(13, "sans", "regular")
    font_hero_title = load_font(14, "serif", "italic")
    font_hero_value = load_font(24, "sans", "bold")
    font_hero_sub = load_font(12, "sans", "regular")
    font_row_label = load_font(12, "sans", "regular")
    font_row_value = load_font(13, "sans", "bold")
    font_box_label = load_font(13, "sans", "bold")
    font_box_value = load_font(19, "sans", "bold")
    font_box_value_sm = load_font(15, "sans", "bold")
    font_box_delta = load_font(12, "sans", "bold")
    font_box_delta_day = load_font(11, "sans", "italic")
    font_grade_letter = load_font(20, "sans", "bold")
    font_grade_count = load_font(14, "sans", "bold")
    font_section_title = load_font(15, "serif", "bold")
    font_sub = load_font(12, "sans", "regular")
    font_bar_pct = load_font(10, "sans", "bold")

    # ---- layout ----
    width = 900
    padding = 14
    gap = 10
    cols = 3
    content_w = width - 2 * padding
    col_w = content_w / cols
    card_w = col_w - gap
    row_h = 78
    card_h = row_h - gap

    avatar_size = 54
    name_w = font_username.getlength(username)
    flag_w = 24 + 6 if (flag_img or country_code) else 0
    country_w = flag_w + font_country.getlength(country_name or country_code)
    left_content_w = max(name_w, country_w)
    left_w = min(max(avatar_size + 14 + left_content_w + 10, 140), 250)

    hero_gap = 10
    hero_x0 = padding + left_w + hero_gap
    hero_area_w = content_w - left_w - hero_gap
    hero_w = (hero_area_w - 2 * hero_gap) / 3
    hero_h = 100
    header_h = 12 + hero_h + 12

    grades_title_h = 22
    grade_gap = 8
    grade_w = (content_w - 4 * grade_gap) / 5
    grade_h = 56

    row_h_wide = row_h + 16  # Total Hits row needs extra room for the breakdown bar

    total_height = int(
        header_h
        + row_h * 3          # Perf/Acc/PlayCount, TotalScore/RankedScore/MaxCombo, Replays/Medals/Playtime
        + row_h_wide          # Total Hits row
        + 10 + grades_title_h + grade_h
        + 12 + 18 + padding  # footer
    )

    def compose(phase):
        img = make_translucent_black(width, total_height)
        draw = ImageDraw.Draw(img)

        # ---- header: avatar with white ring, username, flag + country ----
        avatar_cy = 12 + hero_h // 2
        ax, ay = padding, avatar_cy - avatar_size // 2
        if avatar_img:
            fitted = ImageOps.fit(avatar_img, (avatar_size, avatar_size))
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
            img.paste(fitted, (ax, ay), mask)
        else:
            draw.ellipse([ax, ay, ax + avatar_size, ay + avatar_size], fill=(0, 0, 0, 200))
        draw.ellipse([ax - 2, ay - 2, ax + avatar_size + 2, ay + avatar_size + 2],
                     outline=border_color, width=3)
        text_x = padding + avatar_size + 14

        name_y = avatar_cy - 20
        max_name_w = hero_x0 - text_x - 10
        display_name = username
        if draw.textlength(display_name, font=font_username) > max_name_w:
            while display_name and draw.textlength(display_name + "\u2026", font=font_username) > max_name_w:
                display_name = display_name[:-1]
            display_name += "\u2026"
        draw.text((text_x, name_y), display_name, font=font_username, fill=text_white)
        flag_y = name_y + 27
        flag_x = text_x
        if flag_img:
            img.paste(flag_img, (flag_x, flag_y), flag_img)
            flag_x += flag_img.width + 6
        draw.text((flag_x, flag_y - 2), country_name or country_code, font=font_country, fill=text_gray)

        # ---- 3 header hero boxes: Global / Country Ranking + Daily Challenge ----
        def hero_box(index, title):
            x = hero_x0 + index * (hero_w + hero_gap)
            y = 12
            draw.rounded_rectangle([x, y, x + hero_w, y + hero_h], radius=8,
                                    fill=glass_fill, outline=border_color, width=2)
            draw.text((x + 12, y + 8), title, font=font_hero_title, fill=text_white)
            return x, y

        def rank_hero(index, title, rank_value, field, history, estimated_pool):
            x0, y0 = hero_box(index, title)
            inner_x = x0 + 12
            rank_color = get_rank_tier_color(rank_value, estimated_pool)
            if phase == "summary":
                draw.text((inner_x, y0 + 26), f"#{fmt_int(rank_value)}" if rank_value else "Unranked",
                           font=font_hero_value, fill=rank_color)
                draw_segments(draw, (inner_x, y0 + 56),
                              delta_segments(diff_for(field), field, text_gray, green, red),
                              font_hero_sub)
            else:
                drew = draw_line_graph(img, draw, x0, y0, hero_w, hero_h, history,
                                        rank_color, lower_is_better=True, top_pad=28)
                if drew:
                    rank_str = f"#{fmt_int(rank_value)}" if rank_value else "Unranked"
                    rw = draw.textlength(rank_str, font=font_row_value)
                    draw.text((x0 + hero_w - 12 - rw, y0 + 8), rank_str, font=font_row_value, fill=rank_color)
                else:
                    draw.text((inner_x, y0 + 34), "Collecting trend data\u2026",
                               font=font_hero_sub, fill=text_gray)

        rank_hero(0, "Global Ranking", global_rank, "global_rank", global_history, ESTIMATED_GLOBAL_PLAYERS)
        rank_hero(1, "Country Ranking", country_rank, "country_rank", country_history, ESTIMATED_COUNTRY_PLAYERS)

        x0, y0 = hero_box(2, "Daily Challenge")
        if not daily:
            draw.text((x0 + 12, y0 + 30), "No data", font=font_hero_sub, fill=text_gray)
        elif phase == "summary":
            rows = [
                ("Total Participation", f"{fmt_int(daily.get('playcount', 0))}d",
                 tier_color(daily.get("playcount", 0), 0)),
                ("Current Daily Streak", f"{fmt_int(daily.get('daily_streak_current', 0))}d",
                 tier_color(daily.get("daily_streak_current", 0), 1)),
                ("Current Weekly Streak", f"{fmt_int(daily.get('weekly_streak_current', 0))}w",
                 tier_color(daily.get("weekly_streak_current", 0), 2)),
            ]
            draw_labeled_rows(draw, x0 + 12, y0 + 26, hero_w, rows, font_row_label, font_row_value, text_gray)
        else:
            rows = [
                ("Best Daily Streak", f"{fmt_int(daily.get('daily_streak_best', 0))}d",
                 tier_color(daily.get("daily_streak_best", 0), 1)),
                ("Best Weekly Streak", f"{fmt_int(daily.get('weekly_streak_best', 0))}w",
                 tier_color(daily.get("weekly_streak_best", 0), 2)),
                ("Top 10% Placements", fmt_int(daily.get("top_10p_placements", 0)), text_white),
                ("Top 50% Placements", fmt_int(daily.get("top_50p_placements", 0)), text_white),
            ]
            draw_labeled_rows(draw, x0 + 12, y0 + 26, hero_w, rows, font_row_label, font_row_value, text_gray)

        # ---- generic stat box (summary: value+delta / detail: graph+delta) ----
        def stat_box(x, y, w, h, label, value_str, field, formatter=None):
            draw.rounded_rectangle([x, y, x + w, y + h], radius=8,
                                    fill=glass_fill, outline=border_color, width=2)
            inner_x = x + 12
            raw_diff = diff_for(field)

            if phase == "summary":
                draw.text((inner_x, y + 8), label, font=font_box_label, fill=text_gray)
                val_font = font_box_value if draw.textlength(value_str, font=font_box_value) <= (w - 24) else font_box_value_sm
                draw.text((inner_x, y + 26), value_str, font=val_font, fill=text_white)
                segs = delta_segments(raw_diff, field, text_gray, green, red, formatter)
                draw_segments(draw, (inner_x, y + 52), segs, font_box_delta)
            else:
                drew = draw_line_graph(img, draw, x, y, w, h, history_for(field), text_white,
                                        lower_is_better=(field in LOWER_IS_BETTER), top_pad=24)
                draw.text((inner_x, y + 6), label, font=font_box_label, fill=text_white)
                segs = delta_segments(raw_diff, field, text_gray, green, red, formatter)
                seg_w = sum(draw.textlength(t, font=font_box_delta if i == 0 else font_box_delta_day)
                            for i, (t, _c) in enumerate(segs))
                draw_segments(draw, (x + w - 10 - seg_w, y + 7), segs, font_box_delta)
                if not drew:
                    draw.text((inner_x, y + h / 2 - 4), "Collecting trend data\u2026",
                               font=font_hero_sub, fill=text_gray)

        pp = stats.get("pp", 0)
        acc = stats.get("hit_accuracy", 0)
        play_count = stats.get("play_count", 0)
        total_score = stats.get("total_score", 0)
        ranked_score = stats.get("ranked_score", 0)
        max_combo = stats.get("maximum_combo", 0)
        total_hits = stats.get("total_hits", 0)
        count_300 = stats.get("count_300", 0)
        count_100 = stats.get("count_100", 0)
        count_50 = stats.get("count_50", 0)
        replays_watched = stats.get("replays_watched_by_others", 0)
        play_time_sec = stats.get("play_time", 0)
        achievements = data.get("user_achievements")
        medals_count = len(achievements) if isinstance(achievements, list) else None

        fmt_pp = lambda v: f"{v:,.0f}pp"
        fmt_acc = lambda v: f"{v:.2f}%"
        fmt_pt = lambda v: fmt_playtime(v)

        y_cursor = header_h
        col_x = [padding + i * col_w for i in range(cols)]

        stat_box(col_x[0], y_cursor, card_w, card_h, "Performance", f"{pp:,.0f}pp", "pp", fmt_pp)
        stat_box(col_x[1], y_cursor, card_w, card_h, "Accuracy", f"{acc:.2f}%", "hit_accuracy", fmt_acc)
        stat_box(col_x[2], y_cursor, card_w, card_h, "Play Count", fmt_int(play_count), "play_count")
        y_cursor += row_h

        stat_box(col_x[0], y_cursor, card_w, card_h, "Total Score", fmt_int(total_score), "total_score")
        stat_box(col_x[1], y_cursor, card_w, card_h, "Ranked Score", fmt_int(ranked_score), "ranked_score")
        stat_box(col_x[2], y_cursor, card_w, card_h, "Maximum Combo", fmt_int(max_combo), "maximum_combo")
        y_cursor += row_h

        # ---- Total Hits: full-width row, own layout per phase ----
        card_h_wide = row_h_wide - gap
        th_x, th_w = col_x[0], content_w
        draw.rounded_rectangle([th_x, y_cursor, th_x + th_w, y_cursor + card_h_wide], radius=8,
                                fill=glass_fill, outline=border_color, width=2)
        if phase == "summary":
            tot_for_bar = count_300 + count_100 + count_50
            bar_y0 = y_cursor + card_h_wide - 10
            bar_y1 = y_cursor + card_h_wide
            if tot_for_bar > 0:
                bar_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
                bar_draw = ImageDraw.Draw(bar_layer)
                w300 = th_w * (count_300 / tot_for_bar)
                w100 = th_w * (count_100 / tot_for_bar)
                bar_draw.rectangle([th_x, bar_y0, th_x + w300, bar_y1], fill=(0, 255, 255, 255))
                bar_draw.rectangle([th_x + w300, bar_y0, th_x + w300 + w100, bar_y1], fill=(0, 255, 0, 255))
                bar_draw.rectangle([th_x + w300 + w100, bar_y0, th_x + th_w, bar_y1], fill=(255, 165, 0, 255))
                mask = Image.new("L", img.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    [th_x, y_cursor, th_x + th_w, y_cursor + card_h_wide], radius=8, fill=255)
                bar_layer.putalpha(Image.composite(bar_layer.getchannel("A"), Image.new("L", img.size, 0), mask))
                img.alpha_composite(bar_layer)

            sub_w = th_w / 4
            headers = ["Total Hits", "300s", "100s", "50s"]
            vals = [fmt_int(total_hits), fmt_int(count_300), fmt_int(count_100), fmt_int(count_50)]
            fields = ["total_hits", None, None, None]
            for i in range(4):
                cx = th_x + i * sub_w + 12
                draw.text((cx, y_cursor + 8), headers[i], font=font_box_label, fill=text_gray)
                draw.text((cx, y_cursor + 26), vals[i], font=font_box_value_sm, fill=text_white)
                if fields[i]:
                    segs = delta_segments(diff_for(fields[i]), fields[i], text_gray, green, red)
                    draw_segments(draw, (cx, y_cursor + 44), segs, font_box_delta)
        else:
            drew = draw_line_graph(img, draw, th_x, y_cursor, th_w, card_h_wide, history_for("total_hits"),
                                    text_white, lower_is_better=False, top_pad=24)
            draw.text((th_x + 12, y_cursor + 6), "Total Hits", font=font_box_label, fill=text_white)
            segs = delta_segments(diff_for("total_hits"), "total_hits", text_gray, green, red)
            seg_w = sum(draw.textlength(t, font=font_box_delta if i == 0 else font_box_delta_day)
                        for i, (t, _c) in enumerate(segs))
            draw_segments(draw, (th_x + th_w - 10 - seg_w, y_cursor + 7), segs, font_box_delta)
            if not drew:
                draw.text((th_x + 12, y_cursor + card_h_wide / 2 - 4), "Collecting trend data\u2026",
                           font=font_hero_sub, fill=text_gray)
        y_cursor += row_h_wide

        stat_box(col_x[0], y_cursor, card_w, card_h, "Replays Watched", fmt_int(replays_watched), "replays_watched_by_others")
        stat_box(col_x[1], y_cursor, card_w, card_h, "Medals",
                 fmt_int(medals_count) if medals_count is not None else "\u2014", "medals")
        stat_box(col_x[2], y_cursor, card_w, card_h, "Total Play Time", fmt_playtime(play_time_sec), "play_time", fmt_pt)
        y_cursor += row_h + 10

        # ---- Grades: SS / SSH / S / SH / A (the only 5 grades the API tracks) ----
        draw.text((padding, y_cursor), "GRADES", font=font_section_title, fill=text_white)
        gy = y_cursor + grades_title_h
        grade_config = [
            ("SS", grades.get("ss", 0), (255, 255, 0), "grade_ss"),
            ("SSH", grades.get("ssh", 0), (255, 0, 255), "grade_ssh"),
            ("S", grades.get("s", 0), (255, 255, 0), "grade_s"),
            ("SH", grades.get("sh", 0), (0, 255, 255), "grade_sh"),
            ("A", grades.get("a", 0), (0, 255, 0), "grade_a"),
        ]
        for i, (label, count, color, field) in enumerate(grade_config):
            gx = padding + i * (grade_w + grade_gap)
            draw.rounded_rectangle([gx, gy, gx + grade_w, gy + grade_h], radius=6,
                                    fill=glass_fill, outline=border_color, width=2)
            draw.text((gx + 10, gy + 8), label, font=font_grade_letter, fill=color)
            count_str = fmt_int(count)
            cw = draw.textlength(count_str, font=font_grade_count)
            draw.text((gx + grade_w - 10 - cw, gy + 10), count_str, font=font_grade_count, fill=text_white)
            segs = delta_segments(diff_for(field), field, text_gray, green, red)
            seg_w = sum(draw.textlength(t, font=font_box_delta_day) for t, _c in segs)
            draw.text((gx + grade_w - 10 - seg_w, gy + 34), "".join(t for t, _c in segs), font=font_box_delta_day, fill=text_gray)

        y_cursor = gy + grade_h + 12

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        draw.text((padding, y_cursor), f"Last updated {timestamp} \u00b7 refreshes hourly",
                   font=font_sub, fill=text_gray)

        return img

    return compose("summary"), compose("detail")


def render_card_apng(data, mode, user):
    frame1, frame2 = render_card_frames(data, mode, user)
    buf = io.BytesIO()
    frame1.save(
        buf, format="PNG", save_all=True, append_images=[frame2],
        duration=[5000, 5000], loop=0,
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
        image_bytes = render_card_apng(data, mode, user)
        _image_cache[cache_key] = (now + CACHE_TTL_SECONDS, image_bytes)

    return Response(
        image_bytes,
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
