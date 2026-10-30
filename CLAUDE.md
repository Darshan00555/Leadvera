# Leadvera — project guide for Claude Code

Leadvera is a self-hosted lead-intelligence portal. A background worker researches **Indian companies that also
run a company abroad** (U.S. LLC, UK Ltd, Australian/NZ Ltd, EU/EEA companies), checks the evidence, keeps a
de-duplicated lead database and emails an internal daily report with an Excel workbook. It never emails leads.

The original business brief is `MASTER_PROMPT.md`; local setup is `LOCAL_SETUP.md`; going live is
`docs/ORACLE_CLOUD.md` (and `docs/DEPLOYMENT.md` for any VPS).

## Stack and layout

| Piece | Where |
|---|---|
| Web portal (FastAPI + Jinja2, no JS framework) | `app/web/main.py`, `app/web/core.py`, `app/web/templates/`, `app/web/static/` |
| Worker: scheduler + research runs | `app/worker.py`, `app/scheduler.py` |
| Research engines | `app/research/claude_code.py` (default), `agent.py` (Anthropic API), `gemini.py` (Google Gemini) |
| Planner, prompts, tools | `app/research/planner.py`, `prompts.py`, `tools.py`, `mcp_server.py` |
| Verification gates, scoring, de-dupe | `app/rules.py`, `app/ingest.py`, `app/jurisdictions.py` |
| Fields / DB models / settings | `app/fields.py`, `app/models.py`, `app/appsettings.py`, `app/db.py` |
| Reports, email, Excel | `app/report.py`, `app/mailer.py`, `app/excel.py` |
| Tests (pytest, SQLite) | `tests/` |

Docker: `docker-compose.local.yml` (project `afportal-local`: db, web, worker; portal on `127.0.0.1:${PORT}`)
and `docker-compose.yml` (production: + Caddy HTTPS + nightly `pg_dump` backups). Code is **baked into the
image** — the containers do not mount the source folder.

## Commands (Windows, PowerShell)

```powershell
# start / rebuild after ANY code change (never use `docker cp` into containers: the next build loses it)
docker compose -f docker-compose.local.yml --env-file .env.local up -d --build
# logs
docker compose -f docker-compose.local.yml --env-file .env.local logs --tail 50 worker web
# tests: the suite needs Python 3.13, so run it in a throwaway container from the built image
docker run --rm -u root -v "${PWD}:/src" -w /src -e ANTHROPIC_API_KEY= -e GEMINI_API_KEY= --entrypoint sh afportal-local-worker -c "pip install -q -r requirements-dev.txt >/dev/null 2>&1; python -m pytest -q -p no:cacheprovider tests"
# queue a run from the CLI
docker compose -f docker-compose.local.yml --env-file .env.local exec worker python -m app.cli run-now --markets US,UK --locations "Delhi NCR" --assignments 10 --max-leads 30
```

- **Before rebuilding, check no run is active** (`select status from runs where status in ('queued','running')`):
  a rebuild kills the worker mid-run.
- Settings changes need no rebuild; a run reads its settings when it starts.
- Docker Desktop must be running; it has stopped on its own before (nothing runs while it is down).
- PowerShell 5.1 mangles quotes and `$` inside `-c "..."` one-liners: put Python in a script file instead.
- Never run `down -v` (deletes the database). Never print `.env.local` values, tokens or SMTP passwords.

## Research engines (Settings → Research engine)

