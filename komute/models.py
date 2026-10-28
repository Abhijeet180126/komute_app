import logging
from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone
from encrypted_model_fields.fields import EncryptedMixin, EncryptedTextField

logger = logging.getLogger(__name__)


class EncryptedFloatField(EncryptedMixin, models.FloatField):
    def to_python(self, value):
        try:
            return super().to_python(value)
        except (ValidationError, ValueError, TypeError):
            logger.error(
                "EncryptedFloatField: failed to decrypt/parse stored value for "
                "field %r; returning None. This can indicate a key-rotation "
                "problem in FIELD_ENCRYPTION_KEY causing data loss.",
                self.name,
                exc_info=True,
            )
            return None


phone_validator = RegexValidator(
regex=r'^\+?[1-9]\d{1,14}$',
message="Phone number must be in E.164 format."
)


class CustomUserManager(BaseUserManager):
    # AbstractUser's default UserManager hard-codes `username` as the
    # identifying field (create_superuser(self, username, email=None, ...)).
    # CustomUser sets USERNAME_FIELD = "email" instead, so createsuperuser
    # never passes `username` at all -- this manager fills it in from email,
    # matching the username==email convention used everywhere else (e.g.
    # signup_view).
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email)
        extra_fields.setdefault("username", email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)


class CustomUser(AbstractUser):
    # Remove default username fields
    # username = None
    first_name = None
    last_name = None

    ROLE_CHOICES = (
        ("rider", "Rider"),
        ("driver", "Driver"),
        ("admin", "Admin"),
    )

    GENDER_CHOICES = (
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
    )

    # Core fields
    full_name = models.TextField()

    email = models.EmailField(
        unique=True
    )

    email_verified = models.BooleanField(
        default=False
    )

    pending_email = models.EmailField(
        null=True,
        blank=True
    )

    phone = models.CharField(
    max_length=16,
    validators=[phone_validator],
    null=True,
    blank=True
    )
    phone_verified = models.BooleanField(
        default=False
    )

    # OAuth
    oauth_provider = models.TextField(
        null=True,
        blank=True
    )

    oauth_subject = models.TextField(
        null=True,
        blank=True
    )

    # Role
    role = models.TextField(
        choices=ROLE_CHOICES,
        default="rider"
    )

    # Profile
    dob = models.DateField(null=True)

    gender = models.TextField(
        choices=GENDER_CHOICES,
        null=True,
        blank=True
    )

    bio = models.TextField(
        null=True,
        blank=True
    )

    avatar_url = models.TextField(
        null=True,
        blank=True
    )

    # Driver verification
    driver_verified = models.BooleanField(
        default=False
    )

    # When the last verification passed. Verification lapses
    # DRIVER_REVERIFY_DAYS later (expire_driver_verification command).
    driver_verified_at = models.DateTimeField(
        null=True,
        blank=True
    )

    # Set when the "re-verify soon" email goes out, cleared on the next pass,
    # so each verification period gets at most one reminder.
    reverify_reminder_sent_at = models.DateTimeField(
        null=True,
        blank=True
    )

    # Encrypted at rest (privacy policy s.9, audit P-5). Only compared in
    # Python, never filtered on in SQL.
    license_number = EncryptedTextField(
        null=True,
        blank=True
    )

    license_province = EncryptedTextField(
        null=True,
        blank=True
    )

    # Left plain: expire_driver_verification filters on it in SQL.
    license_expiry = models.DateField(
        null=True,
        blank=True
    )

    # No licence image field: the photo is sent with the verification
    # request and never stored (audit P-5/P-13).

    selfie_url = models.TextField(
        null=True,
        blank=True
    )

    face_match_score = EncryptedFloatField(
        null=True,
        blank=True
    )

    # Stats
    avg_rating = models.FloatField(
        default=0
    )

    total_rides = models.IntegerField(
        default=0
    )

    # Deactivation / deletion request (Section 8 of the privacy policy).
    # deactivated_at is set the moment the user confirms via the emailed link
    # (is_active=False blocks login immediately) and anchors the
    # ACCOUNT_DELETION_GRACE_DAYS clock. data_purged_at is set once
    # purge_deactivated_accounts has anonymized/purged the account, so the
    # job doesn't reprocess it.
    deactivated_at = models.DateTimeField(
        null=True,
        blank=True
    )

    data_purged_at = models.DateTimeField(
        null=True,
        blank=True
    )

    # Timestamps
    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    # Auth config
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name", "dob"]

    objects = CustomUserManager()

    class Meta:
        db_table = "users"

    def clean(self):
        super().clean()

        if self.dob is None:
            return

        today = date.today()

        age = (
            today.year - self.dob.year
            - (
                (today.month, today.day)
                < (self.dob.month, self.dob.day)
            )
        )

        if age < 18:
            raise ValidationError(
                "User must be at least 18 years old."
            )

    def __str__(self):
        return self.email

    @property
    def driver_reverify_due_at(self):
        """When the current verification lapses, or None if there is no dated one."""
        if not self.driver_verified_at:
            return None
        return self.driver_verified_at + timedelta(days=settings.DRIVER_REVERIFY_DAYS)

    @property
    def license_expired(self):
        return self.license_expiry is not None and self.license_expiry < timezone.localdate()

    @property
    def driver_verification_current(self):
        """driver_verified, not past its re-verification date, and the
        license it was checked against hasn't expired since.

        Checked directly where it matters (posting rides, accepting bookings)
        so a driver is blocked on time even if the daily
        expire_driver_verification run hasn't cleared driver_verified yet.
        """
        due = self.driver_reverify_due_at
        return (
            self.driver_verified
            and (due is None or timezone.now() < due)
            and not self.license_expired
        )


