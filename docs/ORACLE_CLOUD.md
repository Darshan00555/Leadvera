# Going live on Oracle Cloud (Always Free)

Leadvera runs 24×7 on a free Oracle Cloud ARM server with automatic HTTPS (Caddy) and nightly database backups.
Your leads, settings, Claude subscription token and Gmail SMTP move over from your PC.

You need: an Oracle Cloud account (card verification, nothing is charged for Always Free resources),
a domain or subdomain you control (e.g. `leads.yourdomain.com` on Hostinger), and about an hour.

---

## Part A – Create the server (Oracle Cloud console, in the browser)

1. **Sign up** at <https://www.oracle.com/cloud/free/>.
   Pick your **Home Region** carefully: it cannot be changed. *India South (Hyderabad)* or *India West (Mumbai)*.
2. Menu → **Compute → Instances → Create instance**
   - **Name:** `leadvera`
   - **Image:** *Change image* → **Canonical Ubuntu 24.04**
   - **Shape:** *Change shape* → **Ampere** → `VM.Standard.A1.Flex` → **2 OCPU, 12 GB memory**
     (Always Free allows up to 4 OCPU / 24 GB in total).
   - **Networking:** keep the default network; make sure **Assign a public IPv4 address** is on.
   - **SSH keys:** *Generate a key pair for me* → **Save private key**. Move the downloaded file to
     `$HOME\.ssh\leadvera.key`.
   - **Create.** If you see *Out of capacity*, pick another *Availability domain* or try again later.
3. Copy the instance's **Public IP address**.
4. **Open the web ports:** on the instance page click the **Subnet** → **Default Security List** →
   **Add Ingress Rules**, twice:
   - Source CIDR `0.0.0.0/0`, IP protocol **TCP**, Destination port **80**
   - Source CIDR `0.0.0.0/0`, IP protocol **TCP**, Destination port **443**

## Part B – Point your domain at the server (Hostinger hPanel)

5. **Domains → your domain → DNS / Nameservers → Add record**:
   type **A**, name **leads**, points to **the public IP**, TTL **300**.
   The portal will be at `https://leads.yourdomain.com`.

## Part C – Prepare the files (on your PC, PowerShell in the project folder)

6. The local portal must be running (it exports your data). Then:
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\make-bundle.ps1 -Domain leads.yourdomain.com
   ```
   This creates `dist\leadvera.tar.gz` (code), `dist\leadvera.dump` (your data) and `dist\server.env`
   (server settings, same `SECRET_KEY` as your PC so the saved token / SMTP password still work).
   **Never share `dist\server.env`.**
7. Lock down the key file (SSH refuses keys other users can read):
   ```powershell
   icacls $HOME\.ssh\leadvera.key /inheritance:r
   icacls $HOME\.ssh\leadvera.key /grant:r "$($env:USERNAME):R"
   ```

## Part D – Upload and start (replace `SERVER_IP`)

8. Upload:
   ```powershell
   scp -i $HOME\.ssh\leadvera.key dist\leadvera.tar.gz dist\leadvera.dump dist\server.env deploy\oracle-setup.sh ubuntu@SERVER_IP:~
   ```
9. Connect and prepare the server (answer `yes` to the fingerprint question):
   ```powershell
   ssh -i $HOME\.ssh\leadvera.key ubuntu@SERVER_IP
   ```
   ```bash
   bash oracle-setup.sh
   exit
   ```
   Connect again with the same `ssh` command (so Docker works without `sudo`).
10. Unpack, restore your data, start:
    ```bash
    mkdir -p leadvera && tar -xzf leadvera.tar.gz -C leadvera && mv server.env leadvera/.env && cd leadvera
    docker compose up -d --wait db
    docker compose cp ~/leadvera.dump db:/tmp/leadvera.dump
    docker compose exec db pg_restore -U afportal -d afportal --no-owner --clean --if-exists /tmp/leadvera.dump
    docker compose up -d --build
    ```
    The first build takes 5–10 minutes.
11. Check:
    ```bash
    docker compose ps                      # db, web, worker, caddy, backup: running
    docker compose logs --tail 30 worker   # "worker ... started", no tracebacks
    ```
    Open `https://leads.yourdomain.com` and sign in with **the same email and password as on your PC**.
12. **Stop the portal on your PC** (`stop-local.bat`). Otherwise both copies run research and send reports
    on the same Claude token.

---

## Updating the server after code changes

On the PC:
```powershell
powershell -ExecutionPolicy Bypass -File deploy\make-bundle.ps1 -Domain leads.yourdomain.com -CodeOnly
scp -i $HOME\.ssh\leadvera.key dist\leadvera.tar.gz ubuntu@SERVER_IP:~
```
On the server:
```bash
tar -xzf ~/leadvera.tar.gz -C ~/leadvera && cd ~/leadvera && docker compose up -d --build
```
Never re-upload `server.env` after going live unless you mean to replace the server's settings.

## Everyday operations (on the server, in `~/leadvera`)

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Worker log | `docker compose logs -f worker` |
| Restart | `docker compose restart web worker` |
| Backups | nightly at 02:15 into `~/leadvera/backups/` (14 days kept) |
| Restore a backup | `docker compose cp backups/afportal-YYYYMMDD.dump db:/tmp/r.dump && docker compose exec db pg_restore -U afportal -d afportal --no-owner --clean --if-exists /tmp/r.dump` |

Never run `docker compose down -v` – it deletes the database.

## Troubleshooting

- **Site does not open / certificate error:** the DNS A record has not spread yet (wait 5–30 min), or ports
  80/443 are missing from the Security List (step 4). `docker compose logs caddy` shows the reason.
- **"not signed in" on runs:** the Claude token expired – run `claude setup-token` on your PC
  (`connect-claude.bat`) and paste the new token in Settings → Research engine.
- **Test email fails:** Oracle blocks outgoing port 25 only; Gmail on port 587 (STARTTLS) works.
