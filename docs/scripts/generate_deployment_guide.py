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
story.append(Paragraph("Django/Channels ride-sharing platform + DL-Verification driver-verification service", subtitle_style))
story.append(Spacer(1, 10 * mm))
story.append(Paragraph("Prepared for: Ops / Deployment Team", meta_style))
story.append(Paragraph("Scope: KomuteProject (Django) and DL-Verification-main (Node.js + Python ML) co-deployed on a single Ubuntu 22.04 droplet", meta_style))
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
    "12. Step 9 — Install Docker & Docker Compose",
    "13. Step 10 — Deploy DL-Verification (Node API + Python Face API)",
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
  "PostgreSQL and Redis. Driver identity verification is delegated to a separate project, "
  "<b>DL-Verification-main</b>, which is a two-service system (Node.js orchestration API + Python Flask "
  "face/OCR ML API).")
warn_("The Django view <font face='Courier'>verify_driver_view</font> "
      "(komute/views.py) calls the verification service at the hardcoded address "
      "<font face='Courier'>http://localhost:3000/api/verify</font>. This is not configurable via an "
      "environment variable. As a direct consequence, <b>the DL-Verification Node API must run on the same "
      "host as Komute, on port 3000</b> — it cannot be deployed on a separate droplet without a code change. "
      "This guide deploys both projects on one droplet to match this constraint.")

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

Komute view verify_driver_view() -----> http://localhost:3000/api/verify
                                              |
                                              v
                              +----------------------------------+
                              | node-api  (Express, port 3000)    |
                              | Docker container [docker compose] |
                              +----------------------------------+
                                              |
                                              | http://python-face-api:5001
                                              v                (docker network)
                              +----------------------------------+
                              | python-face-api (Flask, port 5001)|
                              | Docker container [docker compose] |
                              | InsightFace (ArcFace) + PaddleOCR |
                              +----------------------------------+

Ports 3000 and 5001 are published to 127.0.0.1 only (not exposed to the
internet) - only Nginx (80/443) is internet-facing, enforced via UFW.
""")

p("<b>Why two different process managers (systemd vs. Docker Compose)?</b> Komute is a plain Django/ASGI "
  "app with no Dockerfile in the repo, so it is run natively under systemd + a Python venv — this is simpler "
  "to debug and keeps direct filesystem access to media/log files. DL-Verification already ships tested "
  "Dockerfiles and a <font face='Courier'>docker-compose.yml</font> for its two services, whose native "
  "dependencies (InsightFace, ONNX Runtime, PaddleOCR, dlib toolchain) are heavy and fragile to install by "
  "hand — Docker Compose is the path of least resistance and reuses what the project already provides.")

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
    "The Komute codebase and the DL-Verification-main codebase, ready to transfer to the server "
    "(via git push to a private repo, or scp/rsync).",
    "SMTP credentials for transactional email (already required by Komute's .env — see README.md).",
])
h2_("Droplet Sizing")
p("The Python Face API loads InsightFace (ArcFace, buffalo_l model) and PaddleOCR into memory and runs "
  "CPU-only inference (no GPU needed). These libraries are RAM-hungry at import time (model weights + "
  "OpenBLAS/ONNX runtime). Undersizing this will cause OOM kills.")
table(
    ["Plan", "vCPU / RAM", "Suitability"],
    [
        ["Basic Premium AMD, 4 GB", "2 vCPU / 4 GB", "Minimum viable — acceptable for staging/low traffic. Watch memory closely."],
        ["Basic Premium AMD, 8 GB (recommended)", "4 vCPU / 8 GB", "Comfortable headroom for Postgres + Redis + Daphne + the two ML containers running concurrently."],
    ],
    col_widths=[170, 110, 225],
)
bullets([
    "OS image: <b>Ubuntu 22.04 LTS x64</b> (used throughout this guide).",
    "Enable weekly backups or snapshots (small extra cost, cheap insurance).",
    "50 GB+ SSD disk recommended — PaddleOCR/InsightFace model downloads plus Docker images consume "
    "several GB.",
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
p("Only SSH, HTTP and HTTPS are exposed to the internet. Postgres (5432), Redis (6379), the Node API "
  "(3000) and the Python Face API (5001) all stay bound to localhost/loopback and are never opened in "
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

h2_("Node.js 20.x (required to run the DL-Verification stack via Docker, and useful for local checks)")
code(r"""
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node -v   # expect v20.x
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
p("Redis backs the Django Channels layer (WebSocket chat fan-out). Default install binds to "
  "127.0.0.1 only, which is correct — do not expose it externally.")
