"""Trust and safety: reviews after a trip, reporting a user, blocking and
unblocking (ISSUES.md A1; API module 7).

Moved from komute/views.py (submit_review_view, report_user_view,
block_user_view, unblock_user_view and their helpers), which call these.
Refusals are SafetyError with the website's message and status."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Avg, Q
from django.urls import reverse

from komute.hashid_utils import decode_id
from komute.models import EmailLog, Review, RideBooking, Rides, UserBlock, UserReport
from komute.services.booking_status import notify_booking_status
from komute.tasks import send_later

REPORT_DETAILS_MAX_CHARS = 2000
# What the review page offers (templates/review.html).
REVIEW_TAGS = ('punctual', 'friendly', 'polite', 'clean', 'great-convo', 'quiet', 'easy-pickup',
               'safe-driver', 'good-music', 'comfortable', 'fair-cost')
SUBRATING_CATEGORIES = ('punctuality', 'communication', 'cleanliness', 'respectfulness')


# The review page's comment box stops typing here (maxlength).
REVIEW_COMMENT_MAX_CHARS = 500
RATING_MSG = 'Rating must be between 1 and 5.'


class SafetyError(Exception):
    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# ──────────────────────────────────────────────────────────────────
# Reviews
# ──────────────────────────────────────────────────────────────────

def took_place(booking):
    """The trip must have happened for this booking: a completed booking, or
    a confirmed one on a completed ride. A rejected, cancelled or draft
    booking on a completed ride doesn't count."""
    return (
        booking.status == RideBooking.STATUS_COMPLETED
        or (booking.status == RideBooking.STATUS_CONFIRMED and booking.ride.status == 'completed')
    )


def expected_reviewee_id(booking, user):
    """The other person on the booking, or None if `user` isn't on it."""
    if booking.ride.driver_id == user.id:
        return booking.passenger_id
    if booking.passenger_id == user.id:
        return booking.ride.driver_id
    return None


def check_review(booking, user):
    """Raises SafetyError unless `user` may review this booking now; returns
    the reviewee's id."""
    reviewee_id = expected_reviewee_id(booking, user)
    if reviewee_id is None:
        raise SafetyError('Not authorized.', status=403, code='not_authorized')
    if not took_place(booking):
        raise SafetyError('Can only review completed rides.', code='not_completed')
    if Review.objects.filter(booking=booking, reviewer=user).exists():
        raise SafetyError('You have already reviewed this trip.', code='already_reviewed')
    return reviewee_id


def _whole_number(value):
    """An int from an int, a whole float or a string of digits; else None.
    A bool is not a number here (JSON true would otherwise count as 1)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def clean_rating(value):
    """The star rating, 1 to 5, or raises SafetyError. 4.7 used to be
    stored as 4."""
    rating = _whole_number(value)
    if rating is None or not 1 <= rating <= 5:
        raise SafetyError(RATING_MSG, code='invalid_rating')
    return rating


def clean_review_extras(comment, tags, subratings):
    """(comment or None, tags, subratings) as the review page sends them:
    a comment of up to 500 characters, tags from the page's list, and 1-5
    scores for the page's categories. Raises SafetyError otherwise; the
    tags are shown publicly, so nothing else may be stored."""
    if comment is None:
        comment = ''
    if not isinstance(comment, str):
        raise SafetyError('Your comment must be text.', code='invalid_comment')
    comment = comment.strip()
    if len(comment.replace('\r\n', '\n')) > REVIEW_COMMENT_MAX_CHARS:
        raise SafetyError(f'Please keep your comment under {REVIEW_COMMENT_MAX_CHARS} characters.',
                          code='comment_too_long')

    tags = tags or []
    if not isinstance(tags, list) or any(t not in REVIEW_TAGS for t in tags):
        raise SafetyError('Please choose tags from the list.', code='invalid_tags')

    subratings = subratings or {}
    if not isinstance(subratings, dict) or any(
            key not in SUBRATING_CATEGORIES or _whole_number(score) is None or not 1 <= _whole_number(score) <= 5
            for key, score in subratings.items()):
        raise SafetyError('Detailed ratings must be 1 to 5 stars for the listed categories.',
                          code='invalid_subratings')
    subratings = {key: _whole_number(score) for key, score in subratings.items()}
    return comment or None, list(dict.fromkeys(tags)), subratings


def save_review(booking, user, reviewee_id, *, rating, body, is_anonymous, tags, subratings):
    """Stores the review (check_review() passed) and refreshes the reviewee's
    average rating."""
    User = get_user_model()
    with transaction.atomic():
        review = Review.objects.create(
            booking=booking,
            reviewer=user,
            reviewee_id=reviewee_id,
            rating=rating,
            body=body,
            tags_json=tags or None,
            subratings_json=subratings or None,
            is_anonymous=is_anonymous,
        )

        # Recompute the reviewee's avg_rating from scratch rather than folding
        # this one rating in -- cheap at this scale, and avoids drift from any
        # review that's ever edited/deleted out from under a running average.
        # select_for_update so two reviews landing on the same reviewee at
        # once can't race and have the second update clobber the first.
        reviewee = User.objects.select_for_update().get(id=reviewee_id)
        avg = Review.objects.filter(reviewee_id=reviewee_id).aggregate(Avg('rating'))['rating__avg']
        reviewee.avg_rating = round(avg, 2) if avg is not None else 0
        reviewee.save(update_fields=['avg_rating'])
    return review


def pending_reviews(user):
    """Trips `user` took part in and hasn't reviewed yet, newest first."""
    reviewed = Review.objects.filter(reviewer=user).values_list('booking_id', flat=True)
    return (
        RideBooking.objects
        .filter(Q(passenger=user) | Q(ride__driver=user))
        .filter(Q(status=RideBooking.STATUS_COMPLETED)
                | Q(status=RideBooking.STATUS_CONFIRMED, ride__status='completed'))
        .exclude(id__in=reviewed)
        .select_related('ride__driver', 'passenger')
        .order_by('-ride__depart_at')
    )


