# Leadvera

Self-hosted web portal that researches **Indian-registered companies with related U.S. LLCs** every day,
verifies them against official registries, scores them, keeps one de-duplicated lead database, and emails
a report every morning (default **11:00 IST**) with the full Excel workbook attached.

```
 ┌──────────── worker (python -m app.worker) ─────────────┐        ┌──── web (uvicorn) ────┐
 │ scheduler tick (every 30s)                              │        │ login · dashboard     │
 │   08:30 IST → queue research run                        │        │ leads · queue         │
 │   11:00 IST → build report → SMTP → recipients (+xlsx)  │        │ pipeline · runs       │
 │ research run                                            │        │ exclusions · coverage │
 │   planner → assignments (industry × US state × pattern) │◄──DB──►│ settings · users      │
 │   Claude agent loop: web_search · web_fetch ·           │Postgres│ audit · email log     │
 │     check_known · lookup_indian_company · fetch_url ·   │        │ Excel / CSV export    │
 │     submit_candidate → rules gate → upsert (per record) │        └───────────────────────┘
 └─────────────────────────────────────────────────────────┘
```

## Two ways to pay for research

| Option | How | Billing |
|---|---|---|
| **Claude subscription (default)** | The worker runs Anthropic's Claude Code CLI headless (`claude -p`), signed in with a token from `claude setup-token`. Portal tools reach it through a local MCP server (`app/research/mcp_server.py`). | Uses your Pro/Max plan's usage limits; no API bill. Any `ANTHROPIC_API_KEY` is stripped from the CLI's environment so it can't switch to API billing. |
| **Anthropic API key** | The worker calls the Messages API directly (`app/research/agent.py`). | Pay per token + web search; per-run cost cap. |

Choose in **Settings → Research engine**.

## What makes the numbers trustworthy

The research agent only *proposes*. Code decides (`app/rules.py`, `app/ingest.py`):

| Rule | Enforced in code |
|---|---|
| Counted as VERIFIED only with: valid CIN/LLPIN + MCA/data.gov.in record, name ending in LLC, valid US state, an **official state-registry URL**, and relationship evidence with URLs | `rules.gate` – downgrades anything else to the Research Queue |
| `Inc.` / `Corp.` without an LLC → disqualified | `rules.gate` |
| Score/priority computed from evidence fields (Section 6), never from the agent | `rules.score` |
| De-duplication by CIN, US state + entity number, domain, normalised name; soft matches go to review, not new rows | `ingest._soft_match` |
| Outreach status, owner, notes, next action and the pipeline are never overwritten by research | `fields.PROTECTED` |
| Verified leads are never silently demoted; contradicting re-checks are flagged | `ingest.ingest_candidate` |
| Every field change is kept in `lead_history`; every admin action in the audit log | |
| Each candidate is committed on its own (checkpointing); the run can be cancelled; time and cost caps stop runs | `research/agent.py` |
| One run at a time across replicas (DB lock); the 11:00 email is sent once per day even with restarts | `scheduler.py`, `security.acquire_lock` |
| CAPTCHA / login / robots.txt-blocked sources are recorded as limitations, never bypassed | `research/tools.Fetcher` |

## Repository layout

| Path | Purpose |
|---|---|
| `app/web/` | FastAPI portal, Jinja templates, CSS/JS (no external CDNs) |
| `app/worker.py`, `app/scheduler.py` | Background worker, daily schedule, report & alert emails |
| `app/research/` | Planner, Claude agent loop, client tools, prompts |
| `app/rules.py`, `app/ingest.py` | Verification gates, scoring, upsert/dedupe |
| `app/excel.py` | 6-sheet workbook export (Verified Leads, Research Queue, Daily Run Log, Exclusions and Duplicates, Sales Pipeline, Research Coverage) |
| `app/mailer.py` | SMTP with retries + email log (Gmail/Workspace, Microsoft 365, Zoho, SES, SendGrid…) |
| `app/india_import.py` | Import MCA / data.gov.in company master CSVs for local CIN lookup |
| `app/cli.py` | Admin CLI (create users, run now, send report, import data, export) |
| `docker-compose.yml`, `Dockerfile`, `deploy/` | Production deployment (Postgres, web, worker, Caddy HTTPS, nightly backups) |
| `MASTER_PROMPT.md` | The business brief the system implements |
| `docs/DEPLOYMENT.md` | Step-by-step hosting guide |

## Run on your own computer with Docker

See **[LOCAL_SETUP.md](LOCAL_SETUP.md)** – double-click `start-local.bat` (Windows) or run `./start-local.sh` (Mac/Linux).

## Quick start (developers, without Docker)

```bash
pip install -r requirements-dev.txt
export ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='ChangeMe-2026!' ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.web.main:app --port 8000 &      # portal at http://localhost:8000
python -m app.worker                        # scheduler + research runs
```
SQLite is used by default locally; set `DATABASE_URL=postgresql+psycopg://...` for Postgres.

## Tests

```bash
python -m pytest -q tests                                                    # SQLite
TEST_DATABASE_URL=postgresql+psycopg://user@host/db python -m pytest -q tests  # Postgres
```
The suite covers verification gates, upsert/dedupe/promotion, protected fields, the agent loop (scripted fake
Claude client: `pause_turn`, parallel tool results, truncated tool calls, cost cap, cancel), the schedule
(queue once, 11:00 send once, interim + final, SMTP failure + retry, encrypted secrets), and the web app
(login, lockout, CSRF, roles, exports, settings).

## First-day checklist

1. Deploy (see `docs/DEPLOYMENT.md`) and sign in with the bootstrap admin; change the password.
2. **Settings → Research**: Anthropic API key, model, daily target, cost cap.
3. **Settings → Email**: recipients + SMTP; click **Send test email**.
4. **Settings → Schedule**: confirm 08:30 start / 11:00 report, Asia/Kolkata.
5. Optional but recommended: **Settings → India master data** – import data.gov.in Company Master CSVs.
6. **Runs → Run research now** once to watch a run live before the first scheduled morning.
7. **Users**: add sales (leads + pipeline) and analyst (research) accounts.

## Honest expectations

- **200 verified leads/day is a target, not a guarantee.** Strict verification (official registry pages for
  both entities + documented relationship) is slow, and some registries (e.g. Delaware's) are CAPTCHA-gated
  and are recorded as limitations rather than bypassed. Expect the verified count to start well below 200
  and grow as the master data, queue revisits and coverage rotation mature. The report always states the
  shortfall.
- **Cost** is metered per run (tokens + web searches) and shown on every run; the per-run cap (default
  $25) stops research when reached. Tune model, effort and caps in Settings.
- The portal never contacts leads. The only emails it sends are internal reports and alerts.