class Vehicle(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vehicles"
    )
    
    make = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    year = models.IntegerField()
    color = models.CharField(max_length=50)
    plate = models.CharField(max_length=20, unique=True)

    seat_capacity = models.IntegerField(default=4)
    photo_url = models.TextField(null=True, blank=True)

    is_primary = models.BooleanField(default=False)

    vehicle_verified = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "vehicles"

    def __str__(self):
        return f"{self.make} {self.model} ({self.plate})"

class Rides(models.Model):
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trips"
    )

    vehicle = models.ForeignKey(
        "Vehicle",
        on_delete=models.SET_NULL,
        null=True,
        related_name="trips"
    )

    origin = models.TextField()
    origin_lat = models.FloatField()
    origin_lng = models.FloatField()

    destination = models.TextField()
    dest_lat = models.FloatField()
    dest_lng = models.FloatField()

    depart_at = models.DateTimeField()
    arrive_at = models.DateTimeField(null=True, blank=True)

    seats_total = models.IntegerField()
    seats_available = models.IntegerField()

    price_cents = models.IntegerField(default=0)
    currency = models.TextField(default="CAD")

    STATUS_CHOICES = (
        ("posted", "Posted"),
        ("full", "Full"),
        ("cancelled", "Cancelled"),
        ("completed", "Completed"),
    )
    status = models.TextField(choices=STATUS_CHOICES, default="posted")

    cancelled_at = models.DateTimeField(null=True, blank=True)

    PASSENGER_PREF_CHOICES = (
        ("any", "Any"),
        ("women_only", "Women Only"),
        ("men_only", "Men Only"),
    )
    passenger_gender_pref = models.TextField(
        choices=PASSENGER_PREF_CHOICES, default="any"
    )

    recurring_group_id = models.TextField(null=True, blank=True)
    preferences_json = models.JSONField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    tz = models.TextField(null=True, blank=True)
    expected_duration_seconds = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rides"

    @property
    def price_display(self):
        return f"${self.price_cents / 100:.2f}"

    def __str__(self):
        return f"{self.origin} → {self.destination}"


class RideStop(models.Model):
    ride = models.ForeignKey(
        "Rides",
        on_delete=models.CASCADE,
        related_name="stops"
    )

    stop_order = models.IntegerField()
    location = models.TextField()
    lat = models.FloatField()
    lng = models.FloatField()
    scheduled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ride_stops"
        ordering = ["stop_order"]

    def __str__(self):
        return f"Stop {self.stop_order}: {self.location}"


class RideBooking(models.Model):

    STATUS_DRAFT = "draft"
    STATUS_REQUESTED = "requested"
    STATUS_CONFIRMED = "confirmed"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUS_COMPLETED = "completed"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_REQUESTED, "Requested"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_COMPLETED, "Completed"),
    ]

    ride = models.ForeignKey(
        "Rides",
        on_delete=models.CASCADE,
        related_name="bookings",
    )

    passenger = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ride_bookings",
    )

    pickup_stop = models.ForeignKey(
        "RideStop",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pickup_bookings",
        help_text="Intermediate boarding stop if not ride origin",
    )

    dropoff_stop = models.ForeignKey(
        "RideStop",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dropoff_bookings",
        help_text="Intermediate drop-off stop if not final destination",
    )

    seats = models.PositiveIntegerField(default=1)

    agreed_price_cents = models.PositiveIntegerField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_REQUESTED,
    )

    booked_at = models.DateTimeField(auto_now_add=True)

    confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    cancelled_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    # When the driver rejected the request. purge_cancelled_rides deletes
    # rejected bookings CANCELLED_RIDE_RETENTION_DAYS after this (audit P-17).
    rejected_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    cancellation_reason = models.TextField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "ride_bookings"
        ordering = ["-booked_at"]

    def __str__(self):
        return f"Booking #{self.id} - Ride {self.ride_id}"


