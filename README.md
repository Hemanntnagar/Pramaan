# Pramaan — backend (v0.1)

A real, standalone version of the Pramaan audit-verification tool — a normal
Python web server with its own database, instead of the earlier browser-only
demo. Runs in any browser, on your own laptop or a real server, with no
dependency on Claude.ai.

> **Want a live demo URL instead of running this locally?** See
> [`DEPLOY_VERCEL.md`](./DEPLOY_VERCEL.md) — deploys to Vercel with zero
> environment variables to configure.

## What this actually does

Registered Environment Auditors (the role created by India's Environment
Audit Rules, 2025) need to verify that a producer's claimed recycled-plastic
volumes match their paper trail. This tool:

- Takes a claimed quantity, a facility's registered capacity, and a list of
  supporting invoices/e-way bills
- Runs 7 explainable checks: volume mismatch, missing documents, capacity
  breach, out-of-period documents, duplicate invoice numbers *within* a
  submission, duplicate invoice numbers *across every case ever audited*, and
  rate-per-kg outliers
- Returns a risk score and a plain-language report
- Optionally reads an uploaded photo/PDF of an invoice with Gemini and
  auto-fills the fields, so the auditor doesn't have to type everything by hand
- Generates a downloadable PDF audit report for each stamped case

Every case is saved permanently to a local SQLite database (`pramaan.db`),
and the cross-case duplicate check is the one thing a single-case-only tool
structurally cannot do — it's the actual point of this version.

## Requirements