code(r"""
sudo systemctl status redis-server
redis-cli ping    # PONG
""")
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
# 12. STEP 9 - DOCKER
# ============================================================================
h1_("12. Step 9 — Install Docker & Docker Compose")
code(r"""
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker deploy
newgrp docker   # or log out/in to pick up the group change

docker --version
docker compose version
""")

page_break()

# ============================================================================
# 13. STEP 10 - DL-VERIFICATION
# ============================================================================
h1_("13. Step 10 — Deploy DL-Verification (Node API + Python Face API)")
p("Transfer DL-Verification-main to the server the same way as Komute (rsync or git). Only the "
  "backend/ folder (node-api + python-face-api) is required for the Komute integration — the frontend/ "
  "React app is <b>not</b> called by Django and can be skipped for this deployment.")
code(r"""
rsync -avz --exclude 'node_modules' --exclude 'venv' --exclude '__pycache__' \
    "./DL-Verification-main/" deploy@<droplet-ip>:/home/deploy/apps/dl-verification/
""")
h2_("Build and start the stack")
p("docker-compose.yml (at the DL-Verification-main root) already defines both services, publishes "
  "ports 3000 and 5001 to the host, wires the internal Docker network so node-api reaches "
  "python-face-api by container name, and sets restart: unless-stopped.")
code(r"""
cd /home/deploy/apps/dl-verification
docker compose build
docker compose up -d
docker compose ps
""")
note_("First startup downloads the InsightFace buffalo_l model and PaddleOCR model weights from the "
      "internet (several hundred MB total). This can take a few minutes on first boot — watch "
      "<font face='Courier'>docker compose logs -f python-face-api</font> until you see the server "
      "report it's ready, before testing.")
h2_("Verify both services")
code(r"""
curl http://127.0.0.1:5001/health
curl http://127.0.0.1:3000/api/health
""")
h2_("Keep the stack running across reboots")
p("Container restart policies (<font face='Courier'>unless-stopped</font>) bring containers back up "
  "automatically once the Docker daemon restarts, and the Docker daemon itself is enabled by default. "
  "Confirm:")
code(r"""sudo systemctl enable docker""")
warn_("Ports 3000 and 5001 are bound by Docker's default port publishing, which listens on all "
      "interfaces (0.0.0.0) unless restricted. Since UFW is enabled and does not explicitly allow 3000/"
      "5001, external traffic to those ports is blocked at the firewall — but be aware Docker manipulates "
      "iptables directly and can bypass UFW rules in some configurations. Confirm from outside the "
      "droplet that 3000 and 5001 are unreachable (e.g. <font face='Courier'>curl http://&lt;droplet-ip&gt;:3000</font> "
      "from your laptop should time out) before going live. If it does not, bind the compose ports "
      "explicitly to loopback in docker-compose.yml, e.g. <font face='Courier'>\"127.0.0.1:3000:3000\"</font> "
      "instead of <font face='Courier'>\"3000:3000\"</font>.")

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
    "<font face='Courier'>docker compose ps</font> (in dl-verification dir) — both containers healthy.",
    "Visit <font face='Courier'>https://komute.example.com/</font> — site loads over HTTPS, padlock valid.",
    "Sign up / log in — confirm verification email is sent (SMTP working).",
    "Open two browser sessions in an active ride chat — confirm messages arrive live (WebSocket / "
    "Redis channel layer working).",
    "Log in as a driver, upload a license photo + take a selfie, submit for verification — confirm the "
    "request reaches Node API → Python Face API and returns a verified/rejected result. If you see "
    "\"Verification service is offline\", check docker compose logs.",
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
    "the ML containers are the most likely source of memory pressure.",
    "<font face='Courier'>journalctl -u komute -f</font> and <font face='Courier'>docker compose logs -f</font> "
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
h2_("DL-Verification")
code(r"""
cd /home/deploy/apps/dl-verification
git pull                       # or re-sync via rsync
docker compose build
docker compose up -d
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
         "node-api container not running, or crashed",
         "docker compose ps; docker compose logs node-api; docker compose restart node-api"],
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
        ["python-face-api container repeatedly restarting",
         "OOM kill (insufficient droplet RAM) or model download interrupted",
         "docker stats; check dmesg for OOM; resize droplet or free memory; docker compose logs python-face-api"],
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
