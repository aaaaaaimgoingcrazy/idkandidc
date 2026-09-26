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

CACHE_TTL_SECONDS = 3600

_token_cache = {"token": None, "expires_at": 0.0}
_image_cache = {}

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

MAX_LOG_DAYS = 90
LOWER_IS_BETTER = {"global_rank", "country_rank"}

_daily_log = {}


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
        "hits_per_play": (stats.get("total_hits", 0) / stats.get("play_count")) if stats.get("play_count") else None,
        "grade_ss": grades.get("ss"),
        "grade_ssh": grades.get("ssh"),
        "grade_s": grades.get("s"),
        "grade_sh": grades.get("sh"),
        "grade_a": grades.get("a"),
        "grade_b": grades.get("b"),
        "grade_c": grades.get("c"),
        "grade_d": grades.get("d"),
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
        return [("new", text_gray)]
    if raw_diff == 0:
        return [("\u2013", text_gray)]
    is_improvement = (raw_diff < 0) if field in LOWER_IS_BETTER else (raw_diff > 0)
    arrow = "\u25b2" if raw_diff > 0 else "\u25bc"
    color = green if is_improvement else red
    mag = formatter(abs(raw_diff)) if formatter else fmt_int(abs(raw_diff))
    return [(f"{arrow} {mag}", color), (" this day", text_gray)]

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
                     lower_is_better=False):
    """Draws a small shaded trend line inside the given box. Returns False
    (drawing nothing) if there isn't enough history yet to plot."""
    if not history_data or len(history_data) < 2:
        return False

    gx0, gy0 = box_x + 6, box_y + 24
    gw, gh = box_w - 12, box_h - 30
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
        [box_x, box_y, box_x + box_w, box_y + box_h], radius=6, fill=255
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

ESTIMATED_GLOBAL_PLAYERS = 2_000_000
ESTIMATED_COUNTRY_PLAYERS = 50_000


def get_rank_tier_color(rank, estimated_pool):
    if not rank:
        return (240, 240, 245)
    if rank <= 100:
        return (242, 152, 198)
    percent = rank / estimated_pool
    for threshold, color in (
        (0.0005, (197, 173, 255)),
        (0.0015, (188, 227, 179)),
        (0.005, (118, 231, 230)),
        (0.015, (255, 229, 102)),
        (0.05, (188, 188, 211)),
        (0.15, (154, 113, 92)),
        (0.5, (186, 179, 171)),
    ):
        if percent < threshold:
            return color
    return (240, 240, 245)


