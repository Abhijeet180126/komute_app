"""
Generates Komute_DigitalOcean_Deployment_Guide.docx
Mirrors generate_deployment_guide.py (PDF) content, styled to match
the existing Komute_Documentation.docx brand (Calibri, green/blue accents).
Run with: venv\\Scripts\\python.exe docs/scripts/generate_deployment_guide_docx.py  (from repo root)
"""
import datetime
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

OUT_FILE = "Komute_DigitalOcean_Deployment_Guide.docx"

GREEN = "0D7810"
BLUE = "0C63B4"
GREY = "5A7060"
CODE_BG = "EFF6FF"
NOTE_BG = "FFF6DA"
NOTE_BORDER = "E8C468"
WARN_BG = "FDEAEA"
WARN_BORDER = "D9534F"

doc = Document()

section = doc.sections[0]
section.page_width = Inches(8.5)
section.page_height = Inches(11)
section.left_margin = section.right_margin = Inches(1)
section.top_margin = section.bottom_margin = Inches(1)


def add_heading(text, level=1, color=GREEN):
    p = doc.add_heading(text, level=level)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for run in p.runs:
        run.font.color.rgb = RGBColor.from_string(color)
        run.font.name = "Calibri"
    return p


def add_para(text, bold=False, italic=False, size=10.5, color=None, indent=False, align=None):
    p = doc.add_paragraph()
    if align:
        p.alignment = align
    run = p.add_run(text)
    run.font.name = "Calibri"
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    if indent:
        p.paragraph_format.left_indent = Inches(0.3)
    return p


def add_bullet(text, level=0, numbered=False):
    p = doc.add_paragraph(style="List Number" if numbered else "List Bullet")
    run = p.add_run(text)
    run.font.name = "Calibri"
    run.font.size = Pt(10.5)
    p.paragraph_format.left_indent = Inches(0.3 * (level + 1))
    return p


def add_bullets(items, numbered=False):
    for it in items:
        add_bullet(it, numbered=numbered)


def _shade(paragraph_or_cell_xml, fill):
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), fill)
    paragraph_or_cell_xml.append(shd)


def add_table(headers, rows, col_widths=None):
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT

    hdr_cells = table.rows[0].cells
    for i, h in enumerate(headers):
        hdr_cells[i].text = h
        r = hdr_cells[i].paragraphs[0].runs[0]
        r.font.bold = True
        r.font.name = "Calibri"
        r.font.size = Pt(9.5)
        r.font.color.rgb = RGBColor(255, 255, 255)
        tcPr = hdr_cells[i]._tc.get_or_add_tcPr()
        _shade(tcPr, GREEN)

    for ri, row in enumerate(rows):
        cells = table.rows[ri + 1].cells
        for ci, val in enumerate(row):
            cells[ci].text = str(val)
            r = cells[ci].paragraphs[0].runs[0]
            r.font.name = "Calibri"
            r.font.size = Pt(9.5)
            if ri % 2 == 1:
                tcPr = cells[ci]._tc.get_or_add_tcPr()
                _shade(tcPr, "F7F7F7")

    if col_widths:
        for i, w in enumerate(col_widths):
            for row in table.rows:
                row.cells[i].width = Inches(w)
    doc.add_paragraph()
    return table


def add_code(code_text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.3)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(4)
    lines = code_text.strip("\n").split("\n")
    for i, line in enumerate(lines):
        run = p.add_run(line + ("\n" if i < len(lines) - 1 else ""))
        run.font.name = "Courier New"
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor.from_string(BLUE)
    _shade(p._p.get_or_add_pPr(), CODE_BG)
    return p


def add_callout(text, label, bg, border):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.15)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(6)
    run1 = p.add_run(f"{label}: ")
    run1.font.bold = True
    run1.font.name = "Calibri"
    run1.font.size = Pt(10)
    run2 = p.add_run(text)
    run2.font.name = "Calibri"
    run2.font.size = Pt(10)
    _shade(p._p.get_or_add_pPr(), bg)
    return p


def add_note(text):
    return add_callout(text, "NOTE", NOTE_BG, NOTE_BORDER)


