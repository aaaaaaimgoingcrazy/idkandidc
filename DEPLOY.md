# Deploying the live osu! stats card (Render.com, free)

This turns `app.py` into an always-on URL that renders your current osu!
stats on demand, cached for 1 hour, so you can embed it in your osu! "me!"
page and it updates itself.

## 1. Get osu! API credentials

1. Go to https://osu.ppy.sh/home/account/edit
2. Scroll to **OAuth** → **New OAuth Application**
3. Name it anything (e.g. "stats card"), leave the callback URL blank
4. Copy the **Client ID** and **Client Secret** — you'll need them in step 4

## 2. Push this folder to GitHub

Create a new (can be private) GitHub repo and push these files:
`app.py`, `requirements.txt`, `render.yaml`, and the `fonts/` folder.

```
git init
git add .
git commit -m "osu stats card renderer"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

## 3. Deploy on Render

1. Sign up / log in at https://render.com
2. Click **New +** → **Blueprint**
3. Connect the GitHub repo you just pushed — Render will detect `render.yaml`
4. When prompted, paste in `OSU_CLIENT_ID` and `OSU_CLIENT_SECRET` from step 1
5. Click **Apply** and wait for the build to finish (a couple of minutes)
6. You'll get a URL like `https://osu-stats-card-xxxx.onrender.com`

Test it by opening this in a browser (replace with your own osu! user ID):
```
https://osu-stats-card-xxxx.onrender.com/renders/profile-basics?user=25752151
```
You should see your stat card image.

## 4. Keep it from sleeping (important, still free)

Render's free web services spin down after 15 minutes with no requests, so
the first viewer after a quiet period would face a slow ~30–50 second load.
Render's free plan includes 750 instance-hours/month, which is enough to
stay running 24/7 (a month is ~744 hours), so a simple external "keep-alive"
ping avoids the sleep entirely at no cost:

1. Go to https://cron-job.org (free, no card needed) and make an account
2. Create a new cron job:
   - URL: `https://osu-stats-card-xxxx.onrender.com/ping`
   - Interval: every 10–14 minutes
3. Save it — Render will now always see recent traffic and stay awake

## 5. Embed it in your osu! profile

In your osu! **me!** page editor, add (using your own user ID and app URL):

```
[url=https://osu-stats-card-xxxx.onrender.com/renders/profile-basics?user=25752151][img]https://osu-stats-card-xxxx.onrender.com/renders/profile-basics?user=25752151[/img][/url]
```

Anyone viewing your profile now loads that image straight from your app,
which serves a cached render if one exists from the last hour, or fetches
fresh stats and re-renders otherwise.

## Notes

- To show a different mode, add `&mode=taiko` (or `fruits`/`mania`) to the URL.
- The cache is per (user, mode) and lives in memory, so it resets on redeploy —
  harmless, it just means the very next request after a redeploy takes a
  couple of seconds longer while it fetches fresh data.
- If you outgrow the free tier (e.g. sharing this for many users) Render's
  paid Starter plan ($7/mo) removes the sleep behavior entirely.

---

# Part 2: Completion by Year tracking (optional add-on)

This adds a second image — a "Completion by Year" card showing what
percentage of all ranked+approved beatmaps you've played, per year,
2007–2026. It runs as a background thread **inside the same free web
service** you already deployed in Part 1 — no second Render service, no
extra cost. When the app boots, it automatically starts crawling in the
background alongside serving image requests.

It tracks two accounts independently:

- **official_osu** — your regular osu.ppy.sh account, standard `osu` mode
- **private_osurx** — your account on the private server, `osurx` (relax) mode

**This is a genuinely large, slow job** — checking every ranked+approved map
for one account is somewhere between many hours and multiple days of
continuous requests. The `/renders/completion` image shows partial results
the whole time and fills in as the crawl makes progress. It runs at a
steady trickle in the background and never blocks or slows down your
`/renders/profile-basics` card, since it's a separate thread.

## 1. Create a database (Neon, free forever)

Render's own free Postgres expires after 30 days — not ideal for a crawl
that might still be running weeks later. Neon's free tier has no expiry:

