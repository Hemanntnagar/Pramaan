# Deploying Pramaan on Vercel — no environment variables needed

This deploys with **zero configuration in the Vercel dashboard** — you don't
set up anything. Here's exactly what that gets you, and what it doesn't.

## What works with no env vars

- The full rule engine (all 7 checks)
- The case register and report view
- The PDF report download
- Manual entry of everything

## What's disabled with no env vars

- The "Upload invoice" AI-reading button. Without a `GEMINI_API_KEY`,
  the app shows a small banner saying so and you just type document
  details in by hand instead — it does not break or error out.

If you ever want that button working, add `GEMINI_API_KEY` under your
Vercel project's Settings → Environment Variables, with no code changes.
That's the one and only env var this app ever looks for.

## The one honest limitation: the database

Vercel Functions run on a read-only filesystem except for `/tmp`, and `/tmp`
is **not durable** — it can be wiped on a cold start or when your request
happens to land on a different function instance. This app detects it's
running on Vercel (via Vercel's own automatically-set `VERCEL` variable —
not something you configure) and writes its SQLite database to `/tmp`
instead of failing outright.

In practice, this means: great for a live demo where you create a few cases
and show them off in one sitting. **Not** something to rely on for cases
that need to survive for days/weeks on a real pilot — for that, you'd want
a real hosted database (Postgres, Turso, etc.), which does need a
connection-string environment variable. That's a deliberate, separate
next step, not something this deployment quietly pretends to solve.

## Deploy it

**Option A — Vercel CLI** (fastest):
```bash
npm i -g vercel
cd pramaan-backend
vercel
```
Answer the prompts (link or create a project) and it deploys. Re-run `vercel --prod` to push to your production URL.

**Option B — GitHub + Vercel dashboard:**
1. Push this folder to a GitHub repo
2. Go to vercel.com → Add New Project → import that repo
3. Leave every setting on its default — Vercel auto-detects FastAPI from
   `requirements.txt` and finds the entrypoint at `index.py`
4. Deploy

You don't need to touch the "Environment Variables" tab at all for this to work.

## How the auto-detection works (for your own understanding, not something to configure)

Vercel's Python runtime looks for a file named `app.py`, `index.py`,
`server.py`, `main.py`, `wsgi.py`, or `asgi.py` at the project root, and
loads a variable named `app` from it. This project's actual code lives in
`app/main.py` (as a package, to keep things organised) — `index.py` at the
root is a two-line file that just does:

```python
from app.main import app
```

so Vercel finds exactly what it's looking for without you having to
restructure anything else.