# ──────────────────────────────────────────────────────────────────
# Reports and blocks
# ──────────────────────────────────────────────────────────────────

def safety_target(me, booking_key, ride_key):
    """The other user a report or block is about, and the booking it came
    from. Named by a booking the signed-in user is part of (`booking_key`),
    or by a ride they can view (`ride_key`, meaning its driver), never by a
    raw user ID. Returns (None, None) if neither names someone else."""
    if booking_key:
        booking_id = decode_id(booking_key)
        booking = booking_id and (
            RideBooking.objects.select_related('ride__driver', 'passenger').filter(id=booking_id).first()
        )
        if not booking:
            return None, None
        if me == booking.passenger:
            return booking.ride.driver, booking
        if me == booking.ride.driver:
            return booking.passenger, booking
        return None, None
    if ride_key:
        ride_id = decode_id(ride_key)
        ride = ride_id and Rides.objects.select_related('driver').filter(id=ride_id).first()
        if not ride or ride.driver == me:
            return None, None
        booking = RideBooking.objects.filter(ride=ride, passenger=me).order_by('-booked_at').first()
        return ride.driver, booking
    return None, None


def send_report_admin_email(request, report):
    """Tell staff a report came in. The report is saved either way."""
    admin_url = request.build_absolute_uri(reverse('admin:komute_userreport_change', args=[report.id]))
    booking = report.booking
    lines = [
        f"New user report #{report.id}",
        '─' * 45,
        '',
        f"Reported:  {report.reported.full_name} <{report.reported.email}> (user {report.reported_id})",
        f"Reporter:  {report.reporter.full_name} <{report.reporter.email}> (user {report.reporter_id})",
        f"Reason:    {report.get_reason_display()}",
    ]
    if booking:
        lines.append(f"Booking:   #{booking.id} ({booking.status}), ride {booking.ride.origin} → {booking.ride.destination}")
    lines += ['', 'Details:', report.details or '(none)', '', f"Review it in the admin: {admin_url}"]
    send_later(EmailMultiAlternatives(
        subject=f"[Komüte Report] {report.get_reason_display()} — report #{report.id}",
        body='\n'.join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[settings.ADMIN_EMAIL],
    ), EmailLog.KIND_REPORT_ADMIN)


def report_user(request, reporter, reported, booking, reason, details):
    """Files the report (the caller found `reported` with safety_target and
    applied the report-user rate limit) and tells staff."""
    if reason not in dict(UserReport.REASON_CHOICES):
        raise SafetyError('Please choose a reason.', code='reason_required')
    if len(details) > REPORT_DETAILS_MAX_CHARS:
        raise SafetyError(f'Please keep the details under {REPORT_DETAILS_MAX_CHARS} characters.',
                          code='details_too_long')
    if reason == UserReport.REASON_OTHER and not details:
        raise SafetyError('Please tell us what happened.', code='details_required')

    report = UserReport.objects.create(
        reporter=reporter,
        reported=reported,
        booking=booking,
        reason=reason,
        details=details,
    )
    send_report_admin_email(request, report)
    return report


def close_chats_between(user_a, user_b):
    """Lock any open chat page between two users who are now blocked."""
    bookings = (
        RideBooking.objects
        .filter(Q(ride__driver=user_a, passenger=user_b) | Q(ride__driver=user_b, passenger=user_a))
        .filter(status__in=(RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED))
        .select_related('ride')
    )
    for booking in bookings:
        notify_booking_status(booking)


def block(blocker, target):
    """Blocks `target` (found with safety_target). Returns True if new."""
    _, created = UserBlock.objects.get_or_create(blocker=blocker, blocked=target)
    if created:
        close_chats_between(blocker, target)
    return created


def unblock(blocker, blocked_id):
    """Returns True if there was a block to remove."""
    deleted, _ = UserBlock.objects.filter(blocker=blocker, blocked_id=blocked_id).delete()
    return bool(deleted)


def blocked_users(user):
    """The users `user` has blocked, newest first."""
    return UserBlock.objects.filter(blocker=user).select_related('blocked').order_by('-created_at')
