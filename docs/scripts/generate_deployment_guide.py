"""
Generates Komute_DigitalOcean_Deployment_Guide.pdf
Run once with: venv\\Scripts\\python.exe docs/scripts/generate_deployment_guide.py  (from repo root)
"""
import re
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, ListFlowable, ListItem, Preformatted, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas as canvas_mod

OUT_FILE = "Komute_DigitalOcean_Deployment_Guide.pdf"

styles = getSampleStyleSheet()

title_style = ParagraphStyle('TitleX', parent=styles['Title'], fontSize=26, spaceAfter=6, textColor=colors.HexColor('#0B3D2E'))
subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=13, alignment=TA_CENTER, textColor=colors.HexColor('#444444'), spaceAfter=4)
meta_style = ParagraphStyle('Meta', parent=styles['Normal'], fontSize=9, alignment=TA_CENTER, textColor=colors.HexColor('#888888'))

h1 = ParagraphStyle('H1', parent=styles['Heading1'], fontSize=17, spaceBefore=16, spaceAfter=8, textColor=colors.HexColor('#0B3D2E'), keepWithNext=True)
h2 = ParagraphStyle('H2', parent=styles['Heading2'], fontSize=13, spaceBefore=12, spaceAfter=6, textColor=colors.HexColor('#155B41'), keepWithNext=True)
h3 = ParagraphStyle('H3', parent=styles['Heading3'], fontSize=11, spaceBefore=8, spaceAfter=4, textColor=colors.HexColor('#333333'), keepWithNext=True)
body = ParagraphStyle('BodyX', parent=styles['BodyText'], fontSize=10, leading=14, spaceAfter=6)
note = ParagraphStyle('Note', parent=styles['BodyText'], fontSize=9.5, leading=13, spaceAfter=6, backColor=colors.HexColor('#FFF6DA'), borderColor=colors.HexColor('#E8C468'), borderWidth=0.75, borderPadding=6, leftIndent=2)
warn = ParagraphStyle('Warn', parent=styles['BodyText'], fontSize=9.5, leading=13, spaceAfter=6, backColor=colors.HexColor('#FDEAEA'), borderColor=colors.HexColor('#D9534F'), borderWidth=0.75, borderPadding=6, leftIndent=2)
code_style = ParagraphStyle('Code', parent=styles['Code'], fontSize=8.3, leading=11, backColor=colors.HexColor('#F4F4F4'), borderColor=colors.HexColor('#CCCCCC'), borderWidth=0.5, borderPadding=6, fontName='Courier')
bullet_style = ParagraphStyle('Bullet', parent=body, spaceAfter=3)

story = []
_bookmark_counter = [0]


def h1_(text):
    story.append(Paragraph(text, h1))


def h2_(text):
    story.append(Paragraph(text, h2))


def h3_(text):
    story.append(Paragraph(text, h3))


def p(text):
    story.append(Paragraph(text, body))


def code(text):
    story.append(Preformatted(text.strip('\n'), code_style))
    story.append(Spacer(1, 6))


def bullets(items, numbered=False):
    lst = ListFlowable(
        [ListItem(Paragraph(it, bullet_style), leftIndent=6) for it in items],
        bulletType='1' if numbered else 'bullet',
        leftIndent=16, spaceBefore=2, spaceAfter=8,
    )
    story.append(lst)


def note_(text):
    story.append(Paragraph("<b>NOTE:</b> " + text, note))


def warn_(text):
    story.append(Paragraph("<b>IMPORTANT:</b> " + text, warn))


def hr():
    story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor('#CCCCCC'), spaceBefore=4, spaceAfter=10))


def table(header, rows, col_widths=None):
    data = [header] + rows
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0B3D2E')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F7F7F7')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))


def page_break():
    story.append(PageBreak())


# ============================================================================
# COVER PAGE
# ============================================================================
story.append(Spacer(1, 60 * mm))
story.append(Paragraph("Komute", title_style))
story.append(Paragraph("Production Deployment Guide — DigitalOcean", subtitle_style))
story.append(Paragraph("Django/Channels ride-sharing platform + verification node (driver verification)", subtitle_style))
story.append(Spacer(1, 10 * mm))
story.append(Paragraph("Prepared for: Ops / Deployment Team", meta_style))
story.append(Paragraph("Scope: KomuteProject (Django) and its verification node (verification_node/, Python ML) run on one Ubuntu 22.04 droplet", meta_style))
page_break()

# ============================================================================
# TOC (manual, PDF bookmarks are added per-section for sidebar navigation)
# ============================================================================
h1_("Table of Contents")
toc_items = [
    "1. Architecture Overview",
    "2. Pre-Deployment Fixes Already Applied",
    "3. Prerequisites",
    "4. Step 1 — Create the DigitalOcean Droplet",
    "5. Step 2 — Initial Server Hardening",
    "6. Step 3 — Install System Dependencies",
    "7. Step 4 — PostgreSQL Setup",
    "8. Step 5 — Redis Setup",
    "9. Step 6 — Deploy the Komute Django App",
    "10. Step 7 — Production Settings Hardening (settings.py)",
    "11. Step 8 — systemd Service for Komute (Daphne)",
    "12. Step 9 — Install the Verification Node Packages and Models",
    "13. Step 10 — Run the Verification Node and the Map Node (systemd)",
    "14. Step 11 — Nginx Reverse Proxy + HTTPS",
    "15. Step 12 — Cron Jobs (Scheduled Management Commands)",
    "16. Step 13 — End-to-End Smoke Test",
    "17. Backups & Monitoring",
    "18. Update / Redeploy Workflow",
    "19. Troubleshooting Reference",
    "Appendix A — Full Config File Listings",
]
bullets(toc_items)
page_break()

