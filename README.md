# Komute

A Django-based carpooling platform.

## Environment Variables

Copy `.env` and fill in values. All variables are required unless a default is noted.

### Email (SMTP)

| Variable | Description | Example |
|---|---|---|
| `EMAIL_BACKEND` | Django email backend | `django.core.mail.backends.smtp.EmailBackend` |
| `EMAIL_HOST` | SMTP server hostname | `smtp.hostinger.com` |
| `EMAIL_PORT` | SMTP port | `587` |
| `EMAIL_USE_TLS` | Enable STARTTLS | `True` |
| `EMAIL_HOST_USER` | SMTP login / sender address | `noreply@example.com` |
| `EMAIL_HOST_PASSWORD` | SMTP password | *(keep secret)* |

### Django Core

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | *(required)* | Django secret key — change in production |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated list of allowed hostnames |

### Infrastructure

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://127.0.0.1:6379` | Redis connection URL; use `rediss://` for TLS (certificate is verified) |

Chat and rate limiting need a running Redis. In development, run one locally and keep the default URL:

- **Docker:** `docker run -d --name komute-redis -p 127.0.0.1:6379:6379 redis:7`
- **WSL:** `sudo apt install redis-server && sudo service redis-server start`
- **Windows native:** install [Memurai](https://www.memurai.com/) (Developer edition), which runs as a service on port 6379

Don't point `REDIS_URL` at a free hosted plan: Upstash deletes free databases after 14 days of inactivity. The test suite doesn't need Redis (`config.settings_regression` uses the in-memory channel layer).

### Application Tuning

| Variable | Default | Description |
|---|---|---|
| `SEARCH_RADIUS_KM` | `15` | Radius in km around origin/destination within which rides are shown in search results |
| `SERVICE_FEE_CENTS` | `100` | Flat service fee added to every booking, in cents (100 = $1.00) |
| `INVITE_EXPIRY_HOURS` | `48` | Hours before a ride invite link/token expires |
