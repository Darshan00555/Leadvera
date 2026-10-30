# Deployment guide

The portal needs an **always-on server** (it runs a background worker that wakes up every morning).
Shared/cPanel web hosting will not work. Any of these does:

| Option | Typical size | Notes |
|---|---|---|
| VPS – DigitalOcean, Hetzner, AWS Lightsail, Linode, Hostinger VPS, E2E Networks (India) | 2 vCPU / 2–4 GB RAM / 40 GB disk | Recommended. Use the Docker steps below. |
| AWS EC2 / Azure VM / GCP Compute Engine | same | Same steps; open ports 80/443 in the security group. |
| Render / Railway / Fly.io | web service + background worker + managed Postgres | Run the image twice: default command (web) and `python -m app.worker` (worker). |

## A. Docker on a VPS (recommended)

1. **DNS** – create an `A` record, e.g. `leads.example.com` → your server's IP.
2. **Install Docker** (Ubuntu 22.04/24.04):
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER && newgrp docker
   ```
3. **Upload the project** to `/opt/afportal` (git clone, or `scp` the zip and unzip).
4. **Configure**:
   ```bash
   cd /opt/afportal
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # paste as SECRET_KEY
   nano .env    # DOMAIN, BASE_URL, SECRET_KEY, POSTGRES_PASSWORD, ADMIN_EMAIL, ADMIN_PASSWORD, ANTHROPIC_API_KEY
   ```
5. **Start**:
   ```bash
   docker compose up -d --build
   docker compose ps                 # db, web, worker, caddy, backup should be running
   docker compose logs -f worker     # see "worker ... started"
   ```
   Caddy obtains a Let's Encrypt certificate automatically; open `https://<DOMAIN>`.
6. **Firewall**: allow only 22, 80, 443 (`ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable`).
7. Sign in, change the admin password, then follow the *First-day checklist* in the README.

### Operations

| Task | Command |
|---|---|
| Update to a new version | `git pull && docker compose up -d --build` |
| Logs | `docker compose logs -f web worker` |
| Queue a run now | `docker compose exec worker python -m app.cli run-now` |
| Re-send the latest report | `docker compose exec worker python -m app.cli send-report` |
| Test SMTP | `docker compose exec worker python -m app.cli test-email` |
| Import India master data (large CSV) | `docker compose cp file.csv worker:/data/ && docker compose exec worker python -m app.cli import-india /data/file.csv --source https://www.data.gov.in/...` |
| Export workbook | `docker compose exec worker python -m app.cli export-xlsx /data/leads.xlsx` |
| Create a user | `docker compose exec web python -m app.cli create-user name@company.com --role sales` |
| Backups | nightly `pg_dump` into `./backups/` (14 days). Copy them off-server (S3, Drive) with your usual tool. |
| Restore | `docker compose exec -T db pg_restore -U afportal -d afportal --clean < backups/afportal-YYYYMMDD.dump` |

## B. Without Docker (Ubuntu + systemd)

```bash
sudo apt install -y python3.13-venv postgresql caddy
sudo useradd --system --home /opt/afportal afportal
sudo -u postgres createuser afportal -P && sudo -u postgres createdb -O afportal afportal
cd /opt/afportal && python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # set DATABASE_URL=postgresql+psycopg://afportal:PASS@localhost/afportal and the rest
sudo cp deploy/afportal-web.service deploy/afportal-worker.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now afportal-web afportal-worker
```
Caddy: `leads.example.com { reverse_proxy 127.0.0.1:8000 }` in `/etc/caddy/Caddyfile`, then `systemctl reload caddy`.

## Email delivery

Configure in **Settings → Email** (stored encrypted). Use a mailbox on your own domain for deliverability:

- **Google Workspace / Gmail**: host `smtp.gmail.com`, port 587, STARTTLS, username = mailbox, password = an
  [App Password](https://myaccount.google.com/apppasswords) (requires 2-Step Verification).
- **Microsoft 365**: `smtp.office365.com`, 587, STARTTLS; SMTP AUTH must be enabled for the mailbox.
- **Amazon SES / SendGrid / Mailgun / Zoho**: use their SMTP credentials.

Add SPF/DKIM/DMARC for the sending domain. Failures are retried, logged under **Email log**, and shown on the run.

## Network requirements for the server

Outbound HTTPS to `api.anthropic.com` (research), the public web (company sites, state registries,
mca.gov.in, data.gov.in), and your SMTP host on 587/465.

## Security notes

- HTTPS only in production (`BASE_URL=https://…` turns on secure cookies + HSTS).
- Passwords hashed with scrypt; 5 failed sign-ins lock an account for 15 minutes.
- CSRF tokens on every form; strict Content-Security-Policy; `noindex`.
- SMTP password and API key encrypted at rest with a key derived from `SECRET_KEY` – keep `.env` private
  and backed up (losing `SECRET_KEY` means re-entering those secrets).
- Roles: **admin** (everything), **analyst** (research, queue, exclusions, exports), **sales** (leads, pipeline).
- CSV exports neutralise spreadsheet formula injection from scraped text.