# ============================================================================
# 1. ARCHITECTURE
# ============================================================================
h1_("1. Architecture Overview")
p("Komute is a Django 5.2 app served over ASGI (Django Channels + Daphne) for WebSocket chat, backed by "
  "PostgreSQL and Redis. Driver identity verification runs in the <b>verification node</b> "
  "(<font face='Courier'>verification_node/</font>), a second Python process in the same virtual environment. "
  "It stores nothing: Komute keeps only the outcome.")
warn_("Komute calls the node at <font face='Courier'>DL_VERIFICATION_API_URL</font> (default "
      "<font face='Courier'>http://127.0.0.1:8080/api/v1/verify</font>), with the key in "
      "<font face='Courier'>DL_VERIFICATION_API_KEY</font>. The node needs <b>2 GB of RAM or more</b> "
      "and runs one verification at a time. This guide runs it on the same droplet. To use a separate "
      "machine, change the URL, put TLS in front of the node and bind it to a private address.")

p("Recommended process topology on the single droplet:")
code(r"""
Internet
   |
   |  443/80 (HTTPS/HTTP)
   v
+-------------------------------------------------------------+
|  Nginx  (reverse proxy, TLS termination, static/media files)|
+-------------------------------------------------------------+
   |  proxy_pass 127.0.0.1:8000 (HTTP + WebSocket upgrade)
   v
+-------------------------------------------------------------+
|  Daphne (ASGI)  --  Komute Django app       [systemd unit]  |
|    - HTTP views, admin, chat consumer                       |
+-------------------------------------------------------------+
   |                                   |
   | psycopg2                         | channels_redis
   v                                   v
+------------------+           +------------------+
| PostgreSQL 16     |           | Redis 7          |
| (apt package)     |           | (apt package)    |
+------------------+           +------------------+

Komute verification call --------> http://127.0.0.1:8080/api/v1/verify
                                              |
                                              v
                              +----------------------------------+
                              | verification node (FastAPI)      |
                              | systemd unit: komute-verify      |
                              | same Python venv, port 8080      |
                              | ONNX models: barcode, face, plate|
                              +----------------------------------+

Port 8080 listens on 127.0.0.1 only (not exposed to the
internet) - only Nginx (80/443) is internet-facing, enforced via UFW.
""")

p("<b>One process manager.</b> Komute and the verification node both run natively under systemd, in "
  "the same Python venv. There is no Docker. The node's models are ONNX files that "
  "<font face='Courier'>fetch_models.py</font> downloads and checks against a hash list.")

page_break()

# ============================================================================
# 2. PRE-DEPLOYMENT FIXES
# ============================================================================
h1_("2. Pre-Deployment Fixes Already Applied")
p("While preparing this guide, two production-blocking gaps were found in KomuteProject and have been "
  "fixed directly in the repository so that the steps below work without surprises:")

table(
    ["File", "Issue", "Fix applied"],
    [
        ["requirements.txt",
         "reportlab is imported in komute/views.py (PDF ride-receipt generation) but was never listed as "
         "a dependency. The receipt download would raise ModuleNotFoundError on a clean server install.",
         "Added reportlab==4.5.1 (pinned to the version already used in local development)."],
        ["config/settings.py",
         "STATIC_ROOT was not defined. Django's collectstatic management command requires it and will "
         "hard-fail without it, which blocks Nginx from serving static files in production.",
         "Added STATIC_ROOT = BASE_DIR / 'staticfiles'."],
    ],
    col_widths=[95, 220, 190],
)
note_("These two fixes are already committed to the working tree. If you deploy from a different copy of "
      "the repo (e.g. an older git history), re-apply them before continuing, or diff against this copy.")
p("Two further production-readiness items are <b>not</b> auto-fixed because they require values only you "
  "know (your real domain name) and a decision about strictness — they are covered explicitly in "
  "<b>Step 7 (Production Settings Hardening)</b> below: CSRF_TRUSTED_ORIGINS and secure-cookie/HTTPS "
  "settings.")

page_break()

# ============================================================================
# 3. PREREQUISITES
# ============================================================================
h1_("3. Prerequisites")
bullets([
    "A DigitalOcean account with billing enabled.",
    "A registered domain name you can point at the droplet (e.g. komute.example.com).",
    "An SSH key pair on your local machine (<font face='Courier'>ssh-keygen -t ed25519</font>).",
    "The Komute codebase (it includes verification_node/), ready to transfer to the server "
    "(via git push to a private repo, or scp/rsync).",
    "SMTP credentials for transactional email (already required by Komute's .env — see README.md).",
])
h2_("Droplet Sizing")
p("The verification node loads ONNX models (face, plate, barcode) into memory and runs CPU-only "
  "inference (no GPU needed). One verification peaks near 1.2 GB. The node needs 2 GB of RAM or more "
  "on top of Postgres, Redis and Daphne. Undersizing this will cause OOM kills.")
