# Deploying on EC2 — hourly sync + auto-refresh + served endpoint

This runs the pipeline on a server: **every hour** it syncs new threads from S3,
rebuilds `dashboard_data.js`, publishes the dashboard to a webroot, and nginx (or
a tiny Python server) serves it at an endpoint.

```
 systemd timer (hourly) ─▶ refresh.sh ─▶ aws s3 sync ─▶ build_dashboard.py ─▶ webroot/
                                                                                  │
                                                          nginx :80  ◀────────────┘  ─▶  http://<ec2>/
```

---

## ⚠️ 0. Security first — this dashboard contains PII

The **Users** tab shows real names/emails and every query is a verbatim trading
conversation. Do **not** put this on the open internet. Pick at least one:

- **Lock the security group** to your office/VPN CIDR (don't use `0.0.0.0/0`).
- **Require a login** — nginx basic auth (steps in `nginx-kyro.conf`), or an ALB with auth.
- **Keep it private** — private subnet, reach it via SSH tunnel:
  `ssh -L 8080:localhost:80 ubuntu@<host>` then open `http://localhost:8080`.

The serving layer is an **allowlist**: only `index.html`, `dashboard_data.js`,
and `assets/` are published to `webroot/`. The raw `threads/` archive and
`user-info.sql` are never web-exposed.

---

## 1. Instance + IAM role (replaces `aws sso login`)

A headless server can't do interactive SSO. Instead, attach an **IAM instance
role** with read access to the bucket — then `aws s3 sync` authenticates
automatically.

- Instance: Amazon Linux 2023 or Ubuntu, `t3.small` is ample.
- Attach a role with this least-privilege policy:

```json
{ "Version": "2012-10-17", "Statement": [
  { "Effect": "Allow",
    "Action": ["s3:ListBucket"],
    "Resource": "arn:aws:s3:::prod-sage-ai",
    "Condition": { "StringLike": { "s3:prefix": "archive/threads/*" } } },
  { "Effect": "Allow",
    "Action": ["s3:GetObject"],
    "Resource": "arn:aws:s3:::prod-sage-ai/archive/threads/*" }
]}
```

## 2. Install dependencies

**Amazon Linux 2023:**
```bash
sudo dnf install -y python3 nginx util-linux   # util-linux = flock
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip && sudo ./aws/install && rm -rf aws awscliv2.zip
```
**Ubuntu:** `sudo apt update && sudo apt install -y python3 nginx awscli`

## 3. Put the code on the box

```bash
mkdir -p ~/kyro-dashboard
# from your laptop — you do NOT need to copy threads/ (refresh.sh pulls it):
rsync -av --exclude threads --exclude 'dashboard_data.js' ./ ubuntu@<host>:~/kyro-dashboard/
```
Verify the role can read the bucket:
```bash
cd ~/kyro-dashboard && aws s3 ls s3://prod-sage-ai/archive/threads/ | head
```

## 4. First refresh (sync + build + publish)

```bash
cd ~/kyro-dashboard && chmod +x refresh.sh && ./refresh.sh
ls webroot/            # -> index.html  dashboard_data.js  assets/
```

## 5. Schedule it hourly (systemd timer — recommended)

```bash
sudo cp deploy/kyro-refresh.service deploy/kyro-refresh.timer /etc/systemd/system/
# edit User= / WorkingDirectory= / AWS_DEFAULT_REGION= in the .service if needed
sudo systemctl daemon-reload
sudo systemctl enable --now kyro-refresh.timer

systemctl list-timers kyro-refresh.timer    # see the next scheduled run
journalctl -u kyro-refresh.service -f        # tail refresh logs
```

`Persistent=true` means a missed run (instance was stopped) fires on next boot.

**Cron alternative** (if you prefer): run at :17 each hour
```bash
( crontab -l 2>/dev/null; echo "17 * * * * \$HOME/kyro-dashboard/refresh.sh >> /var/log/kyro-refresh.log 2>&1" ) | crontab -
```

## 6. Serve the endpoint

nginx is **optional** — use it only if you want port 80 or TLS. For a fixed port
inside a VPN (e.g. 8004), the plain Python service (Option A) is the complete,
simpler path — no extra software, and it serves `webroot/` only.

### Option A — plain Python service (recommended for a fixed VPN port, e.g. 8004)
```bash
sudo cp deploy/kyro-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kyro-dashboard
systemctl status kyro-dashboard           # confirm it's running
```
Allow **8004 from your VPN CIDR** in the security group → reach it at
`http://<ec2-private-ip>:8004/`. Serves `webroot/` only and auto-restarts on
crash/reboot (a bare `python3 …` in your SSH session would die on logout — the
service is what keeps it up).

### Option B — nginx (only if you later want port 80 / TLS)
```bash
sudo cp deploy/nginx-kyro.conf /etc/nginx/conf.d/kyro.conf
sudo nginx -t && sudo systemctl enable --now nginx
```
Open **port 80 to your VPN CIDR** → `http://<ec2-private-ip>/`

## 7. Recommended hardening

- **Basic auth:** uncomment the `auth_basic` lines in `nginx-kyro.conf` after
  `sudo htpasswd -c /etc/nginx/.htpasswd <user>`.
- **HTTPS:** with a domain, `sudo dnf install certbot python3-certbot-nginx` then
  `sudo certbot --nginx`. Or front it with an ALB/CloudFront that terminates TLS.

## Updating the dashboard code later
Re-`rsync` the repo (excluding `threads/`), then `./refresh.sh` (or just wait for
the timer) — it republishes `index.html` + `assets/` to `webroot/` along with fresh data.

## Troubleshooting
| Symptom | Check |
|---|---|
| `Unable to locate credentials` | Instance role attached? `aws sts get-caller-identity` |
| Timer not firing | `systemctl status kyro-refresh.timer`, `journalctl -u kyro-refresh` |
| 403 / wrong files served | nginx `root` points at `webroot`, not the repo root |
| nginx welcome page shows | remove the stock default site — AL2023: comment the `server {}` in `/etc/nginx/nginx.conf`; Ubuntu: `sudo rm /etc/nginx/sites-enabled/default` — then `sudo systemctl reload nginx` |
| `duplicate default_server` on `nginx -t` | same as above — the stock config already claims `:80` |
| Stale data in browser | hard-refresh; `dashboard_data.js` is sent `no-store` |
| `flock: command not found` | install `util-linux` |
