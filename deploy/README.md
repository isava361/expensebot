# ExpenseBot Mini App: Ubuntu deployment

Target: `https://expense.ivansavelyev.ru:8443/`.
The files here are deployment templates, not evidence that the server has been
configured. SSH access and the new bot's token file are required to deploy.

## 1. Inspect before making changes

Run `sudo bash deploy/inspect-server.sh`. Inspect the full `nginx -T` output,
active sockets, services, renewal timers and existing ExpenseBot paths. Do not
publish inventory output if existing applications embed secrets in their config.
Record the existing KCalorieBot response status/body hash, certificate fingerprint,
service PID and the process listening on 443 for comparison after deployment.
Also inspect `journalctl -u nginx -n 50` and `/var/log/nginx/error.log` for a
previous failed reload. `nginx -T` describes files on disk, not necessarily what
the running workers use.

Confirm public A and AAAA DNS for `expense.ivansavelyev.ru` reach this server.
If AAAA exists, ensure IPv6 reaches Nginx on 80 and 8443 too; otherwise fix that
DNS record before certificate issuance. Match the new `listen` addresses and
options to existing sockets. The templates use IPv4 wildcard sockets; do not
add a competing wildcard if Nginx is bound to a specific address. Add IPv6
listeners to this vhost only if consistent with the inspected setup.

`18082` is a candidate, not a confirmed free port. Check `ss -ltnp`, then try a
temporary bind to `127.0.0.1:18082` with Python's `socket.bind`; if occupied,
choose a different free port and change both `.env` and `proxy_pass` entries.
Never use 443, 8080 or KCalorieBot's 18081. Do not stop mtg, restart Nginx or edit
existing applications' configurations to make a port available.

If the on-disk Nginx configuration already contains an unapplied listener
conflicting with mtg, do not reload it blindly. Resolve that discrepancy with
the server owner before proceeding; a syntax check alone cannot prove safety.

## 2. Install the isolated application

Use `/opt/expensebot` for code, `/etc/expensebot/expensebot.env` for credentials,
and `/var/lib/expensebot` for its ledger, wizard state and backups. If any of
these already exists, inspect it first and preserve its contents. Do not
overwrite a live bot's data or start another poller for the same token.

For a fresh installation, after checking the paths:

```bash
sudo useradd --system --home-dir /var/lib/expensebot --shell /usr/sbin/nologin expensebot
sudo install -d -o root -g root -m 0755 /opt/expensebot
sudo install -d -o root -g root -m 0700 /etc/expensebot
sudo install -d -o expensebot -g expensebot -m 0700 /var/lib/expensebot
```

Copy `main.py`, `core.py`, `repository.py`, `storage.py`, `handlers.py`, `rates.py`,
`workbook.py`, `miniapp.py`, `configure_menu.py`, `requirements.txt`, `migrations/`,
`web/` and `deploy/` into `/opt/expensebot`. Install `python3-venv` and `certbot`
if missing using the server's package manager; reuse the installed Nginx.

```bash
sudo python3 -m venv /opt/expensebot/.venv
sudo /opt/expensebot/.venv/bin/pip install -r /opt/expensebot/requirements.txt
```

Create `/etc/expensebot/expensebot.env` from `deploy/.env.example`, using the
new bot's token and the verified port. Keep it root-owned, mode `0600`.
Do not put credentials in shell history, Git, frontend files or command-line
arguments. systemd reads this file before dropping privileges to `expensebot`.
The bot and Mini App share this bot's SQLite ledger, separate from KCalorieBot.
Record the verified bot username as `EXPECTED_BOT_USERNAME=...` in this file.

```bash
sudo install -m 0644 /opt/expensebot/deploy/expensebot.service /etc/systemd/system/expensebot.service
sudo systemctl daemon-reload
sudo systemctl enable --now expensebot.service
sudo systemctl status expensebot --no-pager
curl --fail http://127.0.0.1:18082/healthz
sudo ss -ltnp
```

The service must listen only on `127.0.0.1:<chosen port>`. Only this new service
is started. Without `MINIAPP_URL` the existing Python entry point remains a
polling-only bot. This deployment enables both in one isolated systemd service.

## 3. Bootstrap HTTP, then issue the certificate

Ensure `sites-enabled` is included inside the existing Nginx `http` block.
If it is not, use the server's existing include directory for this new vhost;
do not change the global config just to match these paths. Do not enable both
the bootstrap and final templates at once.

```bash
sudo install -d -m 0755 /var/www/letsencrypt/.well-known/acme-challenge
sudo install -d -m 0755 /usr/local/libexec
sudo install -m 0755 /opt/expensebot/deploy/reload-nginx.sh /usr/local/libexec/expensebot-reload-nginx
sudo install -m 0644 /opt/expensebot/deploy/nginx-http.conf /etc/nginx/sites-available/expensebot
sudo ln -s /etc/nginx/sites-available/expensebot /etc/nginx/sites-enabled/expensebot
sudo /usr/local/libexec/expensebot-reload-nginx http
```

The helper checks syntax, requests reload, waits for new workers and verifies
the new vhost's response header. It prints journal and error logs for review.
Inspect new errors, especially `bind()`, `address already in use`, `emerg` and
conflicting server names. If reload fails, restore **only ExpenseBot's file**
to its last known working version and investigate; do not stop other services.

Write a uniquely named probe under
`/var/www/letsencrypt/.well-known/acme-challenge/`. Fetch it from outside the
server at `http://expense.ivansavelyev.ru/.well-known/acme-challenge/<probe>`.
Require HTTP 200 and the exact file contents, without following redirects.
Verify a nonexistent challenge returns 404, then remove only the probe file.