table(
    ["Plan", "vCPU / RAM", "Suitability"],
    [
        ['Current test droplet', '1 vCPU / 961 MB', 'Testing only, until it is resized. A verification peaks near 1.2 GB: add 2 GB of swap first (below).'],
        ["Basic Premium AMD, 4 GB", "2 vCPU / 4 GB", "Minimum viable — acceptable for staging/low traffic. Watch memory closely."],
        ["Basic Premium AMD, 8 GB (recommended)", "4 vCPU / 8 GB", "Comfortable headroom for Postgres + Redis + Daphne + the verification node."],
    ],
    col_widths=[170, 110, 225],
)
bullets([
    "OS image: <b>Ubuntu 22.04 LTS x64</b> (used throughout this guide).",
    "Enable weekly backups or snapshots (small extra cost, cheap insurance).",
    "25 GB+ SSD disk recommended — the node's five models take about 50 MB.",
    "On the 961 MB test droplet, add a 2 GB swap file before starting the verification node, or a verification can be killed for lack of memory: <font face='Courier'>sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile</font> (add it to /etc/fstab to keep it after a reboot).",
])

page_break()

# ============================================================================
# 4. STEP 1 - CREATE DROPLET
# ============================================================================
h1_("4. Step 1 — Create the DigitalOcean Droplet")
bullets([
    "Log in to the DigitalOcean control panel → <b>Create → Droplets</b>.",
    "Choose image: <b>Ubuntu 22.04 (LTS) x64</b>.",
    "Choose plan: Basic → Premium AMD/Intel → <b>8 GB RAM / 4 vCPU</b> (see sizing table above).",
    "Choose a datacenter region close to your users.",
    "Authentication: select <b>SSH Key</b> and add your public key (avoid password auth entirely).",
    "Hostname: e.g. <font face='Courier'>komute-prod-01</font>.",
    "Enable backups (recommended).",
    "Click <b>Create Droplet</b> and note the assigned public IPv4 address.",
], numbered=True)
h2_("DNS")
p("In your domain registrar / DNS provider, create an A record pointing your domain (and www, if used) "
  "at the droplet's public IP:")
code(r"""
Type   Name              Value              TTL
A      komute            <droplet-ip>       3600
A      www.komute        <droplet-ip>       3600
""")

page_break()

# ============================================================================
# 5. STEP 2 - HARDENING
# ============================================================================
h1_("5. Step 2 — Initial Server Hardening")
p("SSH in as root the first time only, to create a non-root deploy user:")
code(r"""
ssh root@<droplet-ip>

adduser deploy
usermod -aG sudo deploy

# Copy your SSH key to the new user
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy

exit
""")
p("From now on, connect as the deploy user:")
code(r"""ssh deploy@<droplet-ip>""")

h2_("Disable root login and password auth")
code(r"""
sudo nano /etc/ssh/sshd_config
# set:
#   PermitRootLogin no
#   PasswordAuthentication no

sudo systemctl restart ssh
""")

h2_("Firewall (UFW)")
p("Only SSH, HTTP and HTTPS are exposed to the internet. Postgres (5432), Redis (6379), the verification node "
  "(8080) all stay bound to localhost/loopback and are never opened in "
  "the firewall.")
code(r"""
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
""")

h2_("System updates")
code(r"""
sudo apt update && sudo apt -y upgrade
sudo timedatectl set-timezone UTC
sudo apt -y install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
""")

page_break()

# ============================================================================
# 6. STEP 3 - SYSTEM DEPENDENCIES
# ============================================================================
h1_("6. Step 3 — Install System Dependencies")

h2_("Python 3.11, build tools, Nginx, PostgreSQL client, Redis")
code(r"""
sudo apt update
sudo apt install -y software-properties-common build-essential git curl \
    python3.11 python3.11-venv python3.11-dev \
    libpq-dev nginx redis-server postgresql postgresql-contrib \
    certbot python3-certbot-nginx
""")

h2_("Confirm Redis is running")
code(r"""
sudo systemctl enable --now redis-server
redis-cli ping     # expect: PONG
""")

page_break()

# ============================================================================
# 7. STEP 4 - POSTGRESQL
# ============================================================================
h1_("7. Step 4 — PostgreSQL Setup")
code(r"""
sudo -u postgres psql

CREATE DATABASE komute;
CREATE USER komute_user WITH PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
ALTER ROLE komute_user SET client_encoding TO 'utf8';
ALTER ROLE komute_user SET default_transaction_isolation TO 'read committed';
ALTER ROLE komute_user SET timezone TO 'UTC';
GRANT ALL PRIVILEGES ON DATABASE komute TO komute_user;
\c komute
GRANT ALL ON SCHEMA public TO komute_user;
\q
""")
note_("PostgreSQL 15+ revokes default CREATE privileges on the public schema from new users — the "
      "<font face='Courier'>GRANT ALL ON SCHEMA public</font> line above is required or "
      "<font face='Courier'>manage.py migrate</font> will fail with a permission error.")
