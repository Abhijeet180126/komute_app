"""Background jobs run by the django-q2 worker, `manage.py qcluster` (audit F-6).

Fire-and-forget emails (booking updates, welcome, invites, report alerts)
are built in the request and queued here, so the page doesn't wait on SES,
a deploy doesn't drop them, and a failed send is retried (Q_CLUSTER in
settings). Emails whose page reports a send failure to the user (password
reset, email verification, account deletion, contact form) still send in
the request.

Each queued email gets an EmailLog row (admin: Komute → Email logs) that
moves from queued to sent, or through retrying to failed.
"""
import logging

from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)


def mask_email(address):
    """'abhijeet@example.com' -> 'ab***@example.com'."""
    local, _, domain = address.partition('@')
    return f'{local[:2]}***@{domain}' if domain else '***'


def _record_attempt(log_id, msg, error=None):
    from django_q.conf import Conf
    from .models import EmailLog

    if not log_id:
        return  # queued before the log existed
    if error is None:
        status = getattr(msg, 'anymail_status', None)
        EmailLog.objects.filter(id=log_id).update(
            status=EmailLog.STATUS_SENT, attempts=F('attempts') + 1, last_error='',
            sent_at=timezone.now(), message_id=(getattr(status, 'message_id', None) or '')[:255],
        )
        return
    log = EmailLog.objects.filter(id=log_id).first()
    if log is None:
        return
    log.attempts += 1
    log.last_error = f'{type(error).__name__}: {error}'[:1000]
    final = Conf.SYNC or (Conf.MAX_ATTEMPTS and log.attempts >= Conf.MAX_ATTEMPTS)
    log.status = EmailLog.STATUS_FAILED if final else EmailLog.STATUS_RETRYING
    log.save(update_fields=['attempts', 'last_error', 'status'])


def send_email(msg, log_id=None):
    """Worker side. Raises on failure, so the job isn't acknowledged and is
    retried."""
    try:
        msg.send(fail_silently=False)
    except Exception as e:
        _record_attempt(log_id, msg, e)
        raise
    _record_attempt(log_id, msg)


def send_later(msg, kind, user=None):
    """Queue an EmailMessage for the worker and log it. If the queue can't
    be written, send it now rather than drop it."""
    from django_q.tasks import async_task
    from .models import EmailLog

    log = EmailLog.objects.create(
        kind=kind, user=user,
        to_masked=', '.join(mask_email(a) for a in msg.to)[:255],
        subject=msg.subject[:255],
    )
    try:
        async_task('komute.tasks.send_email', msg, log.id, group='email')
    except Exception:
        logger.exception('Could not queue email "%s"; sending it now', msg.subject)
        try:
            send_email(msg, log.id)
        except Exception:
            logger.exception('Could not send email "%s"', msg.subject)