Issue a separate certificate with the operator's real email address:

```bash
sudo certbot certonly --webroot -w /var/www/letsencrypt \
  --cert-name expense.ivansavelyev.ru -d expense.ivansavelyev.ru \
  --email YOUR_EMAIL --agree-tos --non-interactive
```

Do not use `standalone` or the Certbot Nginx installer. This command neither
binds port 80 nor rewrites other Nginx virtual hosts.

## 4. Enable HTTPS and verify it really applied

```bash
sudo install -m 0644 /opt/expensebot/deploy/nginx-https.conf /etc/nginx/sites-available/expensebot
sudo /usr/local/libexec/expensebot-reload-nginx https
```

The HTTPS check validates TLS with SNI, requires new workers and the vhost
marker, and compares the certificate actually served on 8443 with the certificate
on disk. If Nginx listens on a specific IP, update the helper's loopback probes
to that inspected Nginx address; the Python application stays on loopback.

From an external machine, without `-k` or redirect following, verify:

```bash
curl --fail -i https://expense.ivansavelyev.ru:8443/
curl --fail https://expense.ivansavelyev.ru:8443/healthz
curl -i https://expense.ivansavelyev.ru:8443/api/me
```

Expect root HTML, `{"status":"ok","app":"expensebot"}` and HTTP 401 respectively.
Verify `/app.js`, `/app.css`, a forged Authorization header (401), and the HTTP
challenge path again. Check the certificate's SAN and expiry. Compare KCalorieBot
responses, certificate and PID, and the 443/8080 socket owners, with the recorded
baseline. If 8443 is blocked upstream, open only that required TCP port after
inspecting the host/provider firewall; do not flush firewall rules.

## 5. Certificate renewal

```bash
sudo install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
sudo install -m 0755 /opt/expensebot/deploy/renew-hook.sh /etc/letsencrypt/renewal-hooks/deploy/expensebot-nginx
sudo certbot renew --cert-name expense.ivansavelyev.ru --dry-run --run-deploy-hooks
```

Inspect this certificate's renewal config: authenticator `webroot`, correct
domain and `/var/www/letsencrypt`. The hook only acts on the ExpenseBot lineage,
reloads Nginx and verifies the served certificate. Certbot dry runs with deploy
hooks use the active certificate; the hook checks that certificate against disk.

Reuse the already active Certbot renewal scheduler. With the Ubuntu apt package,
enable `certbot.timer` if no scheduler exists. With snap, inspect and use
`snap.certbot.renew.timer`. Do not add a second cron/timer or modify existing
certificate renewal files. Confirm the next scheduled run with
`systemctl list-timers --all` and inspect dry-run logs. An existing failed
renewal hook for another app needs separate investigation, not an unrequested edit.

## 6. Connect the bot after HTTPS verification

Run using systemd's environment reader, so credentials never enter command args:

```bash
sudo systemd-run --wait --pipe --collect --unit=expensebot-configure-menu \
  --property=User=expensebot --property=Group=expensebot \
  --property=WorkingDirectory=/opt/expensebot \
  --property=EnvironmentFile=/etc/expensebot/expensebot.env \
  /opt/expensebot/.venv/bin/python /opt/expensebot/configure_menu.py
```

The script checks the public HTTPS root, health endpoint, unauthorized API and
the expected bot username, sets `setChatMenuButton`, then reads the menu back
with `getChatMenuButton`. It does not send chat messages. In Telegram, open the
new bot's private chat → **Расходы** and verify loading with real `initData`.
If a specific chat has an older menu override, update that chat's menu separately.
The bot-profile Main Mini App button is optional and configured in BotFather;
the API-installed menu button already launches the app with signed user data.

## App scope and security

The Mini App includes groups and settings, joining by code, members/balances,
paginated active/deleted expenses, equal/custom shares with review, editing,
history, restoration, receipt attachment/viewing and Excel export, plus overall
debts, partial payments and recipient confirmation. Its per-account offline
queue retains failed entries for manual correction and retry.
New Mini App writes appear there because both interfaces use the same repository.
Receipt uploads send a photo to the author's private bot chat and keep only
the Telegram `file_id` in SQLite. Excel is sent to the requesting user's private
chat only when they choose that action. Other Mini App writes do not send chat
notifications; recipients see pending payments in the debts screen.

When upgrading, apply the `/api/groups/.../expenses/.../receipt` location from
`nginx-https.conf`: it allows 10 MiB photos and turns off both request and response
buffering. Without it the previous 32 KiB limit rejects ordinary photos; buffering
must stay disabled to avoid temporary receipt files on the server. Uploads and
downloads in Python use memory only and share a three-request concurrency limit.
The Bot instance is passed by `main.py`; no extra storage service or environment
variable is required. The user must have started the bot in a private chat and
must not have blocked it. Photo and Excel integration tests use a fake Bot and
send no real Telegram messages.

Every API request validates Telegram HMAC-SHA256, constant-time hash comparison,
duplicate fields, user identity and `auth_date` (one hour by default, at most
30 seconds clock skew into the future). The token stays on the server. Auth
data travels in an Authorization header, is not persisted in browser storage
and is omitted from access logs. No cookie authentication or permissive CORS is
used. Expired initData requires reopening the app. Ledger permissions are
checked on the server, under the same lock as the operation. Expense operation
IDs prevent retrying a save from writing the same expense twice.

References: [Telegram validation and Mini Apps](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app),
[Certbot webroot and renewal](https://eff-certbot.readthedocs.io/en/stable/using.html),
[Nginx reload behavior](https://nginx.org/en/docs/control.html).