p("Confirm the database accepts local connections (default <font face='Courier'>pg_hba.conf</font> on "
  "Ubuntu uses peer/md5 for local — no change needed unless you customized it).")

page_break()

# ============================================================================
# 8. STEP 5 - REDIS
# ============================================================================
h1_("8. Step 5 — Redis Setup")
p("Redis backs the Django Channels layer (WebSocket chat fan-out) and the shared cache used for "
  "rate limiting. Use this local redis-server, not a free hosted plan: Upstash deletes free "
  "databases after 14 days of inactivity, which silently breaks chat. Default install binds to "
  "127.0.0.1 only, which is correct — do not expose it externally.")
code(r"""
sudo systemctl status redis-server
redis-cli ping    # PONG

# Confirm it only listens on localhost and protected mode is on
grep -E '^(bind|protected-mode)' /etc/redis/redis.conf
#   bind 127.0.0.1 -::1
#   protected-mode yes
sudo ss -ltnp | grep 6379     # only 127.0.0.1:6379 / [::1]:6379, never 0.0.0.0
sudo ufw status | grep 6379   # no output: the port is not opened in UFW
""")
p("Keep REDIS_URL=redis://127.0.0.1:6379 in .env. If a managed Redis is ever used instead, it "
  "must be a paid plan with a rediss:// (TLS) URL; Komute verifies its certificate.")
p("Optional hardening — set a password:")
code(r"""
sudo nano /etc/redis/redis.conf
# requirepass  <strong-redis-password>

sudo systemctl restart redis-server
""")
p("If you set a password, update REDIS_URL in Komute's .env accordingly:")
code(r"""REDIS_URL=redis://:<strong-redis-password>@127.0.0.1:6379""")

page_break()

# ============================================================================
# 9. STEP 6 - DEPLOY KOMUTE
# ============================================================================
h1_("9. Step 6 — Deploy the Komute Django App")

h2_("Transfer the code")
p("This project is not currently a git repository. Either initialize one and push to a private GitHub/"
  "GitLab repo (recommended, makes future redeploys a one-line git pull), or transfer the files directly "
  "with rsync:")
code(r"""
# Option A - from your local machine, over rsync (excludes venv/pycache/db.sqlite3)
rsync -avz --exclude 'venv' --exclude '__pycache__' --exclude 'db.sqlite3' \
    "./KomuteProject/" deploy@<droplet-ip>:/home/deploy/apps/komute/

# Option B - if you push the project to a private git remote
ssh deploy@<droplet-ip>
mkdir -p /home/deploy/apps && cd /home/deploy/apps
git clone <your-private-repo-url> komute
""")

h2_("Create the virtualenv and install dependencies")
code(r"""
cd /home/deploy/apps/komute
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
""")

h2_("Create the production .env")
p("Copy the structure documented in README.md. Generate fresh secrets for production — "
  "<b>do not reuse development secrets</b>, with one exception noted below.")
code(r"""
# Generate a new Django SECRET_KEY
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# Generate a Fernet key for FIELD_ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
""")
warn_("FIELD_ENCRYPTION_KEY encrypts biometric/OCR fields at rest (see "
      "django-encrypted-model-fields in models.py). If you are migrating an <b>existing</b> production "
      "database with real encrypted data, you must reuse the exact same key used to write that data, or "
      "those fields become permanently unreadable. Only generate a fresh key for a brand-new database.")
code(r"""
nano /home/deploy/apps/komute/.env
""")
table(
    ["Variable", "Production value"],
    [
        ["DEBUG", "False"],
        ["ALLOWED_HOSTS", "komute.example.com,www.komute.example.com"],
        ["SECRET_KEY", "<generated above>"],
        ["FIELD_ENCRYPTION_KEY", "<generated above>"],
        ["DB_NAME / DB_USER / DB_PASSWORD", "komute / komute_user / <postgres password from Step 4>"],
        ["DB_HOST / DB_PORT", "localhost / 5432"],
        ["REDIS_URL", "redis://127.0.0.1:6379 (or with password, see Step 5)"],
        ["EMAIL_HOST / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD / etc.", "your production SMTP credentials"],
        ["CONTACT_EMAIL, SEARCH_RADIUS_KM, SERVICE_FEE_CENTS, ...", "business values — see README.md for defaults"],
    ],
    col_widths=[230, 275],
)

h2_("Migrate, collect static, create admin user")
code(r"""
mkdir -p logs media staticfiles
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
""")

h2_("Smoke-test Daphne manually before wiring systemd")
code(r"""
python -m daphne -b 127.0.0.1 -p 8000 config.asgi:application
# In another shell: curl -I http://127.0.0.1:8000/
# Ctrl+C to stop once confirmed
""")

page_break()

