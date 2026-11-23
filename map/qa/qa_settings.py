"""Throwaway Django settings for the qa "before" screenshots. NEVER used by the real site.
Same settings as config/settings.py, but: SQLite file inside map/qa/out, in-memory cache and channel layer, HTTP allowed.
The secrets are random per run and live only in this process; .env is untouched and the real database is never opened.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet

# config.settings reads these at import time (python-decouple), so they must exist before the import below.
os.environ.setdefault("SECRET_KEY", "qa-" + os.urandom(24).hex())
os.environ.setdefault("DB_NAME", "unused")
os.environ.setdefault("DB_USER", "unused")
os.environ.setdefault("DB_PASSWORD", "unused")
os.environ.setdefault("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
    os.environ.setdefault(_k, "unused")  # email is switched to the in-memory backend below; nothing is ever sent
os.environ.setdefault("AWS_SES_REGION", "ca-central-1")
os.environ.setdefault("DEFAULT_FROM_EMAIL", "qa@example.com")
os.environ.setdefault("DEBUG", "true")  # also switches off the HTTPS redirect

from config.settings import *  # noqa: E402,F401,F403

_OUT = Path(__file__).resolve().parent / "out"
_OUT.mkdir(exist_ok=True)
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(_OUT / "qa_db.sqlite3")}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
CSRF_TRUSTED_ORIGINS = ["http://127.0.0.1:8770", "http://localhost:8770"]
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

AXES_ENABLED = False  # the test suite logs in with client.login(), which the lockout backend rejects; one test turns it back on itself