- **Python 3.10+** (the code uses modern type syntax; 3.11 or 3.12 is fine)
- A **Google Gemini API key** is optional — only needed for the "Upload invoice"
  button (see [Environment variables](#environment-variables))

## Tech stack

| Layer | Choice |
|-------|--------|
| Web framework | [FastAPI](https://fastapi.tiangolo.com/) |
| ASGI server | [Uvicorn](https://www.uvicorn.org/) |
| Database | SQLite (plain `sqlite3`, no ORM) |
| PDF reports | [ReportLab](https://www.reportlab.com/) with bundled DejaVu Sans fonts |
| AI extraction | Google Gemini API via [httpx](https://www.python-httpx.org/) |
| Frontend | Single static HTML file — no Node, no build step |

## Setup

```bash
cd pramaan-backend
python3 -m venv venv
source venv/bin/activate          # on Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then paste your Gemini API key into .env
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

Then open **http://localhost:8000** in any browser.

If you skip the `.env` step, everything still works except the "Upload
invoice" button — you'll see a small banner saying AI reading isn't
configured, and you just type the document details in by hand instead.

## Using the UI

The browser UI has two tabs:

- **New Audit Case** — enter producer details, add supporting documents
  manually or via upload, set a typical market-rate range (default ₹18–42/kg),
  and stamp the case. A "Load a sample case" link pre-fills a deliberately
  flawed submission for demo purposes.
- **Case Register** — lists every saved case, with links to reopen the report
  or download its PDF. "Clear all saved cases" wipes the database.

On first run the database file `pramaan.db` is created next to the app
(see [Database](#database)).

## Project layout

```
pramaan-backend/
  index.py          Vercel entrypoint — re-exports `app` from app/main.py
  app/
    main.py         FastAPI app — all the HTTP routes
    db.py           SQLite read/write helpers (no ORM, plain SQL)
    verify.py       the actual rule engine — the real logic lives here
    ai_extract.py   server-side call to Gemini for reading documents
    pdf_report.py   turns a verified case into a submittable PDF report
    fonts/          DejaVu Sans (bundled so ₹ renders correctly in PDFs)
  static/
    index.html      the whole frontend — one file, no build step
  requirements.txt
  .env.example
  DEPLOY_VERCEL.md  how to deploy to Vercel (no env vars required)
  .vercelignore
```

## The 7 checks (and how scoring works)

Each flag carries a severity — **low** (1 point), **medium** (2), or **high**
(4). The risk score is the sum of those weights. Verdict bands:

| Score | Verdict |
|-------|---------|
| 0 | Verified — no issues found |
| 1–3 | Flagged — low risk |
| 4–7 | Flagged — medium risk |
| 8+ | Flagged — high risk |

| # | Check | Thresholds |
|---|-------|------------|
| 1 | Claimed vs. documented volume | >10% gap → medium; >25% → high |
| 2 | No supporting documents | high, when claimed > 0 |
| 3 | Facility capacity | claimed > capacity → high; >95% of capacity → low |
| 4 | Document dated outside reporting period | medium |
| 5 | Duplicate invoice number within this submission | high |
| 6 | Duplicate invoice number in a *different* saved case | high |
| 7 | Rate per kg outside the entered market range | medium |

Cases are numbered sequentially as `2026-001`, `2026-002`, … based on how
many cases are already in the database.

## Environment variables

| Variable | Required? | Purpose |
|----------|-----------|---------|
| `GEMINI_API_KEY` | No | Enables `/api/extract` and the upload button. Get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey). |
| `VERCEL` | No (auto-set) | When present, SQLite writes to `/tmp/pramaan.db` instead of the project directory. You never set this yourself — Vercel sets it on deploy. |

Copy `.env.example` to `.env` locally. On Vercel, add `GEMINI_API_KEY`
under Project Settings → Environment Variables only if you want AI reading;
everything else works with zero configuration.

## Database

- **Locally:** `pramaan.db` in the project root (created on first request,
  gitignored).
- **On Vercel:** `/tmp/pramaan.db` — not durable across cold starts or
  function instances. Fine for a live demo; not for long-running pilots.
  See [`DEPLOY_VERCEL.md`](./DEPLOY_VERCEL.md).

Schema: three tables — `cases`, `documents`, `flags` — with an index on
`documents.invoice` to make the cross-case duplicate lookup fast.

## API reference (for poking at it with curl, or building another frontend)

| Method | Path                  | What it does                                   |
|--------|-----------------------|-------------------------------------------------|
| POST   | `/api/verify`         | Run the rule engine on a case and save it       |
| GET    | `/api/cases`          | List every saved case (summary fields only)     |
| GET    | `/api/cases/{case_no}`| Full detail for one case (docs + flags)         |
| GET    | `/api/cases/{case_no}/pdf` | Download a submittable PDF audit report  |
| DELETE | `/api/cases`          | Wipe every saved case                           |
| POST   | `/api/extract`        | Upload a file, get back AI-read invoice fields  |
| GET    | `/api/health`         | `{"ok": true, "ai_configured": true/false}`     |

**POST `/api/verify`** — JSON body:

```json
{
  "name": "Shree Polymers Pvt. Ltd.",
  "period": "2026-04",
  "claimed": 14000,
  "capacity": 15000,
  "rate_min": 18,
  "rate_max": 42,
  "docs": [
    {"invoice": "INV-2026-2201", "date": "2026-04-04", "qty": 4200, "rate": 27}
  ]
}
```

Returns the full case record (case number, flags, score, verdict, etc.) and
persists it to the database.

**POST `/api/extract`** — multipart form upload with a `file` field. Accepts
images and PDFs. Uses Gemini (`gemini-2.0-flash`) to return:

```json
{
  "invoice_no": "INV-2026-0042",
  "date": "2026-04-04",
  "quantity_kg": 4200,
  "rate_per_kg": 27,
  "facility_name": "Shree Polymers Pvt. Ltd."
}
```

Returns **422** with a plain-text detail message if no API key is configured
or the model response can't be parsed.

## What's deliberately not here yet

- **Auth / multiple auditors.** Right now this is single-tenant — fine for
  one auditor piloting it on their own laptop, not fine for a shared
  deployment with several auditors who shouldn't see each other's cases.
  The next real step is adding login + per-auditor data scoping.
- **A real database for production.** SQLite is genuinely fine at pilot
  scale (a few hundred cases). If this gets deployed for many auditors at
  once, swap `db.py` for Postgres — the function signatures wouldn't need
  to change, just what's inside them.
- **PDF page-splitting.** A multi-page PDF is currently sent to Gemini as
  one document; for now, upload single-invoice files for the most reliable
  reads.

## Tested

This was actually run end-to-end before being handed over — not just
written and hoped for:

- A clean case (`Verified — no issues found`)
- A second case that reused an invoice number from the first case, had an
  internal duplicate, breached facility capacity, and had an out-of-range
  rate — correctly flagged all of it and scored it `Flagged — high risk`,
  **including catching the cross-case invoice reuse**, which is the one
  feature a single-case tool can't do
- List/get/delete endpoints, and the no-API-key fallback on `/api/extract`
  (returns a clean error instead of crashing)
- The PDF report (`/api/cases/{case_no}/pdf`) — generated for both a
  high-risk flagged case and a clean verified case, rendered to an image
  and actually looked at (not just generated and assumed correct): the
  ₹ symbol displays properly (it's not in PDF's default fonts, so this
  bundles DejaVu Sans rather than silently rendering black boxes), the
  red/amber/green verdict colours match the brand, and a producer name
  containing "&" doesn't break the PDF generation
- A nonexistent case number on the PDF route returns a clean 404 instead
  of a server error