# ============================================================================
# 10. STEP 7 - SETTINGS HARDENING
# ============================================================================
h1_("10. Step 7 — Production Settings Hardening (settings.py)")
p("Nginx will terminate TLS and proxy plain HTTP to Daphne on 127.0.0.1:8000. Add the following to "
  "<font face='Courier'>config/settings.py</font> (below the existing STATIC_ROOT block is a good spot) "
  "so Django knows it is being served over HTTPS and enforces secure cookies/CSRF correctly. Replace the "
  "domain with your own.")
code(r"""
CSRF_TRUSTED_ORIGINS = ['https://komute.example.com', 'https://www.komute.example.com']

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
""")
warn_("Do not enable SECURE_SSL_REDIRECT / HSTS until HTTPS is actually working end-to-end (Step 11) — "
      "otherwise you can lock yourself out of the site over plain HTTP before the certificate is issued. "
      "Add this block after Step 11 completes, or set SECURE_SSL_REDIRECT = False until then.")

page_break()

# ============================================================================
# 11. STEP 8 - SYSTEMD FOR KOMUTE
# ============================================================================
h1_("11. Step 8 — systemd Service for Komute (Daphne)")
code(r"""
sudo nano /etc/systemd/system/komute.service
""")
code(r"""
[Unit]
Description=Komute Django/Channels app (Daphne ASGI)
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=deploy
Group=deploy
WorkingDirectory=/home/deploy/apps/komute
EnvironmentFile=/home/deploy/apps/komute/.env
ExecStart=/home/deploy/apps/komute/venv/bin/daphne \
    -b 127.0.0.1 -p 8000 config.asgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""")
code(r"""
sudo systemctl daemon-reload
sudo systemctl enable --now komute
sudo systemctl status komute
journalctl -u komute -f     # tail logs
""")

page_break()

# ============================================================================
# 12. STEP 9 - VERIFICATION NODE PACKAGES AND MODELS
# ============================================================================
h1_("12. Step 9 — Install the Verification Node Packages and Models")
p("The verification node is the <font face='Courier'>verification_node/</font> folder in the Komute "
  "repository. It uses the same venv as Komute (Python 3.11). Its packages are listed in "
  "<font face='Courier'>requirements.txt</font>, so the install from Step 6 covers them. Download the "
  "models once (five models, about 50 MB; every file is checked against <font face='Courier'>models.lock</font>):")
code(r"""
cd /var/www/komute
source venv/bin/activate
pip install -r requirements.txt
python verification_node/fetch_models.py
""")
p("Add these lines to <font face='Courier'>/var/www/komute/.env</font>. The node reads only "
  "<font face='Courier'>KOMUTE_*</font> keys and <font face='Courier'>DL_VERIFICATION_API_KEY</font>. "
  "Use the same key value for Komute.")
code(r"""
KOMUTE_ENV=production
KOMUTE_MODELS_DIR=verification_node/backend/models
KOMUTE_LIVENESS_MODEL=verification_node/backend/models/minifasnet_v2.onnx
KOMUTE_REDIS_URL=redis://127.0.0.1:6379/0
DL_VERIFICATION_API_KEY=<long random value>
""")
warn_("The node refuses to start without DL_VERIFICATION_API_KEY (24+ characters in production) and, in production, without the liveness model (MiniFASNetV2, downloaded by fetch_models.py). Do not edit files under verification_node/ on the server: change the node in its own repository and export it again.")

page_break()

# ============================================================================
# 13. STEP 10 - RUN THE VERIFICATION NODE
# ============================================================================
h1_("13. Step 10 — Run the Verification Node and the Map Node (systemd)")
p("Install the unit file that ships with the node (user www-data, working directory "
  "<font face='Courier'>/var/www/komute</font>, port 8080, one worker):")
code(r"""
sudo cp /var/www/komute/verification_node/deploy/komute-verify.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now komute-verify
""")
h2_("Verify the node")
code(r"""
curl http://127.0.0.1:8080/health      # {"status":"ok", ...}
journalctl -u komute-verify -f
""")
note_("Do not use <font face='Courier'>EnvironmentFile</font> in the unit: the node reads "
      "<font face='Courier'>/var/www/komute/.env</font> itself, and systemd parses .env differently.")
h2_("Cutover and rollback")
p("Start <font face='Courier'>komute-verify</font>, then stop the old DL-Verification service. The URL "
  "and key are unchanged. To roll back, stop <font face='Courier'>komute-verify</font> and start the old "
  "service again.")
warn_("The node handles one verification at a time. A second driver at the same moment gets the generic "
      "\"service error\" message.")

