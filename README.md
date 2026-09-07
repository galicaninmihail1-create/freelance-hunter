# Freelance Hunter Live Canary v1

Monitored opportunity discovery for the official FL.ru “Business automation” RSS feed:

`RSS → normalization → persistent dedupe → analytical enrichment → enriched/fallback Telegram card`

The service never applies to jobs or contacts clients. FL.ru HTML scraping, Kwork, automatic applications, and automatic score calibration are outside this version.

## Safety modes

- `HUNTER_MODE=manual` is the default. Starting FastAPI does not start polling.
- `HUNTER_MODE=live` starts the non-overlapping scheduler during FastAPI lifespan.
- A run that is still active causes an overlapping trigger to be skipped.
- Live mode requires the approved RSS URL, Polza, and Telegram credentials.

`analyzer_v1` is frozen to `qwen/qwen3.8-flash`, reasoning disabled, the current strict JSON Schema, and the current deterministic scoring engine.

## Configuration

Copy `.env.example` to `.env` and keep secrets out of source control.

```dotenv
HUNTER_MODE=manual
POLL_INTERVAL_MINUTES=10
TELEGRAM_SCORE_THRESHOLD=50
FL_RSS_URLS=https://www.fl.ru/rss/?category=41,https://www.fl.ru/rss/?subcategory=798&category=5,https://www.fl.ru/rss/?subcategory=279&category=5,https://www.fl.ru/rss/?subcategory=585&category=5,https://www.fl.ru/rss/?subcategory=703&category=5,https://www.fl.ru/rss/?subcategory=280&category=5,https://www.fl.ru/rss/?category=31
AI_PROVIDER=polza
POLZA_MODEL=qwen/qwen3.8-flash
POLZA_BASE_URL=https://polza.ai/api/v1
POLZA_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_ID=
TELEGRAM_WEBHOOK_SECRET=
```

Only switch `HUNTER_MODE` to `live` deliberately. Register the public HTTPS `/telegram/webhook` endpoint with Telegram before relying on inline-button callbacks.

## Dry-run

The dry-run fetches only the configured official RSS feeds. It never calls Polza,
never sends Telegram messages, and never inserts jobs. On the first successful
fetch of a newly configured feed, it does persist that feed's historical identity
baseline in the dedicated `rss_feed_state` and `rss_feed_baseline_identities`
tables.

```powershell
python -m app.dry_run_live_canary
```

It reports RSS items, new notification candidates, duplicates, hard-filtered jobs, jobs that would need AI, and the legacy diagnostic count of existing analyzed jobs above the Telegram threshold.
The report also includes each official feed name, items seen, new unique jobs attributed
to that feed, and duplicates already seen in another feed during the same poll.
It also reports newly onboarded feeds, historical identities suppressed before the
pipeline, and groups of feeds whose current stable identity sets are identical.

## Telegram behavior

Every new unique non-historical job receives one Telegram delivery attempt. Hard-filter
classification and score remain analytical data and never block that attempt. Successful
analysis produces the enriched card; an analysis or enrichment failure produces a
source-facts-only fallback card with no AI draft button. A durable reservation is written
before sending, so a restart does not produce a duplicate notification.

On the first live startup, every job already present in SQLite is durably classified as
`suppressed_pre_live`. RSS duplicates are never sent from the historical/dedupe path;
only jobs first inserted by a live poll can proceed through analysis, scoring, and notification.

Each newly added RSS feed has a separate durable onboarding baseline. Its first
successful fetch records every current stable FL.ru identity and sends none of
those items into hard filtering, Polza, drafts, or Telegram. Later polls suppress
those baseline identities globally across all feeds; only identities first seen
after onboarding enter the normal live pipeline. A failed feed fetch is never
marked onboarded, so its baseline is retried on a later successful poll.

Buttons:

- `🔥 Интересно` records `interested` feedback.
- `👎 Не подходит` records `rejected` feedback.
- `✍️ Подготовить отклик` reserves one draft request, generates an editable draft, and returns it only in Telegram.

Drafts are never submitted to FL.ru. Repeated button presses reuse the saved draft instead of making another paid request.

## Running

```powershell
python -m pip install -e ".[dev]"
python -m uvicorn app.main:app
```

### Local Windows canary

From the `freelance-hunter` directory, using the existing `.venv`:

```powershell
.\run_local.bat
```

The launcher binds only to `127.0.0.1:8765`, loads the existing `.env` and SQLite database, and writes safe application/access logs to its console. The scheduler starts only when `.env` explicitly contains `HUNTER_MODE=live`; keep `manual` through setup checks.

Without a public HTTPS URL, outgoing Telegram cards work but inline-button callbacks cannot reach `/telegram/webhook`. The buttons remain visible but are temporarily non-interactive. Do not use an insecure tunnel or expose the local HTTP port publicly.

Local Telegram setup order:

```powershell
.\.venv\Scripts\python.exe -m app.telegram_admin get-me
.\.venv\Scripts\python.exe -m app.telegram_admin discover-chat
.\.venv\Scripts\python.exe -m app.telegram_admin smoke-card
```

Insert `TELEGRAM_BOT_TOKEN` directly into `.env`, send `/start` to the bot, run `discover-chat`, then insert the returned ID as `TELEGRAM_ALLOWED_CHAT_ID`. These commands never print the token and `smoke-card` never invokes Polza. Do not register a webhook for the temporary local-only launch.

Manual endpoints:

- `POST /runs/dry-run` — RSS-only preview.
- `POST /runs/collect` — explicit analyzer_v1 processing; requires `AI_PROVIDER=polza`.
- `GET /jobs/shortlist` — current shortlisted jobs.
- `POST /telegram/webhook` — authorized Telegram callbacks.

## Recovery model

- Each job failure is isolated from subsequent jobs.
- Polza transport/API failures are not retried automatically.
- The analyzer permits its single schema-repair retry only after a successful HTTP response.
- Telegram notification and draft reservations are durable and at-most-once.
- Failed operations are stored without secrets for manual inspection; there are no infinite retries.

## Tests

```powershell
python -m pytest -q
```

Tests use fake AI and Telegram implementations and make no paid requests.
