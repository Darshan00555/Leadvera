# Run the portal on your own computer (Docker)

## 1. Install Docker Desktop (one time)

- **Windows 10/11**: https://www.docker.com/products/docker-desktop/ → install → restart if asked →
  open Docker Desktop and wait for **"Engine running"**. (It enables WSL 2 for you; accept the prompt.)
- **Mac** (Apple chip or Intel): same link → drag to Applications → open it once.
- **Linux**: install Docker Engine + the Compose plugin.

Needs ~4 GB free RAM and ~3 GB disk.

## 2. Start the portal

Unzip `af-leadgen-portal.zip`, open the `af-leadgen` folder, then:

| Windows | Mac / Linux |
|---|---|
| Double-click **`start-local.bat`** | Open Terminal in the folder → `./start-local.sh` |

The first time it asks for:
1. **Admin email** – your login.
2. **Admin password** – 12+ characters with upper case, lower case and a digit (no `$`).
3. **Anthropic API key** – leave it **empty** if you will use your Claude Pro/Max plan (step 3).
4. **Port** – press Enter for 8000.

It saves these in `.env.local` (private – don't share it), builds the containers (first time: a few
minutes), and opens **http://localhost:8000** in your browser.

## 3. Use your Claude Pro / Max plan for research (no API bill)

Research runs through **Claude Code** (included in the Docker image), signed in with your own Claude
account, so it uses your plan's usage limits instead of a separately billed API key.

1. Make sure the portal is running (step 2).
2. **Windows:** double-click **`connect-claude.bat`** · **Mac/Linux:** `./connect-claude.sh`
3. A long link appears. Open it in your browser, sign in with the Claude account that has Pro/Max, approve.
4. The page shows a code → paste it into the window → press Enter.
5. The window prints a token starting with `sk-ant-oat01-…`. Copy it.
6. Portal → **Settings → Research engine** → choose **"My Claude subscription (Pro / Max)"** → paste the token
   into **Subscription token** → model **Sonnet** → **Save**.
7. Dashboard → **Run research now**, then open *Runs & reports* to watch it live.

The token is valid for **1 year**; repeat these steps when it expires (the run alert email will tell you).

**Know the limits of a Pro plan:** Pro has a usage limit that resets every few hours and is shared with
your normal Claude chats and Claude Code use. Deep research (many searches and page reads per company)
uses it quickly, so expect roughly tens of companies per run, not hundreds. When the limit is hit, the run
stops cleanly, keeps everything found so far, and the report says "usage limit reached". Max plans have
higher limits; the API option in Settings has no plan limits but is billed separately.

## 4. Finish setup in the portal

1. Sign in.
2. **Settings → Email & report**: your email address(es) under *To*, plus SMTP. For Gmail:
   host `smtp.gmail.com`, port `587`, STARTTLS, username = your Gmail address, password = a
   **Gmail App Password** (Google Account → Security → 2-Step Verification → App passwords).
   Click **Send test email**.
3. **Settings → Research engine**: subscription token (step 3) or an Anthropic API key, plus the daily target.
4. **Settings → Schedule**: research starts 08:30, report at 11:00, time zone Asia/Kolkata.
5. Dashboard → **Run research now** to watch the first run live under *Runs & reports*.

## 5. Everyday use

| Task | Windows | Mac / Linux |
|---|---|---|
| Start | `start-local.bat` | `./start-local.sh` |
| Connect Claude subscription | `connect-claude.bat` | `./connect-claude.sh` |
| Stop (data is kept) | `stop-local.bat` | `./stop-local.sh` |
| Worker logs | `docker compose -f docker-compose.local.yml --env-file .env.local logs -f worker` | same |
| Wipe everything and start fresh | `docker compose -f docker-compose.local.yml --env-file .env.local down -v`, then delete `.env.local` | same |

The containers restart automatically when Docker Desktop starts. In Docker Desktop → Settings →
General, tick **"Start Docker Desktop when you sign in"** so the portal comes back after a reboot.

## Important: your computer must be on

The schedule runs inside Docker on this machine:

- If the computer is **asleep/off at 08:30**, research starts as soon as it is back on that day.
- The **11:00 report** is sent only if the computer is on (it is sent as soon as it wakes, the same day).
- For reliable daily emails without keeping a laptop awake, move to a small cloud server later – the same
  project runs there with `docker-compose.yml` (see `docs/DEPLOYMENT.md`).

## Troubleshooting

| Problem | Fix |
|---|---|
| "Docker is not running" | Open Docker Desktop and wait for "Engine running". |
| Port 8000 already in use | Delete `.env.local` and run the start script again with another port (e.g. 8080) – or edit `PORT=` in `.env.local`. |
| Forgot admin password | `docker compose -f docker-compose.local.yml --env-file .env.local exec web python -m app.cli create-user you2@example.com --role admin` |
| Test email fails with Gmail | Use an App Password, not your normal password; 2-Step Verification must be on. |
| Run fails "not signed in or the token expired" | Run connect-claude again and paste the new token in Settings → Research engine. |
| Report says "usage limit reached" | Your Pro limit ran out; research continues on the next run. Lower *Assignments per run* or use Sonnet. |
| connect-claude says the container is not running | Run start-local first. |