h2_("Map node (Apple Maps)")
p('The map node (map/) holds the Apple Maps key and answers address search, routes and the browser map token for the site. It is a third small process in the same venv, on 127.0.0.1:8081, and stores nothing. Its packages come with requirements.txt from Step 6. Add these lines to /var/www/komute/.env:')
code(r"""
APPLE_MAPS_TEAM_ID=<10 characters, Apple Account > Membership>
APPLE_MAPS_KEY_ID=<10 characters, from the key's page>
APPLE_MAPS_PRIVATE_KEY_PATH=/etc/komute/AuthKey_<KEYID>.p8
APPLE_MAPS_WEB_ORIGIN=https://komute.example.com
MAP_ENV=production
MAP_API_URL=http://127.0.0.1:8081
MAP_API_KEY=<24+ random characters, the same value for the site and the node>
MAP_AUTOCOMPLETE=photon
MAP_ROUTES=osrm
MAP_DISPLAY=leaflet
""")
note_("MAP_ENV=production makes the node refuse to start without the Apple key, the web origin and a MAP_API_KEY of 24 characters or more. The map node reads only MAP_ENV, never KOMUTE_ENV, so the two nodes' modes stay separate. APPLE_MAPS_WEB_ORIGIN must be the site's exact origin with https:// and no trailing slash: MapKit refuses the token otherwise.")
p('Keep the Apple .p8 key outside the repository, readable only by root and www-data:')
code(r"""
sudo mkdir -p /etc/komute
sudo cp AuthKey_<KEYID>.p8 /etc/komute/
sudo chown root:www-data /etc/komute/AuthKey_<KEYID>.p8
sudo chmod 640 /etc/komute/AuthKey_<KEYID>.p8
""")
p('Install the unit that ships with the node and check it:')
code(r"""
sudo cp /var/www/komute/map/deploy/komute-map.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now komute-map
curl http://127.0.0.1:8081/health      # {"status":"ok","provider_configured":true}
sudo systemctl restart komute          # the site reads the MAP_* lines at start
""")
note_('The three MAP_* switches start on the old providers (Photon, OSRM, Leaflet). Turn them on one at a time: MAP_AUTOCOMPLETE=apple (address search), MAP_ROUTES=apple (drive time on post and edit ride), MAP_DISPLAY=mapkit (route map on ride summary and booking). Restart komute after each change. To roll back, set a switch back and restart komute. Apple allows 25,000 calls a day per team.')

page_break()

# ============================================================================
# 14. STEP 11 - NGINX + TLS
# ============================================================================
h1_("14. Step 11 — Nginx Reverse Proxy + HTTPS")
p("Only Komute (Daphne on 127.0.0.1:8000) is exposed publicly. Static and media files are served "
  "directly by Nginx for performance, bypassing Django entirely (as the comment in "
  "config/urls.py already suggests for production).")
code(r"""
sudo nano /etc/nginx/sites-available/komute
""")
code(r"""
server {
    listen 80;
    server_name komute.example.com www.komute.example.com;

    # Profile form with license, selfie and vehicle photos. Must equal
    # UPLOAD_REQUEST_MAX_MB in .env (default 40).
    client_max_body_size 40M;
    # Over the limit: show Komute's explanation page instead of Nginx's bare 413.
    error_page 413 /upload-too-large/;

    location = /upload-too-large/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static/ {
        alias /home/deploy/apps/komute/staticfiles/;
    }

    location /media/ {
        alias /home/deploy/apps/komute/media/;
    }

    # Licenses/selfies/vehicle photos: reachable ONLY via Django's
    # X-Accel-Redirect after an owner/staff check, never directly.
    location /protected-media/ {
        internal;
        alias /home/deploy/apps/komute/private_media/;
        add_header X-Content-Type-Options nosniff always;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
""")
note_("The Upgrade/Connection headers above allow the same location block to transparently proxy both "
      "regular HTTP requests and the /ws/chat/&lt;token&gt;/ WebSocket connections that Daphne serves — "
      "no separate location block is required since ProtocolTypeRouter handles both on the same port.")
code(r"""
sudo ln -s /etc/nginx/sites-available/komute /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
""")
h2_("Issue a TLS certificate")
code(r"""
sudo certbot --nginx -d komute.example.com -d www.komute.example.com
""")
p("Certbot edits the Nginx config to redirect HTTP to HTTPS and sets up a systemd timer for automatic "
  "renewal. Confirm the timer is active:")
code(r"""systemctl list-timers | grep certbot""")
p("Now go back and uncomment the SECURE_SSL_REDIRECT / HSTS block from Step 7, restart Komute:")
code(r"""sudo systemctl restart komute""")

page_break()

# ============================================================================
# 15. STEP 12 - CRON
# ============================================================================
h1_("15. Step 12 — Cron Jobs (Scheduled Management Commands)")
p("Two management commands need to run on a schedule: "
  "<font face='Courier'>complete_rides</font> (auto-completes rides past their scheduled time — logs to "
  "logs/cron.log per the LOGGING config in settings.py) and "
  "<font face='Courier'>purge_old_messages</font> (enforces MESSAGE_RETENTION_DAYS).")
code(r"""
crontab -e -u deploy
""")
code(r"""
# Komute cron jobs
*/10 * * * * /home/deploy/apps/komute/venv/bin/python /home/deploy/apps/komute/manage.py complete_rides >> /home/deploy/apps/komute/logs/cron.log 2>&1
0 3 * * * /home/deploy/apps/komute/venv/bin/python /home/deploy/apps/komute/manage.py purge_old_messages >> /home/deploy/apps/komute/logs/cron.log 2>&1
""")
note_("Review komute/management/commands/complete_rides.py and purge_old_messages.py for any "
      "documented recommended interval before finalizing the schedule above — the 10-minute / daily "
      "cadence here is a reasonable starting point, not a hard requirement.")