def add_warn(text):
    return add_callout(text, "IMPORTANT", WARN_BG, WARN_BORDER)


def page_break():
    doc.add_page_break()


# ============================================================================
# COVER PAGE
# ============================================================================
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("\n\n\n\nKomüte")
run.font.name = "Calibri"
run.font.size = Pt(42)
run.font.bold = True
run.font.color.rgb = RGBColor.from_string(GREEN)

p2 = doc.add_paragraph()
p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
r2 = p2.add_run("Production Deployment Guide — DigitalOcean")
r2.font.name = "Calibri"
r2.font.size = Pt(16)
r2.font.color.rgb = RGBColor.from_string(BLUE)

p2b = doc.add_paragraph()
p2b.alignment = WD_ALIGN_PARAGRAPH.CENTER
r2b = p2b.add_run("Django/Channels ride-sharing platform + verification node (driver verification)")
r2b.font.name = "Calibri"
r2b.font.size = Pt(12)
r2b.font.color.rgb = RGBColor.from_string(GREY)

doc.add_paragraph()
p3 = doc.add_paragraph()
p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
r3 = p3.add_run(f"Version 1.0  ·  {datetime.date.today().strftime('%B %Y')}")
r3.font.name = "Calibri"
r3.font.size = Pt(11)
r3.font.color.rgb = RGBColor.from_string(GREY)

p4 = doc.add_paragraph()
p4.alignment = WD_ALIGN_PARAGRAPH.CENTER
r4 = p4.add_run("Scope: KomuteProject (Django) and its verification node (verification_node/, Python ML)\nrun on one Ubuntu 22.04 droplet")
r4.font.name = "Calibri"
r4.font.size = Pt(10)
r4.font.color.rgb = RGBColor.from_string(GREY)
r4.font.italic = True

page_break()

# ============================================================================
# TOC
# ============================================================================
add_heading("Table of Contents", level=1)
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
    "13. Step 10 — Run the Verification Node (systemd)",
    "14. Step 11 — Nginx Reverse Proxy + HTTPS",
    "15. Step 12 — Cron Jobs (Scheduled Management Commands)",
    "16. Step 13 — End-to-End Smoke Test",
    "17. Backups & Monitoring",
    "18. Update / Redeploy Workflow",
    "19. Troubleshooting Reference",
    "Appendix A — Full Config File Listings",
]
add_bullets(toc_items)
page_break()