1. Go to https://neon.tech and sign up (GitHub login works)
2. Create a new project (any name/region)
3. Copy the connection string it shows you — looks like:
   `postgresql://user:password@ep-xxxx.aws.neon.tech/dbname?sslmode=require`
4. Keep this tab open, you'll paste this as `DATABASE_URL` in step 3

## 2. Get your private server's OAuth credentials

On the private server (`lazer-api.shikkesora.com` or wherever your relax
account lives), register an OAuth application the same way you did for
osu.ppy.sh, and note its Client ID, Client Secret, and your username there.
If you're not sure where that settings page is, ask whoever runs the server.

## 3. Replace your files and add the env vars

**Files to add/replace in your GitHub repo** (all in this zip):

| File | Action |
|---|---|
| `app.py` | Replace (now starts the worker thread automatically) |
| `worker.py` | **New** — add this file |
| `db.py` | **New** — add this file |
| `requirements.txt` | Replace (adds `psycopg2-binary`) |
| `render.yaml` | Replace (adds the new env var slots, still one service) |

Commit and push all five.

On Render, open your **existing** `osu-stats-card` service (don't create a
new one) → **Environment** tab → add these new variables:

| Variable | Value |
|---|---|
| `DATABASE_URL` | the Neon connection string from step 1 |
| `OFFICIAL_USERNAME` | your osu.ppy.sh username |
| `PRIVATE_BASE_URL` | e.g. `https://lazer-api.shikkesora.com` |
| `PRIVATE_CLIENT_ID` / `PRIVATE_CLIENT_SECRET` | from step 2 |
| `PRIVATE_USERNAME` | your username on the private server |

Saving these triggers a redeploy automatically. Your existing
`OSU_CLIENT_ID`/`OSU_CLIENT_SECRET` are reused for the beatmap crawl too —
no need to re-enter those.

## 4. Watch it work

Open the service's **Logs** tab on Render. You should see, mixed in with
the normal request logs:

```
[app] Completion worker thread started.
[worker] Schema ready.
[worker] Beatmap fetch: ['ranked', 'approved'] × 2007-2026
[worker]   ranked 2007: +512 maps
...
[worker] Beatmap fetch phase caught up.
[worker] Entering continuous score-check loop for both accounts.
[worker] official_osu: checked 50 this round — 50/118204 total
```

The beatmap-fetch phase (building the master map list) should finish in
under an hour. After that, the score-check counters climb slowly and
continuously — that's expected, and it survives redeploys since progress
is saved to the database as it goes, not kept in memory.

## 5. View and embed the completion card

```
https://osu-stats-card-xxxx.onrender.com/renders/completion?account=official
https://osu-stats-card-xxxx.onrender.com/renders/completion?account=relax
```

Each shows a "not scanned yet" placeholder for any year the crawl hasn't
reached, filling in real percentages as it goes. This image is cached for
6 hours (not 1, like the stats card), since the underlying data only
changes as fast as the crawl progresses — add `&refresh=1` to bypass that
cache while checking on progress.

Embed it the same way as the stats card:
```
[url=https://osu-stats-card-xxxx.onrender.com/renders/completion?account=official][img]https://osu-stats-card-xxxx.onrender.com/renders/completion?account=official[/img][/url]
```

## A note on the free tier's sleep behavior

Part 1's keep-alive ping (cron-job.org hitting `/ping` every 10–14 min) is
what keeps this service — and therefore the background crawl — running
24/7. If that ping ever stops, the service sleeps after 15 minutes idle,
and the crawl pauses until the next real request wakes it back up. No data
is lost either way; it just pauses and resumes.

## What's intentionally not included

The original script this was adapted from also builds PP-over-time graphs,
a custom XP/leveling system, monthly replay charts, and most-played-map
lists from the same score data. Those are a different scope from
"completion tracking" and aren't part of this — the `completions` table in
Postgres already saves per-map score/grade/pp/accuracy for every map it
checks, though, so building any of those later just means new queries
against data that's already being collected, not re-crawling anything.