1. **Claude Code on the user's Claude Pro/Max subscription** (default, `research_backend=claude_code`): runs
   `claude -p` in the worker with `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, stored encrypted), strips
   every `ANTHROPIC_*` key so it never bills the API, and gives the CLI only WebSearch/WebFetch plus the
   `afleads` MCP tools (`check_known`, `lookup_indian_company`, `fetch_url`, `submit_candidate`, `finish_batch`).
   Uses only: `claude_code_model` (haiku/sonnet/opus/fable), `claude_code_effort` (low/medium/high → appended
   to the system prompt), `claude_code_batch_minutes`, plus the shared limits below. Pro "usage/session limit"
   messages stop the run cleanly (`LIMIT_RE`), not as a failure.
2. **Anthropic API key** (`api`): `agent.py`. The only engine that uses model, effort, cost cap, turns, web
   searches and page fetches per assignment.
3. **Google Gemini** (`gemini`, added by another session): `gemini.py`, function calling with a schema
   sanitiser and retry/backoff. Free-tier AI Studio keys give Pro models a quota of 0 — use a Flash model.
   Its `web_search` tool is `ToolBox.search_web` (see *Known issues*).

Shared limits: `daily_target` = **daily lead limit** (new verified + web-verified across all runs that day, default
50); per-run `max_leads` cap; `max_batches_per_run` / per-run `assignments`; `max_run_minutes`;
`revisit_queue_per_run`.

## Targeting

Runs are launched from `/runs/new` (or the CLI) with an optional focus stored in `runs.targets`
(`planner.clean_targets`): markets (`US`, `GB`, `AU`, `NZ`, EU/EEA codes, or the `EU` / `EEA` shortcuts), U.S.
states, Indian cities/states (added to every query; other cities are dropped quickly), industries, assignments,
`max_leads`, queue revisits. Without a focus a run uses the saved defaults (`target_markets`, `us_states`,
`indian_states`, `industries`). Non-U.S. planner keys are `@GB` etc. so Germany never collides with Delaware.

## Lead tiers and verification (do not loosen)

`rules.gate()` decides `verification_status`; it can only downgrade what the agent proposes.
- **VERIFIED** – Indian entity on an official Indian record (MCA / data.gov.in) **and** the overseas entity on its
  country's official register (U.S.: LLC designator + state + Secretary of State; others: company-form designator
  + register host from `jurisdictions.py`) **and** a cited page linking both. U.S. rules are byte-for-byte the
  original ones. MCA pages and most U.S. registries need a CAPTCHA, so this tier is rare by design.
- **Web-verified** (`leads.web_verified_at`, `rules.web_evidence`) – an extra, lower tier: valid CIN + Indian
  legal name with a cited page, the overseas entity's exact legal name, and a cited page linking both. Gets a
  pipeline entry and its own list, report section and Excel sheet. It never changes `verification_status`, so
  every run keeps trying to promote it.
- **Research queue** – `PARTIALLY VERIFIED` / `UNVERIFIED` without the web tier. **Disqualified** → `exclusions`.

Never bypass CAPTCHAs, logins or robots.txt; restricted sources are recorded as limitations.

## Data model rules

- `fields.LEAD_FIELDS` defines the `Lead` columns dynamically — **never delete entries** (pages would 500).
  Change exports through `EXCEL_FIELDS` (the 18 sales columns) and research through `NOT_RESEARCHED`
  (15 fields the agent no longer collects, to save tokens).
- The `us_*` columns hold the overseas entity for every country; `foreign_country` says which (blank = US).
  Lead IDs are `IN-<country>-000123`.
- `db._add_missing_columns()` adds new nullable columns at startup (no Alembic). New columns must be nullable.
- Internal identifiers stay as they are even though the brand changed: DB user/database `afportal`, compose
  project `afportal-local`, MCP server `afleads`, cookie `af_session`, `AF_*` env vars and the
  `af-portal-secrets:` encryption salt. Changing the salt or `SECRET_KEY` makes saved secrets unreadable.

## Reports and email

- Daily report at `report_time` (default 11:00 Asia/Kolkata) for the day's run; interim report if research is still
  running after `report_wait_minutes`, final report when it ends; a run already emailed is not resent.
- Manual runs email their report with the workbook as soon as they finish (`email_after_run`, per-run checkbox).
- Workbook sheets: Web-Verified Leads (includes any VERIFIED), Research Queue, Daily Run Log, Exclusions and
  Duplicates, Sales Pipeline, Research Coverage. Lead sheets and the CSV export use only `EXCEL_FIELDS`.
- Branding comes from the `brand_name` / `brand_tagline` settings (default "Leadvera").

## Guardrails for changes

- Match the surrounding style; keep `rules.py` / `ingest.py` gates as strict as they are.
- Run the tests after every change and rebuild with `up -d --build`.
- Ask before anything destructive (deleting volumes/data/files, git history changes).
- The portal must never email leads – only internal reports and alerts to the configured recipients.

## Known issues / open items

- `ToolBox.search_web` (used by the Gemini and API engines, not by Claude Code) scrapes DuckDuckGo HTML with a
  browser User-Agent and without a robots.txt check – conflicts with the guardrails; route it through the polite
  fetcher with the honest `Config.USER_AGENT`, and decode DuckDuckGo redirect links so sources are not all
  `duckduckgo.com`.
- The Gemini engine has no tests.
- U.S. registries (Delaware, Wyoming, Florida Sunbiz, New Mexico) and MCA block automated access, so most leads
  stop at web-verified; UK Companies House is the most reliable official register.

## History (Oct 2026)

- **Setup:** DB-password sync in `start-local.*`; admin bootstrap only when ADMIN_EMAIL is missing; scheduler no
  longer piles up "SMTP not configured" errors; no duplicate daily reports; Pro session-limit detection.
- **Rebrand + UI:** AnywhereFormations → Leadvera (configurable); new design system (icon sidebar, top bar with
  search and theme toggle, light/dark, mobile drawer), redesigned pages, cache-busting asset URLs.
- **Targeting & markets:** run launcher, UK/AU/NZ/EU/EEA with official registers, country filter, column migration.
- **Lead quality:** web-verified tier + backfill, live "Found in this run" counter.
- **Reports & limits:** auto-email after manual runs, daily lead limit 50, per-run lead cap, 18-column Excel/CSV,
  no "Verified Leads" sheet, 15 unused fields no longer researched.
- **Other session:** Gemini engine; Haiku and effort options for Claude Code; cheaper defaults (Sonnet / low effort
  for the API engine, 10 assignments, 12 turns, 10 searches, 15 fetches, 6,000-character page text).
- **Going live:** `deploy/oracle-setup.sh`, `deploy/make-bundle.ps1` (code + data dump + server `.env` with the same
  `SECRET_KEY`), `docs/ORACLE_CLOUD.md`.