class Message(models.Model):
    ride = models.ForeignKey(
        'Rides',
        on_delete=models.CASCADE,
        related_name='messages',
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_messages',
    )
    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='received_messages',
    )
    body = models.TextField()
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = 'messages'
        ordering = ['created_at']

    def __str__(self):
        return f"Msg #{self.id} in ride {self.ride_id}"


class PriceOffer(models.Model):

    STATUS_PENDING   = "pending"
    STATUS_ACCEPTED  = "accepted"
    STATUS_DECLINED  = "declined"
    STATUS_COUNTERED = "countered"

    STATUS_CHOICES = [
        (STATUS_PENDING,   "Pending"),
        (STATUS_ACCEPTED,  "Accepted"),
        (STATUS_DECLINED,  "Declined"),
        (STATUS_COUNTERED, "Countered"),
    ]

    booking = models.ForeignKey(
        "RideBooking",
        on_delete=models.CASCADE,
        related_name="price_offers",
    )

    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="price_offers",
        help_text="Driver or passenger who proposed this price.",
    )

    price_cents = models.PositiveIntegerField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    responded_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "negotiations"
        ordering = ["created_at"]

    def __str__(self):
        return f"Offer #{self.id} on booking {self.booking_id} — {self.price_cents}¢ ({self.status})"


class RideSearch(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ride_searches",
    )

    origin = models.TextField()
    origin_lat = models.FloatField()
    origin_lng = models.FloatField()

    destination = models.TextField()
    dest_lat = models.FloatField()
    dest_lng = models.FloatField()

    search_date = models.DateField()
    passenger_count = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "search_log"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.origin} → {self.destination} on {self.search_date}"


class VerificationLog(models.Model):
    RESULT_APPROVED = "approved"
    RESULT_MANUAL_REVIEW = "manual_review"
    RESULT_REJECTED = "rejected"

    RESULT_CHOICES = [
        (RESULT_APPROVED, "Approved"),
        (RESULT_MANUAL_REVIEW, "Manual Review"),
        (RESULT_REJECTED, "Rejected"),
    ]

    # reason_codes values (see views._reason_codes)
    REASON_IMAGE_QUALITY = "IMAGE_QUALITY"
    REASON_FACE_MISMATCH = "FACE_MISMATCH"
    REASON_VEHICLE_MISMATCH = "VEHICLE_MISMATCH"
    REASON_EXPIRY_MISMATCH = "EXPIRY_MISMATCH"
    REASON_DOB_MISMATCH = "DOB_MISMATCH"
    REASON_LICENSE_NUMBER_MISMATCH = "LICENSE_NUMBER_MISMATCH"
    REASON_NAME_MISMATCH = "NAME_MISMATCH"
    REASON_OTHER = "OTHER"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="verification_logs",
    )

    # No longer written (privacy policy s.9/s.13: extracted licence data is
    # deleted once the decision is recorded). Kept for rows written before
    # that; scrub_verification_data nulls them.
    ocr_extracted_json = EncryptedTextField(null=True, blank=True)

    # Outcome-only details kept for support (up to
    # VERIFICATION_RECORD_RETENTION_DAYS). Neither holds licence values:
    #   stage_results: {"license_ocr": bool, "face_biometrics": bool,
    #                   "vehicle_verification": bool} from the service.
    #   reason_codes:  categories Komute derives from the service's
    #                  rejection_reasons text (REASON_* below); the text
    #                  itself is shown to the driver and then discarded.
    stage_results = models.JSONField(null=True, blank=True)
    reason_codes = models.JSONField(null=True, blank=True)

    face_score = EncryptedFloatField(null=True, blank=True)

    result = models.TextField(choices=RESULT_CHOICES)

    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_verifications",
    )

    notes = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "verification_log"

    def __str__(self):
        return f"VerificationLog #{self.id} — {self.user_id} ({self.result})"