# ============================================================================
# 1. ARCHITECTURE
# ============================================================================
add_heading("1. Architecture Overview", level=1)
add_para(
    "Komute is a Django 5.2 app served over ASGI (Django Channels + Daphne) for WebSocket chat, backed by "
    "PostgreSQL and Redis. Driver identity verification runs in the verification node "
    "(verification_node/), a second Python process in the same virtual environment. It stores nothing: "
    "Komute keeps only the outcome."
)
add_warn(
    "Komute calls the node at DL_VERIFICATION_API_URL (default http://127.0.0.1:8080/api/v1/verify), "
    "with the key in DL_VERIFICATION_API_KEY. The node needs 2 GB of RAM or more and runs one "
    "verification at a time. This guide runs it on the same droplet. To use a separate machine, change "
    "the URL, put TLS in front of the node and bind it to a private address."
)
add_para("Recommended process topology on the single droplet:")
add_code(r"""
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
add_para(
    "One process manager. Komute and the verification node both run natively under systemd, in the same "
    "Python venv. There is no Docker. The node's models are ONNX files that fetch_models.py downloads and "
    "checks against a hash list."
)
page_break()

# ============================================================================
# 2. PRE-DEPLOYMENT FIXES
# ============================================================================
add_heading("2. Pre-Deployment Fixes Already Applied", level=1)
add_para(
    "While preparing this guide, two production-blocking gaps were found in KomuteProject and have been "
    "fixed directly in the repository so that the steps below work without surprises:"
)
add_table(
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
    col_widths=[1.1, 2.6, 2.5],
)
add_note(
    "These two fixes are already committed to the working tree. If you deploy from a different copy of "
    "the repo (e.g. an older git history), re-apply them before continuing, or diff against this copy."
)
add_para(
    "Two further production-readiness items are not auto-fixed because they require values only you know "
    "(your real domain name) and a decision about strictness — they are covered explicitly in Step 7 "
    "(Production Settings Hardening) below: CSRF_TRUSTED_ORIGINS and secure-cookie/HTTPS settings."
)
page_break()

# ============================================================================
# 3. PREREQUISITES
# ============================================================================
add_heading("3. Prerequisites", level=1)
add_bullets([
    "A DigitalOcean account with billing enabled.",
    "A registered domain name you can point at the droplet (e.g. komute.example.com).",
    "An SSH key pair on your local machine (ssh-keygen -t ed25519).",
    "The Komute codebase (it includes verification_node/), ready to transfer to the server "
    "(via git push to a private repo, or scp/rsync).",
    "SMTP credentials for transactional email (already required by Komute's .env — see README.md).",
])
add_heading("Droplet Sizing", level=2, color=BLUE)
add_para(
    "The verification node loads ONNX models (face, plate, barcode) into memory and runs CPU-only "
    "inference (no GPU needed). One verification peaks near 1.2 GB. The node needs 2 GB of RAM or more "
    "on top of Postgres, Redis and Daphne. Undersizing this will cause OOM kills."
)
add_table(
    ["Plan", "vCPU / RAM", "Suitability"],
    [
        ["Basic Premium AMD, 4 GB", "2 vCPU / 4 GB", "Minimum viable — acceptable for staging/low traffic. Watch memory closely."],
        ["Basic Premium AMD, 8 GB (recommended)", "4 vCPU / 8 GB", "Comfortable headroom for Postgres + Redis + Daphne + the verification node."],
    ],
    col_widths=[2.3, 1.4, 2.5],
)
add_bullets([
    "OS image: Ubuntu 22.04 LTS x64 (used throughout this guide).",
    "Enable weekly backups or snapshots (small extra cost, cheap insurance).",
    "25 GB+ SSD disk recommended — the node's models take about 180 MB.",
])
page_break()

# ============================================================================
# 4. STEP 1
# ============================================================================
add_heading("4. Step 1 — Create the DigitalOcean Droplet", level=1)
add_bullets([
    "Log in to the DigitalOcean control panel → Create → Droplets.",
    "Choose image: Ubuntu 22.04 (LTS) x64.",
    "Choose plan: Basic → Premium AMD/Intel → 8 GB RAM / 4 vCPU (see sizing table above).",
    "Choose a datacenter region close to your users.",
    "Authentication: select SSH Key and add your public key (avoid password auth entirely).",
    "Hostname: e.g. komute-prod-01.",
    "Enable backups (recommended).",
    "Click Create Droplet and note the assigned public IPv4 address.",
], numbered=True)
add_heading("DNS", level=2, color=BLUE)
add_para(
    "In your domain registrar / DNS provider, create an A record pointing your domain (and www, if used) "
    "at the droplet's public IP:"
)
add_code(r"""
Type   Name              Value              TTL
A      komute            <droplet-ip>       3600
A      www.komute        <droplet-ip>       3600
""")
page_break()

# ============================================================================
# 5. STEP 2
# ============================================================================
add_heading("5. Step 2 — Initial Server Hardening", level=1)
add_para("SSH in as root the first time only, to create a non-root deploy user:")
add_code(r"""
ssh root@<droplet-ip>

adduser deploy
usermod -aG sudo deploy

# Copy your SSH key to the new user
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy

exit
""")
add_para("From now on, connect as the deploy user:")
add_code("ssh deploy@<droplet-ip>")

add_heading("Disable root login and password auth", level=2, color=BLUE)
add_code(r"""
sudo nano /etc/ssh/sshd_config
# set:
#   PermitRootLogin no
#   PasswordAuthentication no

sudo systemctl restart ssh
""")

add_heading("Firewall (UFW)", level=2, color=BLUE)
add_para(
    "Only SSH, HTTP and HTTPS are exposed to the internet. Postgres (5432), Redis (6379) and the verification node "
    "(8080) all stay bound to localhost/loopback and are never opened in the firewall."
)
add_code(r"""
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
""")

add_heading("System updates", level=2, color=BLUE)
add_code(r"""
sudo apt update && sudo apt -y upgrade
sudo timedatectl set-timezone UTC
sudo apt -y install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
""")
page_break()

# ============================================================================
# 6. STEP 3
# ============================================================================
add_heading("6. Step 3 — Install System Dependencies", level=1)

add_heading("Python 3.11, build tools, Nginx, PostgreSQL client, Redis", level=2, color=BLUE)
add_code(r"""
sudo apt update
sudo apt install -y software-properties-common build-essential git curl \
    python3.11 python3.11-venv python3.11-dev \
    libpq-dev nginx redis-server postgresql postgresql-contrib \
    certbot python3-certbot-nginx
""")

add_heading("Confirm Redis is running", level=2, color=BLUE)
add_code(r"""
sudo systemctl enable --now redis-server
redis-cli ping     # expect: PONG
""")
page_break()

# ============================================================================
# 7. STEP 4
# ============================================================================
add_heading("7. Step 4 — PostgreSQL Setup", level=1)
add_code(r"""
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
add_note(
    "PostgreSQL 15+ revokes default CREATE privileges on the public schema from new users — the "
    "GRANT ALL ON SCHEMA public line above is required or manage.py migrate will fail with a permission "
    "error."
)
add_para(
    "Confirm the database accepts local connections (default pg_hba.conf on Ubuntu uses peer/md5 for "
    "local — no change needed unless you customized it)."
)
page_break()

# ============================================================================
# 8. STEP 5
# ============================================================================
add_heading("8. Step 5 — Redis Setup", level=1)
add_para(
    "Redis backs the Django Channels layer (WebSocket chat fan-out) and the shared cache used for "
    "rate limiting. Use this local redis-server, not a free hosted plan: Upstash deletes free "
    "databases after 14 days of inactivity, which silently breaks chat. Default install binds to "
    "127.0.0.1 only, which is correct — do not expose it externally."
)
add_code(r"""
sudo systemctl status redis-server
redis-cli ping    # PONG

# Confirm it only listens on localhost and protected mode is on
grep -E '^(bind|protected-mode)' /etc/redis/redis.conf
#   bind 127.0.0.1 -::1
#   protected-mode yes
sudo ss -ltnp | grep 6379     # only 127.0.0.1:6379 / [::1]:6379, never 0.0.0.0
sudo ufw status | grep 6379   # no output: the port is not opened in UFW
""")
add_para(
    "Keep REDIS_URL=redis://127.0.0.1:6379 in .env. If a managed Redis is ever used instead, it "
    "must be a paid plan with a rediss:// (TLS) URL; Komute verifies its certificate."
)
add_para("Optional hardening — set a password:")
add_code(r"""
sudo nano /etc/redis/redis.conf
# requirepass  <strong-redis-password>

sudo systemctl restart redis-server
""")
add_para("If you set a password, update REDIS_URL in Komute's .env accordingly:")
add_code("REDIS_URL=redis://:<strong-redis-password>@127.0.0.1:6379")
page_break()

# ============================================================================
# 9. STEP 6
# ============================================================================
add_heading("9. Step 6 — Deploy the Komute Django App", level=1)

add_heading("Transfer the code", level=2, color=BLUE)
add_para(
    "This project is not currently a git repository. Either initialize one and push to a private GitHub/"
    "GitLab repo (recommended, makes future redeploys a one-line git pull), or transfer the files "
    "directly with rsync:"
)
add_code(r"""
# Option A - from your local machine, over rsync (excludes venv/pycache/db.sqlite3)
rsync -avz --exclude 'venv' --exclude '__pycache__' --exclude 'db.sqlite3' \
    "./KomuteProject/" deploy@<droplet-ip>:/home/deploy/apps/komute/

# Option B - if you push the project to a private git remote
ssh deploy@<droplet-ip>
mkdir -p /home/deploy/apps && cd /home/deploy/apps
git clone <your-private-repo-url> komute
""")

add_heading("Create the virtualenv and install dependencies", level=2, color=BLUE)
add_code(r"""
cd /home/deploy/apps/komute
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
""")

add_heading("Create the production .env", level=2, color=BLUE)
add_para(
    "Copy the structure documented in README.md. Generate fresh secrets for production — do not reuse "
    "development secrets, with one exception noted below."
)
add_code(r"""
# Generate a new Django SECRET_KEY
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# Generate a Fernet key for FIELD_ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
""")
add_warn(
    "FIELD_ENCRYPTION_KEY encrypts biometric/OCR fields at rest (see django-encrypted-model-fields in "
    "models.py). If you are migrating an existing production database with real encrypted data, you must "
    "reuse the exact same key used to write that data, or those fields become permanently unreadable. "
    "Only generate a fresh key for a brand-new database."
)
add_code("nano /home/deploy/apps/komute/.env")
add_table(
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
    col_widths=[2.7, 3.0],
)

add_heading("Migrate, collect static, create admin user", level=2, color=BLUE)
add_code(r"""
mkdir -p logs media staticfiles
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
""")

add_heading("Smoke-test Daphne manually before wiring systemd", level=2, color=BLUE)
add_code(r"""
python -m daphne -b 127.0.0.1 -p 8000 config.asgi:application
# In another shell: curl -I http://127.0.0.1:8000/
# Ctrl+C to stop once confirmed
""")
page_break()

# ============================================================================
# 10. STEP 7
# ============================================================================
add_heading("10. Step 7 — Production Settings Hardening (settings.py)", level=1)
add_para(
    "Nginx will terminate TLS and proxy plain HTTP to Daphne on 127.0.0.1:8000. Add the following to "
    "config/settings.py (below the existing STATIC_ROOT block is a good spot) so Django knows it is being "
    "served over HTTPS and enforces secure cookies/CSRF correctly. Replace the domain with your own."
)
add_code(r"""
CSRF_TRUSTED_ORIGINS = ['https://komute.example.com', 'https://www.komute.example.com']

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
""")
add_warn(
    "Do not enable SECURE_SSL_REDIRECT / HSTS until HTTPS is actually working end-to-end (Step 11) — "
    "otherwise you can lock yourself out of the site over plain HTTP before the certificate is issued. "
    "Add this block after Step 11 completes, or set SECURE_SSL_REDIRECT = False until then."
)
page_break()

# ============================================================================
# 11. STEP 8
# ============================================================================
add_heading("11. Step 8 — systemd Service for Komute (Daphne)", level=1)
add_code("sudo nano /etc/systemd/system/komute.service")
add_code(r"""
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
add_code(r"""
sudo systemctl daemon-reload
sudo systemctl enable --now komute
sudo systemctl status komute
journalctl -u komute -f     # tail logs
""")
page_break()

# ============================================================================
# 12. STEP 9
# ============================================================================
add_heading("12. Step 9 — Install the Verification Node Packages and Models", level=1)
add_para(
    "The verification node is the verification_node/ folder in the Komute repository. It uses the same venv "
    "as Komute (Python 3.11). Its packages are listed in requirements.txt, so the install from Step 6 covers "
    "them. Download the models once (about 180 MB; every file is checked against models.lock):"
)
add_code(r"""
cd /var/www/komute
source venv/bin/activate
pip install -r requirements.txt
python verification_node/fetch_models.py
""")
add_para(
    "Add these lines to /var/www/komute/.env. The node reads only KOMUTE_* keys and "
    "DL_VERIFICATION_API_KEY. Use the same key value for Komute."
)
add_code(r"""
KOMUTE_ENV=dev
KOMUTE_DEV_SKIP_LIVENESS=true
KOMUTE_MODELS_DIR=verification_node/backend/models
DL_VERIFICATION_API_KEY=<long random value>
""")
add_warn(
    "No liveness model exists yet. KOMUTE_ENV=production refuses to start without one, so the node runs "
    "with KOMUTE_ENV=dev and liveness skipped until a model is supplied. Do not edit files under "
    "verification_node/ on the server: change the node in its own repository and export it again."
)
page_break()

# ============================================================================
# 13. STEP 10
# ============================================================================
add_heading("13. Step 10 — Run the Verification Node (systemd)", level=1)
add_para(
    "Install the unit file that ships with the node (user www-data, working directory /var/www/komute, "
    "port 8080, one worker):"
)
add_code(r"""
sudo cp /var/www/komute/verification_node/deploy/komute-verify.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now komute-verify
""")
add_heading("Verify the node", level=2, color=BLUE)
add_code(r"""
curl http://127.0.0.1:8080/health      # {"status":"ok", ...}
journalctl -u komute-verify -f
""")
add_note(
    "Do not use EnvironmentFile in the unit: the node reads /var/www/komute/.env itself, and systemd "
    "parses .env differently."
)
add_heading("Cutover and rollback", level=2, color=BLUE)
add_para(
    "Start komute-verify, then stop the old DL-Verification service. The URL and key are unchanged. "
    "To roll back, stop komute-verify and start the old service again."
)
add_warn(
    "The node handles one verification at a time. A second driver at the same moment gets the generic "
    "\"service error\" message."
)
page_break()

# ============================================================================
# 14. STEP 11
# ============================================================================
add_heading("14. Step 11 — Nginx Reverse Proxy + HTTPS", level=1)
add_para(
    "Only Komute (Daphne on 127.0.0.1:8000) is exposed publicly. Static and media files are served "
    "directly by Nginx for performance, bypassing Django entirely (as the comment in config/urls.py "
    "already suggests for production)."
)
add_code("sudo nano /etc/nginx/sites-available/komute")
add_code(r"""
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
add_note(
    "The Upgrade/Connection headers above allow the same location block to transparently proxy both "
    "regular HTTP requests and the /ws/chat/<token>/ WebSocket connections that Daphne serves — no "
    "separate location block is required since ProtocolTypeRouter handles both on the same port."
)
add_code(r"""
sudo ln -s /etc/nginx/sites-available/komute /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
""")
add_heading("Issue a TLS certificate", level=2, color=BLUE)
add_code("sudo certbot --nginx -d komute.example.com -d www.komute.example.com")
add_para(
    "Certbot edits the Nginx config to redirect HTTP to HTTPS and sets up a systemd timer for automatic "
    "renewal. Confirm the timer is active:"
)
add_code("systemctl list-timers | grep certbot")
add_para("Now go back and uncomment the SECURE_SSL_REDIRECT / HSTS block from Step 7, restart Komute:")
add_code("sudo systemctl restart komute")
page_break()

# ============================================================================
# 15. STEP 12
# ============================================================================
add_heading("15. Step 12 — Cron Jobs (Scheduled Management Commands)", level=1)
add_para(
    "Two management commands need to run on a schedule: complete_rides (auto-completes rides past their "
    "scheduled time — logs to logs/cron.log per the LOGGING config in settings.py) and "
    "purge_old_messages (enforces MESSAGE_RETENTION_DAYS)."
)
add_code("crontab -e -u deploy")
add_code(r"""
# Komute cron jobs
*/10 * * * * /home/deploy/apps/komute/venv/bin/python /home/deploy/apps/komute/manage.py complete_rides >> /home/deploy/apps/komute/logs/cron.log 2>&1
0 3 * * * /home/deploy/apps/komute/venv/bin/python /home/deploy/apps/komute/manage.py purge_old_messages >> /home/deploy/apps/komute/logs/cron.log 2>&1
""")
add_note(
    "Review komute/management/commands/complete_rides.py and purge_old_messages.py for any documented "
    "recommended interval before finalizing the schedule above — the 10-minute / daily cadence here is a "
    "reasonable starting point, not a hard requirement."
)
page_break()

# ============================================================================
# 16. STEP 13
# ============================================================================
add_heading("16. Step 13 — End-to-End Smoke Test", level=1)
add_bullets([
    "systemctl status komute — active (running).",
    "systemctl status komute-verify and curl http://127.0.0.1:8080/health — node healthy.",
    "Visit https://komute.example.com/ — site loads over HTTPS, padlock valid.",
    "Sign up / log in — confirm verification email is sent (SMTP working).",
    "Open two browser sessions in an active ride chat — confirm messages arrive live (WebSocket / Redis "
    "channel layer working).",
    "Log in as a driver, upload a license photo + take a selfie, submit for verification — confirm the "
    "request reaches the verification node and returns a verified/rejected result. If you see "
    "\"Verification service is offline\", check journalctl -u komute-verify.",
    "Download a ride receipt PDF — confirms the reportlab fix from Step 2 took effect.",
    "Check static assets (CSS/images) load correctly — confirms collectstatic + Nginx alias are correct.",
])
page_break()

# ============================================================================
# 17. BACKUPS & MONITORING
# ============================================================================
add_heading("17. Backups & Monitoring", level=1)
add_heading("Database backups", level=2, color=BLUE)
add_code("sudo -u postgres pg_dump komute | gzip > /home/deploy/backups/komute-$(date +%F).sql.gz")
add_para(
    "Add this as a nightly cron job, and copy backups off-droplet (DigitalOcean Spaces, or rclone to "
    "another provider). Consider DigitalOcean Managed PostgreSQL as a lower-maintenance alternative to "
    "self-hosting Postgres, with automated backups built in."
)
add_heading("Uploaded image backups", level=2, color=BLUE)
add_code("tar czf /home/deploy/backups/komute-private-media-$(date +%F).tar.gz -C /home/deploy/apps/komute private_media")
add_para(
    "Driver's licenses, selfies and vehicle photos live in private_media/, not in the database. "
    "Back the folder up alongside the database dump, and keep those backups private — they contain "
    "identity documents."
)
add_heading("Droplet-level backups", level=2, color=BLUE)
add_para(
    "Enable DigitalOcean's weekly Droplet backups (configured at Droplet creation, or afterwards under "
    "the Droplet's Backups tab) as a full-system safety net."
)
add_heading("Monitoring", level=2, color=BLUE)
add_bullets([
    "Enable the free DigitalOcean Monitoring agent and set alert policies for CPU, memory, and disk — "
    "the verification node is the most likely source of memory pressure.",
    "journalctl -u komute -f and journalctl -u komute-verify -f for live troubleshooting.",
    "Rotate logs/cron.log (e.g. via logrotate) so it doesn't grow unbounded.",
])
page_break()

# ============================================================================
# 18. UPDATE / REDEPLOY
# ============================================================================
add_heading("18. Update / Redeploy Workflow", level=1)
add_heading("Komute", level=2, color=BLUE)
add_code(r"""
cd /home/deploy/apps/komute
git pull                       # or re-sync via rsync
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart komute
""")
add_heading("Verification node", level=2, color=BLUE)
add_code(r"""
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
add_heading("19. Troubleshooting Reference", level=1)
add_table(
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
    col_widths=[1.9, 1.9, 2.4],
)
page_break()

# ============================================================================
# APPENDIX
# ============================================================================
add_heading("Appendix A — Full Config File Listings", level=1)

add_heading("A.1 — /etc/systemd/system/komute.service", level=2, color=BLUE)
add_code(r"""
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

add_heading("A.2 — /etc/nginx/sites-available/komute", level=2, color=BLUE)
add_code(r"""
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

add_heading("A.3 — Komute .env template (production)", level=2, color=BLUE)
add_code(r"""
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

add_heading("A.4 — crontab (deploy user)", level=2, color=BLUE)
add_code(r"""
*/10 * * * * /home/deploy/apps/komute/venv/bin/python /home/deploy/apps/komute/manage.py complete_rides >> /home/deploy/apps/komute/logs/cron.log 2>&1
0 3 * * * /home/deploy/apps/komute/venv/bin/python /home/deploy/apps/komute/manage.py purge_old_messages >> /home/deploy/apps/komute/logs/cron.log 2>&1
""")

doc.add_paragraph()
p_end = doc.add_paragraph()
p_end.alignment = WD_ALIGN_PARAGRAPH.CENTER
r_end = p_end.add_run("End of guide.")
r_end.font.name = "Calibri"
r_end.font.size = Pt(9)
r_end.font.color.rgb = RGBColor.from_string(GREY)
r_end.font.italic = True

doc.save(OUT_FILE)
print(f"Wrote {OUT_FILE}")