def render_card_frames(data, mode, user):
    """Builds both animation frames (summary + detail views) as PIL RGBA
    images of identical size. Layout, fonts, and spacing are ported directly
    from the reference design; only the data underneath is real (see the
    tracking helpers above) instead of the reference's dummy placeholders."""
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

    current_values = extract_trackable_values(data)
    log = update_daily_log(user, mode, current_values)

    def diff_for(field):
        return get_raw_diff(log, field, current_values.get(field))

    def history_for(field):
        return get_own_history(log, field)

    api_rank_history = data.get("rank_history") or {}
    global_history = api_rank_history.get("data") or history_for("global_rank")
    country_history = history_for("country_rank")

    pp = stats.get("pp", 0)
    acc = stats.get("hit_accuracy", 0)
    play_count = stats.get("play_count", 0)
    total_score = stats.get("total_score", 0)
    ranked_score = stats.get("ranked_score", 0)
    total_hits = stats.get("total_hits", 0)
    count_300 = stats.get("count_300", 0)
    count_100 = stats.get("count_100", 0)
    count_50 = stats.get("count_50", 0)
    hits_per_play = (total_hits / play_count) if play_count else 0
    max_combo = stats.get("maximum_combo", 0)
    replays_watched = stats.get("replays_watched_by_others", 0)
    achievements = data.get("user_achievements")
    medals_count = len(achievements) if isinstance(achievements, list) else None
    play_time_sec = stats.get("play_time", 0)

    avatar_img = None
    if avatar_url:
        try:
            avatar_resp = requests.get(avatar_url, timeout=10)
            avatar_img = Image.open(io.BytesIO(avatar_resp.content)).convert("RGBA")
        except Exception:
            avatar_img = None
    flag_img = fetch_flag_image(country_code)

    glass_fill = (0, 0, 0, 77)
    border_color = (255, 255, 255)
    text_white = (240, 240, 245)
    text_gray = (185, 190, 205)
    green = (110, 235, 150)
    red = (255, 110, 110)

    width = 780
    padding = 14
    gap = 8
    cols = 4
    content_w = width - 2 * padding
    col_w = int((content_w - (cols - 1) * gap) // cols)

    def get_col_x(c):
        return int(padding + c * (col_w + gap))

    hero_h = 84
    header_h = padding + hero_h + 10
    core_row_h = 68
    grades_title_h = 24
    grades_box_h = 60

    total_height = int(header_h + (core_row_h * 3 + gap * 2) + 10 + grades_title_h + grades_box_h + 12 + 14 + padding)

    font_username = load_font(19, "serif", "italic")
    font_country = load_font(12, "sans", "regular")
    font_hero_title = load_font(14, "serif", "italic")
    font_hero_value = load_font(24, "sans", "bold")
    font_hero_sub = load_font(12, "sans", "regular")
    font_daily_row_label = load_font(11, "sans", "regular")
    font_daily_row_value = load_font(12, "sans", "bold")

    font_core_label = load_font(12, "sans", "bold")
    font_core_delta = load_font(11, "sans", "bold")
    font_core_delta_day = load_font(10, "sans", "italic")

    font_core_value = load_font(18, "sans", "bold")
    font_core_value_small = load_font(14, "sans", "bold")

    font_th_value = load_font(14, "sans", "regular")
    font_th_delta = load_font(10, "sans", "bold")
    font_th_delta_day = load_font(9, "sans", "italic")
    font_bar_pct = load_font(10, "sans", "bold")

    font_sub = load_font(11, "sans", "regular")
    font_section_title = load_font(18, "serif", "bold")

    font_grade_letter = load_font(28, "sans", "bold")
    font_grade_count = load_font(13, "sans", "regular")
    font_grade_change_val = load_font(14, "sans", "bold")
    font_grade_change_text = load_font(11, "sans", "italic")

    def draw_dual_font_left(draw, x, y, segs, font_num, font_day):
        if not segs:
            return
        num_text, num_color = segs[0]
        draw.text((x, y), num_text, font=font_num, fill=num_color)
        if len(segs) > 1:
            dw = draw.textlength(num_text, font=font_num)
            draw.text((x + dw, y), segs[1][0], font=font_day, fill=segs[1][1])

    def dual_font_width(draw, segs, font_num, font_day):
        if not segs:
            return 0
        w = draw.textlength(segs[0][0], font=font_num)
        if len(segs) > 1:
            w += draw.textlength(segs[1][0], font=font_day)
        return w

    def compose(phase):
        img = make_translucent_black(width, total_height)
        draw = ImageDraw.Draw(img)

        col0_x = get_col_x(0)
        avatar_size = 52
        avatar_cy = padding + hero_h // 2
        ax = int(col0_x + 2)
        ay = int(avatar_cy - avatar_size // 2)

        if avatar_img:
            fitted = ImageOps.fit(avatar_img, (avatar_size, avatar_size))
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
            img.paste(fitted, (ax, ay), mask)
        else:
            draw.ellipse([ax, ay, ax + avatar_size, ay + avatar_size], fill=(0, 0, 0, 200))
        draw.ellipse([ax - 2, ay - 2, ax + avatar_size + 2, ay + avatar_size + 2],
                     outline=border_color, width=2)

        text_x = int(ax + avatar_size + 10)
        name_y = int(avatar_cy - 18)
        max_name_w = (col0_x + col_w) - text_x - 4
        display_name = username
        if draw.textlength(display_name, font=font_username) > max_name_w:
            while display_name and draw.textlength(display_name + "\u2026", font=font_username) > max_name_w:
                display_name = display_name[:-1]
            display_name += "\u2026"
        draw.text((text_x, name_y), display_name, font=font_username, fill=text_white)

        flag_y = int(avatar_cy + 6)
        flag_x = int(text_x)
        if flag_img:
            img.paste(flag_img, (flag_x, flag_y), flag_img)
            flag_x += int(flag_img.width + 6)
        draw.text((flag_x, flag_y - 2), country_name or country_code, font=font_country, fill=text_gray)

        def hero_box(col_index, title):
            x = get_col_x(col_index)
            y = padding
            draw.rounded_rectangle([x, y, x + col_w, y + hero_h], radius=6,
                                    fill=glass_fill, outline=border_color, width=2)
            draw.text((x + 12, y + 7), title, font=font_hero_title, fill=text_white)
            return x, y

        g_color = get_rank_tier_color(global_rank, ESTIMATED_GLOBAL_PLAYERS)
        c_color = get_rank_tier_color(country_rank, ESTIMATED_COUNTRY_PLAYERS)

        x0, y0 = hero_box(1, "Global Ranking")
        if phase == "summary":
            draw.text((x0 + 12, y0 + 28), f"#{fmt_int(global_rank)}" if global_rank else "Unranked",
                       font=font_hero_value, fill=g_color)
            draw_segments(draw, (x0 + 12, y0 + 58),
                          delta_segments(diff_for("global_rank"), "global_rank", text_gray, green, red),
                          font_hero_sub)
        else:
            drew = draw_line_graph(img, draw, x0, y0, col_w, hero_h, global_history, g_color, lower_is_better=True)
            if drew:
                rank_str = f"#{fmt_int(global_rank)}" if global_rank else ""
                rw = draw.textlength(rank_str, font=font_daily_row_value)
                draw.text((x0 + col_w - 12 - rw, y0 + 8), rank_str, font=font_daily_row_value, fill=g_color)
            else:
                draw.text((x0 + 12, y0 + 34), "Collecting trend data\u2026", font=font_hero_sub, fill=text_gray)

        x0, y0 = hero_box(2, "Country Ranking")
        if phase == "summary":
            draw.text((x0 + 12, y0 + 28), f"#{fmt_int(country_rank)}" if country_rank else "Unranked",
                       font=font_hero_value, fill=c_color)
            draw_segments(draw, (x0 + 12, y0 + 58),
                          delta_segments(diff_for("country_rank"), "country_rank", text_gray, green, red),
                          font_hero_sub)
        else:
            drew = draw_line_graph(img, draw, x0, y0, col_w, hero_h, country_history, c_color, lower_is_better=True)
            if drew:
                rank_str = f"#{fmt_int(country_rank)}" if country_rank else ""
                rw = draw.textlength(rank_str, font=font_daily_row_value)
                draw.text((x0 + col_w - 12 - rw, y0 + 8), rank_str, font=font_daily_row_value, fill=c_color)
            else:
                draw.text((x0 + 12, y0 + 34), "Collecting trend data\u2026", font=font_hero_sub, fill=text_gray)

        x0, y0 = hero_box(3, "Daily Challenge")
        right_margin = x0 + col_w - 12
        if not daily:
            draw.text((x0 + 12, y0 + 34), "No data", font=font_hero_sub, fill=text_gray)
        elif phase == "summary":
            participation = daily.get("playcount", 0)
            daily_cur = daily.get("daily_streak_current", 0)
            weekly_cur = daily.get("weekly_streak_current", 0)
            rows = [
                ("Total Participation", f"{fmt_int(participation)}d", tier_color(participation, 0)),
                ("Current Daily Streak", f"{fmt_int(daily_cur)}d", tier_color(daily_cur, 1)),
                ("Current Weekly Streak", f"{fmt_int(weekly_cur)}w", tier_color(weekly_cur, 2)),
            ]
            ry = y0 + 27
            for label, value, color in rows:
                draw.text((x0 + 12, ry), label, font=font_daily_row_label, fill=text_white)
                value_w = draw.textlength(value, font=font_daily_row_value)
                draw.text((right_margin - value_w, ry - 1), value, font=font_daily_row_value, fill=color)
                ry += 16
        else:
            rows = [
                ("Best Daily Streak", f"{fmt_int(daily.get('daily_streak_best', 0))}d",
                 tier_color(daily.get("daily_streak_best", 0), 1)),
                ("Best Weekly Streak", f"{fmt_int(daily.get('weekly_streak_best', 0))}w",
                 tier_color(daily.get("weekly_streak_best", 0), 2)),
                ("Top 10% Placements", fmt_int(daily.get("top_10p_placements", 0)), text_white),
                ("Top 50% Placements", fmt_int(daily.get("top_50p_placements", 0)), text_white),
            ]
            ry = y0 + 24
            for label, value, color in rows:
                draw.text((x0 + 12, ry), label, font=font_daily_row_label, fill=text_gray)
                value_w = draw.textlength(value, font=font_daily_row_value)
                draw.text((right_margin - value_w, ry - 1), value, font=font_daily_row_value, fill=color)
                ry += 13

        def draw_standard_box(bx, by, label, value, field, formatter=None):
            draw.rounded_rectangle([bx, by, bx + col_w, by + core_row_h], radius=6, fill=glass_fill)
            raw_diff = diff_for(field)
            segs = delta_segments(raw_diff, field, text_gray, green, red, formatter)

            if phase == "summary":
                draw.text((bx + 10, by + 6), label, font=font_core_label, fill=text_white)
                val_font = font_core_value if draw.textlength(value, font=font_core_value) <= (col_w - 20) else font_core_value_small
                draw.text((bx + 10, by + 26), value, font=val_font, fill=text_white)
                draw_dual_font_left(draw, bx + 10, by + 48, segs, font_core_delta, font_core_delta_day)
            else:
                history_data = history_for(field)
                drew = draw_line_graph(img, draw, bx, by, col_w, core_row_h, history_data, (255, 255, 255),
                                        lower_is_better=(field in LOWER_IS_BETTER))
                draw.text((bx + 10, by + 6), label, font=font_core_label, fill=text_white)
                seg_w = dual_font_width(draw, segs, font_core_delta, font_core_delta_day)
                draw_dual_font_left(draw, bx + col_w - 10 - seg_w, by + 7, segs, font_core_delta, font_core_delta_day)
                if not drew:
                    draw.text((bx + 10, by + core_row_h / 2 - 4), "Collecting trend data\u2026",
                               font=font_hero_sub, fill=text_gray)

            draw.rounded_rectangle([bx, by, bx + col_w, by + core_row_h], radius=6, outline=border_color, width=2)

        fmt_pp = lambda v: f"{v:,.0f}pp"
        fmt_acc = lambda v: f"{v:.2f}%"
        fmt_pt = lambda v: fmt_playtime(v)

        y_cursor = header_h
        draw_standard_box(get_col_x(0), y_cursor, "Performance", f"{pp:,.0f}pp", "pp", fmt_pp)
        draw_standard_box(get_col_x(1), y_cursor, "Accuracy", f"{acc:.2f}%", "hit_accuracy", fmt_acc)
        draw_standard_box(get_col_x(2), y_cursor, "Play Count", fmt_int(play_count), "play_count")
        draw_standard_box(get_col_x(3), y_cursor, "Total Score", fmt_int(total_score), "total_score")

        y_cursor += core_row_h + gap
        draw_standard_box(get_col_x(0), y_cursor, "Ranked Score", fmt_int(ranked_score), "ranked_score")

        th_x = get_col_x(1)
        th_w = col_w * 2 + gap
        draw.rounded_rectangle([th_x, y_cursor, th_x + th_w, y_cursor + core_row_h], radius=6, fill=glass_fill)

        if phase == "summary":
            tot_for_bar = count_300 + count_100 + count_50
            if tot_for_bar > 0:
                bar_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                bar_draw = ImageDraw.Draw(bar_overlay)
                w300 = int(th_w * (count_300 / tot_for_bar))
                w100 = int(th_w * (count_100 / tot_for_bar))
                bar_y_offset = int(y_cursor + core_row_h - 12)
                bar_draw.rectangle([th_x, bar_y_offset, th_x + w300, y_cursor + core_row_h], fill=(0, 255, 255, 255))
                bar_draw.rectangle([th_x + w300, bar_y_offset, th_x + w300 + w100, y_cursor + core_row_h], fill=(0, 255, 0, 255))
                bar_draw.rectangle([th_x + w300 + w100, bar_y_offset, th_x + th_w, y_cursor + core_row_h], fill=(255, 165, 0, 255))
                box_mask = Image.new("L", img.size, 0)
                ImageDraw.Draw(box_mask).rounded_rectangle([th_x, y_cursor, th_x + th_w, y_cursor + core_row_h], radius=6, fill=255)
                bar_overlay.putalpha(Image.composite(bar_overlay.getchannel("A"), Image.new("L", img.size, 0), box_mask))
                img.alpha_composite(bar_overlay)

                def draw_pct(px, pw, pct):
                    if pw > 35:
                        txt = f"{pct:.1f}%"
                        tw = draw.textlength(txt, font=font_bar_pct)
                        draw.text((int(px + pw / 2 - tw / 2), int(bar_y_offset - 1)), txt, font=font_bar_pct, fill=(0, 0, 0))

                draw_pct(th_x, w300, (count_300 / tot_for_bar) * 100)
                draw_pct(th_x + w300, w100, (count_100 / tot_for_bar) * 100)
                draw_pct(th_x + w300 + w100, th_w - w300 - w100, (count_50 / tot_for_bar) * 100)

            sub_w = th_w / 4
            th_headers = ["Total Hits", "300s", "100s", "50s"]
            th_vals = [fmt_int(total_hits), fmt_int(count_300), fmt_int(count_100), fmt_int(count_50)]
            th_fields = ["total_hits", None, None, None]
            for i in range(4):
                cx = int(th_x + i * sub_w + 10)
                draw.text((cx, y_cursor + 6), th_headers[i], font=font_core_label, fill=text_white)
                draw.text((cx, y_cursor + 24), th_vals[i], font=font_th_value, fill=(255, 255, 0))
                if th_fields[i]:
                    segs = delta_segments(diff_for(th_fields[i]), th_fields[i], text_gray, green, red)
                    draw_dual_font_left(draw, cx, y_cursor + 40, segs, font_th_delta, font_th_delta_day)
        else:
            history_data = history_for("total_hits")
            drew = draw_line_graph(img, draw, th_x, y_cursor, th_w, core_row_h, history_data, (255, 255, 255), lower_is_better=False)
            draw.text((th_x + 10, y_cursor + 6), "Total Hits", font=font_core_label, fill=text_white)
            segs = delta_segments(diff_for("total_hits"), "total_hits", text_gray, green, red)
            seg_w = dual_font_width(draw, segs, font_core_delta, font_core_delta_day)
            draw_dual_font_left(draw, th_x + th_w - 10 - seg_w, y_cursor + 7, segs, font_core_delta, font_core_delta_day)
            if not drew:
                draw.text((th_x + 10, y_cursor + core_row_h / 2 - 4), "Collecting trend data\u2026",
                           font=font_hero_sub, fill=text_gray)

        draw.rounded_rectangle([th_x, y_cursor, th_x + th_w, y_cursor + core_row_h], radius=6, outline=border_color, width=2)

        draw_standard_box(get_col_x(3), y_cursor, "Hits Per Play", f"{hits_per_play:.1f}", "hits_per_play")

        y_cursor += core_row_h + gap
        draw_standard_box(get_col_x(0), y_cursor, "Maximum Combo", fmt_int(max_combo), "maximum_combo")
        draw_standard_box(get_col_x(1), y_cursor, "Replays Watched", fmt_int(replays_watched), "replays_watched_by_others")
        draw_standard_box(get_col_x(2), y_cursor, "Medals",
                           fmt_int(medals_count) if medals_count is not None else "\u2014", "medals")
        draw_standard_box(get_col_x(3), y_cursor, "Total Play Time", fmt_playtime(play_time_sec), "play_time", fmt_pt)

        y_cursor += core_row_h + 10

        draw.text((padding, y_cursor), "GRADES", font=font_section_title, fill=text_white)
        grades_box_y = int(y_cursor + grades_title_h)
        grade_inner_gap = 6
        grade_box_w = int((col_w - grade_inner_gap) // 2)

        grade_config = [
            ("SS", grades.get("ss"), (255, 255, 0), "grade_ss"),
            ("SSH", grades.get("ssh"), (255, 0, 255), "grade_ssh"),
            ("S", grades.get("s"), (255, 255, 0), "grade_s"),
            ("SH", grades.get("sh"), (0, 255, 255), "grade_sh"),
            ("A", grades.get("a"), (0, 255, 0), "grade_a"),
            ("B", grades.get("b"), (255, 170, 0), "grade_b"),
            ("C", grades.get("c"), (255, 102, 0), "grade_c"),
            ("D", grades.get("d"), (255, 0, 0), "grade_d"),
        ]

        for i, (label, count, color, field) in enumerate(grade_config):
            parent_col = i // 2
            sub_pos = i % 2
            x = int(get_col_x(parent_col) + sub_pos * (grade_box_w + grade_inner_gap))

            draw.rounded_rectangle([x, grades_box_y, x + grade_box_w, grades_box_y + grades_box_h],
                                    radius=4, fill=glass_fill, outline=border_color, width=2)
            draw.text((x + 6, grades_box_y + 11), label, font=font_grade_letter, fill=color)

            right_edge = x + grade_box_w - 6
            count_str = fmt_int(count) if count is not None else "\u2014"
            cw = draw.textlength(count_str, font=font_grade_count)
            draw.text((int(right_edge - cw), int(grades_box_y + 5)), count_str, font=font_grade_count, fill=(255, 255, 0))

            d = diff_for(field)
            if d:
                change_str = f"+{fmt_int(d)}" if d > 0 else f"-{fmt_int(abs(d))}"
                d_color = green if d > 0 else red
                cw1 = draw.textlength(change_str, font=font_grade_change_val)
                draw.text((int(right_edge - cw1), int(grades_box_y + 24)), change_str,
                           font=font_grade_change_val, fill=d_color)
                day_text = "this day"
                cw2 = draw.textlength(day_text, font=font_grade_change_text)
                draw.text((int(right_edge - cw2), int(grades_box_y + 40)), day_text,
                           font=font_grade_change_text, fill=(215, 215, 215))

        y_cursor = grades_box_y + grades_box_h + 10

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
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