class BiometricConsent(models.Model):
    """One row each time a driver accepts the biometric notice shown before
    the verification selfie (privacy policy s.12, Terms s.9; BIPA written
    release, Texas CUBI, Washington RCW 19.375, Quebec Law 25 express
    consent). Holds no biometric data, only proof of what was agreed and
    when, so it is kept for the life of the account."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="biometric_consents",
    )
    # settings.BIOMETRIC_NOTICE_VERSION at the time of acceptance.
    notice_version = models.CharField(max_length=32)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "biometric_consent"
        ordering = ["-accepted_at"]

    def __str__(self):
        return f"BiometricConsent #{self.id} — {self.user_id} (v{self.notice_version})"


class LegalAcceptance(models.Model):
    """One row each time a user accepts the Terms & Conditions and the
    Privacy Policy: at signup, and again whenever settings.TERMS_VERSION or
    PRIVACY_VERSION changes (LegalAcceptanceMiddleware asks). The first row
    starts the Terms' 30-day arbitration opt-out window (s.12). Kept for the
    life of the account."""

    SOURCE_SIGNUP = "signup"
    SOURCE_REACCEPT = "reaccept"
    SOURCE_CHOICES = [
        (SOURCE_SIGNUP, "Signup"),
        (SOURCE_REACCEPT, "Accepted updated versions"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="legal_acceptances",
    )
    terms_version = models.CharField(max_length=32)
    privacy_version = models.CharField(max_length=32)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "legal_acceptance"
        ordering = ["-accepted_at"]
        indexes = [models.Index(fields=["user", "terms_version", "privacy_version"])]

    def __str__(self):
        return (f"LegalAcceptance #{self.id} — {self.user_id} "
                f"(terms {self.terms_version}, privacy {self.privacy_version})")


class Review(models.Model):
    booking = models.ForeignKey(
        "RideBooking",
        on_delete=models.SET_NULL,
        null=True,
        related_name="reviews",
    )

    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="reviews_given",
    )

    reviewee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviews_received",
    )

    rating = models.PositiveSmallIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(5),
        ]
    )

    body = models.TextField(null=True, blank=True)

    tags_json = models.JSONField(null=True, blank=True)

    subratings_json = models.JSONField(null=True, blank=True)

    is_anonymous = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "review"
        constraints = [
            models.UniqueConstraint(
                fields=["booking", "reviewer"],
                name="unique_review_per_booking_reviewer",
            )
        ]

    def __str__(self):
        return f"Review #{self.id} — {self.reviewer_id} → {self.reviewee_id} ({self.rating}★)"


class RideInvite(models.Model):
    STATUS_SENT     = "sent"
    STATUS_VIEWED   = "viewed"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_EXPIRED  = "expired"

    STATUS_CHOICES = [
        (STATUS_SENT,     "Sent"),
        (STATUS_VIEWED,   "Viewed"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_EXPIRED,  "Expired"),
    ]

    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_invites",
    )

    passenger = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_invites",
    )

    ride = models.ForeignKey(
        "Rides",
        on_delete=models.CASCADE,
        related_name="invites",
    )

    status = models.TextField(
        choices=STATUS_CHOICES,
        default=STATUS_SENT,
    )

    sent_at = models.DateTimeField(auto_now_add=True)

    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "invites"

    def __str__(self):
        return f"Invite #{self.id} — ride {self.ride_id} → {self.passenger_id} ({self.status})"


class UserReport(models.Model):
    """A user's report about another user (ISSUES.md A1). Staff work through
    open reports in the admin. Closed reports are deleted
    REPORT_RETENTION_DAYS after closing (purge_user_reports)."""

    REASON_HARASSMENT            = "harassment"
    REASON_UNSAFE_DRIVING        = "unsafe_driving"
    REASON_NO_SHOW               = "no_show"
    REASON_FRAUD                 = "fraud"
    REASON_INAPPROPRIATE_MESSAGE = "inappropriate_message"
    REASON_OTHER                 = "other"

    REASON_CHOICES = [
        (REASON_HARASSMENT,            "Harassment or threats"),
        (REASON_UNSAFE_DRIVING,        "Unsafe driving"),
        (REASON_NO_SHOW,               "No-show"),
        (REASON_FRAUD,                 "Fraud or fake profile"),
        (REASON_INAPPROPRIATE_MESSAGE, "Inappropriate message"),
        (REASON_OTHER,                 "Other"),
    ]

    STATUS_OPEN      = "open"
    STATUS_REVIEWING = "reviewing"
    STATUS_ACTIONED  = "actioned"
    STATUS_DISMISSED = "dismissed"

    STATUS_CHOICES = [
        (STATUS_OPEN,      "Open"),
        (STATUS_REVIEWING, "Reviewing"),
        (STATUS_ACTIONED,  "Actioned"),
        (STATUS_DISMISSED, "Dismissed"),
    ]
    CLOSED_STATUSES = (STATUS_ACTIONED, STATUS_DISMISSED)

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="reports_made",
    )

    reported = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reports_received",
    )

    # The booking the report came from, if any. Staff read the chat from it.
    booking = models.ForeignKey(
        "RideBooking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
    )

    reason = models.TextField(choices=REASON_CHOICES)
    details = models.TextField(blank=True, default="")

    status = models.TextField(choices=STATUS_CHOICES, default=STATUS_OPEN)
    staff_note = models.TextField(blank=True, default="")

    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports_handled",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_reports"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Report #{self.id} — {self.reporter_id} → {self.reported_id} ({self.reason}, {self.status})"


class UserBlockManager(models.Manager):
    def between(self, user_a_id, user_b_id):
        """True if either user has blocked the other."""
        return self.filter(
            models.Q(blocker_id=user_a_id, blocked_id=user_b_id)
            | models.Q(blocker_id=user_b_id, blocked_id=user_a_id)
        ).exists()

    def hidden_ids(self, user_id):
        """IDs of users this user has blocked or been blocked by."""
        pairs = self.filter(
            models.Q(blocker_id=user_id) | models.Q(blocked_id=user_id)
        ).values_list("blocker_id", "blocked_id")
        return {b if a == user_id else a for a, b in pairs}


class UserBlock(models.Model):
    """A block hides each user's rides from the other, refuses bookings and
    invites between them, and closes their chats (ISSUES.md A1). It works
    the same in both directions; only the blocker can lift it."""

    blocker = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="blocks_made",
    )

    blocked = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="blocks_received",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects = UserBlockManager()

    class Meta:
        db_table = "user_blocks"
        constraints = [
            models.UniqueConstraint(
                fields=["blocker", "blocked"],
                name="unique_user_block",
            ),
            models.CheckConstraint(
                condition=~models.Q(blocker=models.F("blocked")),
                name="user_block_not_self",
            ),
        ]

    def __str__(self):
        return f"Block {self.blocker_id} → {self.blocked_id}"

class EmailLog(models.Model):
    """One row per queued email, so staff can see whether it went out
    (audit F-6). Holds no body and only a masked address; purge_task_log
    deletes rows after TASK_LOG_RETENTION_DAYS."""

    KIND_BOOKING_REQUEST   = "booking_request"
    KIND_BOOKING_CONFIRMED = "booking_confirmed"
    KIND_BOOKING_CANCELLED = "booking_cancelled"
    KIND_BOOKING_REJECTED  = "booking_rejected"
    KIND_RIDE_CANCELLED    = "ride_cancelled"
    KIND_WELCOME           = "welcome"
    KIND_INVITE            = "invite"
    KIND_REPORT_ADMIN      = "report_admin"

    KIND_CHOICES = [
        (KIND_BOOKING_REQUEST, "Booking request (to driver)"),
        (KIND_BOOKING_CONFIRMED, "Booking confirmed"),
        (KIND_BOOKING_CANCELLED, "Booking cancelled (to driver)"),
        (KIND_BOOKING_REJECTED, "Booking declined (to rider)"),
        (KIND_RIDE_CANCELLED, "Ride cancelled (to rider)"),
        (KIND_WELCOME, "Welcome"),
        (KIND_INVITE, "Ride invite"),
        (KIND_REPORT_ADMIN, "User report (to staff)"),
    ]

    STATUS_QUEUED   = "queued"
    STATUS_RETRYING = "retrying"
    STATUS_SENT     = "sent"
    STATUS_FAILED   = "failed"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RETRYING, "Retrying"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
    ]

    kind = models.CharField(max_length=30, choices=KIND_CHOICES)

    # The account the email went to; empty for staff emails.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="email_logs",
    )

    to_masked = models.CharField(max_length=255)
    subject = models.CharField(max_length=255)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)

    # SES's id for the sent message, for looking it up in AWS.
    message_id = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "email_logs"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} to {self.to_masked} ({self.status})"
