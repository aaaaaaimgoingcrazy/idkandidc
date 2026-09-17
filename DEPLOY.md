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