page_break()

# ============================================================================
# 16. STEP 13 - SMOKE TEST
# ============================================================================
h1_("16. Step 13 — End-to-End Smoke Test")
bullets([
    "<font face='Courier'>systemctl status komute</font> — active (running).",
    "<font face='Courier'>systemctl status komute-verify</font> and <font face='Courier'>curl http://127.0.0.1:8080/health</font> — node healthy.",
    'systemctl status komute-map and curl http://127.0.0.1:8081/health — map node healthy. With MAP_ROUTES=apple, post a ride and check the drive time appears; with MAP_DISPLAY=mapkit, open a ride summary and check the map and route line appear.',
    "Visit <font face='Courier'>https://komute.example.com/</font> — site loads over HTTPS, padlock valid.",
    "Sign up / log in — confirm verification email is sent (SMTP working).",
    "Open two browser sessions in an active ride chat — confirm messages arrive live (WebSocket / "
    "Redis channel layer working).",
    "Log in as a driver, add the front and back of the licence and take a selfie, submit for verification — confirm the "
    "request reaches the verification node and returns a verified/rejected result. If you see "
    "\"Verification service is offline\", check journalctl -u komute-verify.",
    "Download a ride receipt PDF — confirms the reportlab fix from Step 2 took effect.",
    "Check static assets (CSS/images) load correctly — confirms collectstatic + Nginx alias are correct.",
])

page_break()

# ============================================================================
# 17. BACKUPS & MONITORING
# ============================================================================
h1_("17. Backups & Monitoring")
h2_("Database backups")
code(r"""
sudo -u postgres pg_dump komute | gzip > /home/deploy/backups/komute-$(date +%F).sql.gz
""")
p("Add this as a nightly cron job, and copy backups off-droplet (DigitalOcean Spaces, or "
  "<font face='Courier'>rclone</font> to another provider). Consider DigitalOcean Managed PostgreSQL "
  "as a lower-maintenance alternative to self-hosting Postgres, with automated backups built in.")
h2_("Uploaded image backups")
code(r"""
tar czf /home/deploy/backups/komute-private-media-$(date +%F).tar.gz -C /home/deploy/apps/komute private_media
""")
p("Driver's licenses, selfies and vehicle photos live in <font face='Courier'>private_media/</font>, "
  "not in the database. Back the folder up alongside the database dump, and keep those backups "
  "private — they contain identity documents.")
h2_("Droplet-level backups")
p("Enable DigitalOcean's weekly Droplet backups (configured at Droplet creation, or afterwards under "
  "the Droplet's Backups tab) as a full-system safety net.")
h2_("Monitoring")
bullets([
    "Enable the free DigitalOcean Monitoring agent and set alert policies for CPU, memory, and disk — "
    "the verification node is the most likely source of memory pressure.",
    "<font face='Courier'>journalctl -u komute -f</font> and <font face='Courier'>journalctl -u komute-verify -f</font> "
    "for live troubleshooting.",
    "Rotate logs/cron.log (e.g. via logrotate) so it doesn't grow unbounded.",
])

page_break()

# ============================================================================
# 18. UPDATE / REDEPLOY WORKFLOW
# ============================================================================
h1_("18. Update / Redeploy Workflow")
h2_("Komute")
code(r"""
cd /home/deploy/apps/komute
git pull                       # or re-sync via rsync
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart komute
""")
h2_("Verification node")
code(r"""
cd /var/www/komute
git pull                       # includes verification_node/
source venv/bin/activate
pip install -r requirements.txt
python verification_node/fetch_models.py
sudo systemctl restart komute-verify
""")

page_break()

# ============================================================================
# 19. TROUBLESHOOTING
# ============================================================================
h1_("19. Troubleshooting Reference")
table(
    ["Symptom", "Likely cause", "Fix"],
    [
        ["\"Verification service is offline\" error in the app",
         "komute-verify not running, or crashed",
         "systemctl status komute-verify; journalctl -u komute-verify -n 100; systemctl restart komute-verify"],
        ["502 Bad Gateway from Nginx",
         "Daphne / komute.service not running",
         "systemctl status komute; journalctl -u komute -n 100"],
        ["Chat shows \"Not connected – retrying…\"",
         "Redis down, or Nginx missing Upgrade headers",
         "journalctl -u komute | grep komute.chat (refusals and close codes; 1011 = Redis unreachable); "
         "redis-cli ping; re-check the Nginx location block from Step 11"],
        ["Chat says the link is no longer valid / signed out",
         "Chat token older than 7 days (close 4403) or session expired (close 4401)",
         "Expected; open the chat again from the bookings page or sign in again"],
        ["Static files / CSS return 404",
         "collectstatic not run, or Nginx alias path wrong",
         "python manage.py collectstatic --noinput; verify the /static/ alias path matches STATIC_ROOT"],
        ["ModuleNotFoundError: reportlab",
         "Deployed from a repo copy before the Step 2 fix",
         "pip install -r requirements.txt after confirming reportlab is listed"],
        ["komute-verify repeatedly restarting",
         "OOM kill (insufficient droplet RAM) or models missing",
         "check dmesg for OOM; resize droplet to 2 GB RAM or more; rerun python verification_node/fetch_models.py"],
        ["manage.py migrate: permission denied for schema public",
         "PostgreSQL 15+ default schema privilege revocation",
         "Re-run the GRANT ALL ON SCHEMA public TO komute_user; from Step 4"],
    ],
    col_widths=[150, 165, 190],
)

page_break()

# ============================================================================
# APPENDIX
# ============================================================================
h1_("Appendix A — Full Config File Listings")

h2_("A.1 — /etc/systemd/system/komute.service")
code(r"""
[Unit]
Description=Komute Django/Channels app (Daphne ASGI)
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=deploy
Group=deploy
WorkingDirectory=/home/deploy/apps/komute
EnvironmentFile=/home/deploy/apps/komute/.env
ExecStart=/home/deploy/apps/komute/venv/bin/daphne \
    -b 127.0.0.1 -p 8000 config.asgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""")

h2_("A.2 — /etc/nginx/sites-available/komute")
code(r"""
server {
    listen 80;
    server_name komute.example.com www.komute.example.com;
    client_max_body_size 40M;   # = UPLOAD_REQUEST_MAX_MB
    error_page 413 /upload-too-large/;

    location = /upload-too-large/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /static/ {
        alias /home/deploy/apps/komute/staticfiles/;
    }
    location /media/ {
        alias /home/deploy/apps/komute/media/;
    }
    location /protected-media/ {
        internal;
        alias /home/deploy/apps/komute/private_media/;
        add_header X-Content-Type-Options nosniff always;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
# (Certbot appends the HTTPS server block + HTTP->HTTPS redirect automatically)
""")

h2_("A.3 — Komute .env template (production)")
code(r"""
DEBUG=False
ALLOWED_HOSTS=komute.example.com,www.komute.example.com

SECRET_KEY=<generated>
FIELD_ENCRYPTION_KEY=<generated>

DB_NAME=komute
DB_USER=komute_user
DB_PASSWORD=<postgres password>
DB_HOST=localhost
DB_PORT=5432

REDIS_URL=redis://127.0.0.1:6379

EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.hostinger.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=noreply@example.com
EMAIL_HOST_PASSWORD=<secret>

SEARCH_RADIUS_KM=15
SERVICE_FEE_CENTS=100
INVITE_EXPIRY_HOURS=48
MESSAGE_RETENTION_DAYS=365
STOP_DETOUR_MAX_KM=30
STOP_DETOUR_MAX_PCT=35
CONTACT_EMAIL=support@example.com

IMAGE_UPLOAD_MAX_MB=15
# UPLOAD_REQUEST_MAX_MB must equal Nginx client_max_body_size
UPLOAD_REQUEST_MAX_MB=40
""")

h2_("A.4 — crontab (deploy user)")
code(r"""
*/10 * * * * /home/deploy/apps/komute/venv/bin/python /home/deploy/apps/komute/manage.py complete_rides >> /home/deploy/apps/komute/logs/cron.log 2>&1
0 3 * * * /home/deploy/apps/komute/venv/bin/python /home/deploy/apps/komute/manage.py purge_old_messages >> /home/deploy/apps/komute/logs/cron.log 2>&1
""")

hr()
story.append(Spacer(1, 10))
story.append(Paragraph("End of guide.", meta_style))


# ============================================================================
# PDF BUILD (page numbers + PDF outline bookmarks for H1 sections)
# ============================================================================
class NumberedCanvas(canvas_mod.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_footer(num_pages)
            super().showPage()
        super().save()

    def _draw_footer(self, page_count):
        page_num = self._pageNumber
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor('#999999'))
        self.drawRightString(200 * mm, 12 * mm, f"Page {page_num} of {page_count}")
        self.drawString(20 * mm, 12 * mm, "Komute — DigitalOcean Deployment Guide")


def _slugify(text):
    return re.sub(r'[^a-zA-Z0-9]+', '-', text).strip('-').lower()


def _on_page(canvas_obj, doc):
    pass


doc = SimpleDocTemplate(
    OUT_FILE, pagesize=A4,
    leftMargin=20 * mm, rightMargin=20 * mm,
    topMargin=18 * mm, bottomMargin=18 * mm,
    title="Komute - DigitalOcean Deployment Guide",
    author="Komute Ops",
)

# Register outline entries for H1 headings by wrapping story build with afterFlowable
class BookmarkDocTemplate(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == 'H1':
            text = flowable.getPlainText()
            key = _slugify(text)
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=0, closed=False)


doc = BookmarkDocTemplate(
    OUT_FILE, pagesize=A4,
    leftMargin=20 * mm, rightMargin=20 * mm,
    topMargin=18 * mm, bottomMargin=18 * mm,
    title="Komute - DigitalOcean Deployment Guide",
    author="Komute Ops",
)

doc.build(story, canvasmaker=NumberedCanvas)
print(f"Wrote {OUT_FILE}")
