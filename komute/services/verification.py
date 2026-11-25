"""Driver identity verification with the verification node (API module 8).

Moved from komute/views.py (verify_driver_view, request_verification_review_view
and their helpers), which call these. The rules that matter most here:

- the driver accepts the current biometric notice on each attempt, and the
  consent is recorded before any image leaves the server (privacy policy s.12);
- the licence photos (front, and back with the barcode) and the selfie are
  resized in memory, sent to the service and dropped: never written to disk
  or storage (audit P-5/P-13). Callers
  install InMemoryUploadHandler before the request body is read;
- only the outcome is kept: which checks passed, reason categories (never the
  service's reason text, which can quote licence values) and the face score;
- a rejection can go to a person for review (Quebec Law 25 s.12.1); a result
  the service couldn't decide goes there by itself (audit P-10).

Refusals are VerificationError with the website's message and status."""
import base64
import logging
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadhandler import MemoryFileUploadHandler
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from django.utils import timezone

from komute.models import BiometricConsent, EmailLog, VerificationLog
from komute.private_media import open_stored_file
from komute.services.images import resize_image
from komute.tasks import send_later

logger = logging.getLogger('komute.views')
upload_logger = logging.getLogger('komute.uploads')

CONSENT_MISSING_MSG = "Please read the biometric notice and tick the box to agree before verifying."
CONSENT_STALE_MSG = "The biometric notice has changed. Please reload the page and review it again."
REVIEW_COMMENT_MAX_CHARS = 2000


class VerificationError(Exception):
    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# The node's error codes (verification_node/README.md, "Errors") -> what the driver sees. Any other code is a
# service error. Never pass the node's own text on: it is for developers.
NODE_ERRORS = {
    "BUSY": ("Verification is busy right now. Please try again in a minute.", 503, "service_busy"),
    "IMAGE_TOO_LARGE": ("One of your photos is too large. Please take it again.", 400, "invalid_photo"),
    "INVALID_IMAGE": ("One of your photos couldn't be read. Please take it again.", 400, "invalid_photo"),
    "MISSING_FIELD": ("Some licence details on your profile are missing. Please complete your profile and try again.",
                      400, "profile_incomplete"),
    "INVALID_FIELD": ("Some licence details on your profile aren't valid. Please check them and try again.",
                      400, "profile_invalid"),
}


class InMemoryUploadHandler(MemoryFileUploadHandler):
    """Keeps every uploaded file in memory whatever its size. Django's default
    writes files over FILE_UPLOAD_MAX_MEMORY_SIZE to a temp file on disk; the
    licence photo must never touch disk (audit P-5/P-13). Nginx's
    client_max_body_size still caps the request."""
    def handle_raw_input(self, *args, **kwargs):
        self.activated = True


# Keyword -> category for the node's free-text rejection_reasons. The
# service can't send codes, and its text may quote licence values, which the
# privacy policy (s.9/s.13) doesn't let us keep -- so only the category is
# stored. First match wins: image quality before the field checks, and vehicle
# before licence number so "licence plate number" isn't read as the DL number.
REASON_PATTERNS = [
    (VerificationLog.REASON_IMAGE_QUALITY, re.compile(r"blur|unreadable|illegible|quality|glare|too dark|retry", re.I)),
    (VerificationLog.REASON_FACE_MISMATCH, re.compile(r"face|facial|biometric|selfie", re.I)),
    (VerificationLog.REASON_VEHICLE_MISMATCH, re.compile(r"vehicle|plate|colou?r|alpr", re.I)),
    (VerificationLog.REASON_EXPIRY_MISMATCH, re.compile(r"expir", re.I)),
    (VerificationLog.REASON_DOB_MISMATCH, re.compile(r"birth|\bdob\b", re.I)),
    (VerificationLog.REASON_LICENSE_NUMBER_MISMATCH, re.compile(r"licen[cs]e\s*(number|no\b|#)|\bdl\s*(number|no\b)", re.I)),
    (VerificationLog.REASON_NAME_MISMATCH, re.compile(r"\bname\b", re.I)),
]


# Shown when a check failed but the service gave no reason for it.
STAGE_FAILED_REASONS = {
    "license_ocr": "The licence check didn't pass: the details read from your licence photo didn't match your profile.",
    "face_biometrics": "The face check didn't pass: your selfie didn't match the photo on your licence.",
    "vehicle_verification": "The vehicle check didn't pass: your vehicle photo didn't match your vehicle details.",
}


def reason_codes(reasons):
    """Categories for the service's rejection reasons, deduplicated in order."""
    codes = []
    for reason in reasons:
        code = next((c for c, pat in REASON_PATTERNS if pat.search(reason)), VerificationLog.REASON_OTHER)
        if code not in codes:
            codes.append(code)
    return codes


def consent_error(consent_flag, notice_version):
    """Biometric consent (privacy policy s.12): the driver must accept the
    current notice on this attempt, before any image is read or sent. An
    old version means the page predates a wording change, so the driver
    hasn't seen the text now in force. Returns (message, code) or None."""
    if not consent_flag:
        return CONSENT_MISSING_MSG, 'consent_required'
    if notice_version != settings.BIOMETRIC_NOTICE_VERSION:
        return CONSENT_STALE_MSG, 'notice_changed'
    return None


def record_biometric_consent(user, consent):
    """Stores proof that `user` accepted the current biometric notice.
    `consent` is common.consent_details() for this request."""
    BiometricConsent.objects.create(
        user=user,
        notice_version=settings.BIOMETRIC_NOTICE_VERSION,
        **consent,
    )


def verify(request, user, license_file, license_back_file, selfie_file, consent):
    """One verification attempt, after the caller's consent_error()
    check. Returns the result the driver is shown; raises
    VerificationError."""
    import requests as http_client

    # Only drivers verify. Checked first: a rider leaves no consent record
    # and no photo is read.
    if user.role != "driver":
        raise VerificationError("Only drivers can verify their identity.", status=403, code='not_a_driver')

    # The licence photos come with this request and are never written to
    # storage: they are resized in memory, sent to the node and dropped
    # with the request (audit P-5/P-13).
    if not license_file:
        raise VerificationError("Please add a photo of your driving license.", code='license_required')
    try:
        license_bytes = resize_image(license_file, label=f"user={user.id} verify-license").read()
    except ValidationError as e:
        raise VerificationError(f"License photo: {e.messages[0]} Please choose another photo.",
                                code='invalid_license_image')

    # The node reads the barcode on the back. It needs more pixels than the
    # front: readable at 1200 and 1600 px, one card fails at 800 px.
    if not license_back_file:
        raise VerificationError("Please add a photo of the back of your driving license, with the barcode.",
                                code='license_back_required')
    try:
        license_back_bytes = resize_image(license_back_file, max_dim=1600,
                                          label=f"user={user.id} verify-license-back").read()
    except ValidationError as e:
        raise VerificationError(f"License back photo: {e.messages[0]} Please choose another photo.",
                                code='invalid_license_back_image')

    # Only the selfie captured for this attempt counts. The profile photo is
    # never used: it may legitimately show the driver's vehicle instead of
    # their face, and it isn't proof that the person is present now.
    if not selfie_file:
        raise VerificationError("Please take a selfie using the camera when prompted.", code='selfie_required')
    # Same size as the stored photos, not the full-resolution original.
    try:
        selfie_bytes = resize_image(selfie_file, label=f"user={user.id} verify-selfie").read()
    except ValidationError as e:
        raise VerificationError(f"Selfie: {e.messages[0]} Please take the selfie again.", code='invalid_selfie')

    # The node checks the licence barcode, the face and the vehicle plate as
    # one decision -- a missing vehicle photo makes it reject the driver (not
    # just the vehicle), so a vehicle with a photo is required before
    # calling out to the service at all.
    user_vehicle = user.vehicles.filter(is_primary=True).first() or user.vehicles.first()
    if not user_vehicle or not user_vehicle.photo_url:
        raise VerificationError("Please add a vehicle with a photo to your profile before verifying.",
                                code='vehicle_photo_required')

    try:
        with open_stored_file(user_vehicle.photo_url) as f:
            vehicle_bytes = f.read()
    except Exception:
        logger.exception("verify_driver_view: could not read vehicle image for user %s", user.id)
        raise VerificationError("Could not read vehicle image. Please try again later.", status=500,
                                code='vehicle_photo_unreadable')

    payload = {
        "request_id": f"req_{uuid.uuid4().hex[:8]}",
        "driver_id": f"usr_{user.id}",
        # When the driver agreed, under which notice. Time zone included: the node refuses one without.
        "consent_at": timezone.now().isoformat(),
        "consent_policy_version": settings.BIOMETRIC_NOTICE_VERSION,
        "personal_info": {
            "full_name": user.full_name or "",
            "date_of_birth": user.dob.strftime("%Y-%m-%d") if user.dob else "",
        },
        "license_details": {
            "license_number": user.license_number or "",
            "issuing_province": user.license_province or "",
            "expiry_date": user.license_expiry.strftime("%Y-%m-%d") if user.license_expiry else "",
        },
        "vehicle_details": {
            "make": user_vehicle.make,
            "model": user_vehicle.model,
            "year": user_vehicle.year,
            "color": user_vehicle.color,
            "plate_number": user_vehicle.plate,
        },
        "images": {
            "selfie_base64": base64.b64encode(selfie_bytes).decode("utf-8"),
            "license_image_base64": base64.b64encode(license_bytes).decode("utf-8"),
            "license_back_base64": base64.b64encode(license_back_bytes).decode("utf-8"),
            "vehicle_photo_base64": base64.b64encode(vehicle_bytes).decode("utf-8"),
        },
    }

    # The consent record is written before the images leave the server.
    record_biometric_consent(user, consent)

    resp = None
    try:
        resp = http_client.post(
            settings.DL_VERIFICATION_API_URL,
            headers={"X-API-Key": settings.DL_VERIFICATION_API_KEY},
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()
    except http_client.exceptions.ConnectionError:
        raise VerificationError("Verification service is offline. Please try again later.",
                                status=503, code='service_offline')
    except http_client.exceptions.HTTPError:
        try:
            node_code = (resp.json().get("error") or {}).get("code")
        except Exception:
            node_code = None
        if node_code in NODE_ERRORS:
            upload_logger.warning("verification refused by node: user=%s code=%s", user.id, node_code)
            message, status, code = NODE_ERRORS[node_code]
            raise VerificationError(message, status=status, code=code)
        logger.exception("verify_driver_view: verification service call failed for user %s", user.id)
        upload_logger.warning("verification failed: user=%s service error (HTTP %s, %s)",
                              user.id, getattr(resp, "status_code", "-"), node_code or "-")
        raise VerificationError("Verification service error. Please try again later.", status=503,
                                code='service_error')
    except Exception:
        logger.exception("verify_driver_view: verification service call failed for user %s", user.id)
        upload_logger.warning("verification failed: user=%s service error (HTTP %s)",
                              user.id, getattr(resp, "status_code", "-"))
        raise VerificationError("Verification service error. Please try again later.", status=503,
                                code='service_error')

    decision = result.get("decision")
    stages = result.get("stages", {})
    stage_results = {
        name: bool((stages.get(name) or {}).get("passed", False))
        for name in ("license_ocr", "face_biometrics", "vehicle_verification")
    }
    # A driver passes only if the decision is APPROVED and every check
    # passed as well; a missing check counts as failed.
    verified = decision == "APPROVED" and all(stage_results.values())
    stage2 = stages.get("face_biometrics", {})
    try:
        similarity = float(stage2.get("vlm_confidence", 0.0))
    except (TypeError, ValueError):
        similarity = 0.0

    user.driver_verified  = verified
    user.face_match_score = similarity
    update_fields = ["driver_verified", "face_match_score"]
    if verified:
        # Starts a new DRIVER_REVERIFY_DAYS period.
        user.driver_verified_at        = timezone.now()
        user.reverify_reminder_sent_at = None
        update_fields += ["driver_verified_at", "reverify_reminder_sent_at"]
    user.save(update_fields=update_fields)

    if stage_results["vehicle_verification"]:
        user_vehicle.vehicle_verified = True
        user_vehicle.save(update_fields=["vehicle_verified"])

    rejection_reasons = result.get("rejection_reasons") or []
    if not isinstance(rejection_reasons, list):
        rejection_reasons = [str(rejection_reasons)]
    rejection_reasons = [str(r) for r in rejection_reasons]
    # An approval with a failed check comes with no reasons; name the checks.
    if not verified and not rejection_reasons:
        rejection_reasons = [STAGE_FAILED_REASONS[name] for name, ok in stage_results.items() if not ok]

    # Privacy policy s.9/s.13: keep the outcome and minimum details only. The
    # OCR output and the reason text (which can quote licence values) are not
    # stored; the driver sees the reasons in this response.
    codes = reason_codes(rejection_reasons)

    # No clear decision from the service: a person decides (audit P-10) and
    # the driver waits, unverified.
    if decision not in ("APPROVED", "REJECTED"):
        log_result = VerificationLog.RESULT_MANUAL_REVIEW
    else:
        log_result = VerificationLog.RESULT_APPROVED if verified else VerificationLog.RESULT_REJECTED
    borderline = log_result == VerificationLog.RESULT_MANUAL_REVIEW
    log = VerificationLog.objects.create(
        user=user,
        stage_results=stage_results,
        reason_codes=codes or None,
        face_score=similarity,
        result=log_result,
        **({"review_status": VerificationLog.REVIEW_PENDING,
            "review_source": VerificationLog.REVIEW_SOURCE_BORDERLINE,
            "review_requested_at": timezone.now()} if borderline else {}),
    )
    if borderline:
        send_review_admin_email(request, log)
    # Reason codes only, never the reason text (see above).
    upload_logger.info(
        "verification result: user=%s decision=%s stages=%s reasons=%s image_bytes=selfie:%d,license:%d,license_back:%d,vehicle:%d",
        user.id, decision, stage_results, codes,
        len(selfie_bytes), len(license_bytes), len(license_back_bytes), len(vehicle_bytes),
    )

    return {
        "verified": verified,
        "similarity": similarity,
        "face_match": stage2,
        "decision": decision,
        "reasons": rejection_reasons,
        "stage_results": stage_results,
        "vehicle_verified": stage_results["vehicle_verification"],
        # A person will look at this result anyway (borderline).
        "review_pending": borderline,
        # Law 25 s.12.1: a rejection can be sent to a person for review.
        "can_request_review": log_result == VerificationLog.RESULT_REJECTED,
    }


# ──────────────────────────────────────────────────────────────────
# Human review
# ──────────────────────────────────────────────────────────────────

def latest_verification(user):
    return VerificationLog.objects.filter(user=user).order_by('-created_at', '-id').first()


def review_state(user):
    """"pending" while a review is in the queue, "available" if the latest
    attempt was rejected and can still be sent, else ""."""
    log = latest_verification(user)
    if not log:
        return ""
    if log.review_status == VerificationLog.REVIEW_PENDING:
        return "pending"
    if log.result == VerificationLog.RESULT_REJECTED and not log.review_status:
        return "available"
    return ""


def send_review_admin_email(request, log):
    """Tell staff a verification attempt is waiting in the review queue."""
    admin_url = request.build_absolute_uri(reverse('admin:komute_verificationlog_change', args=[log.id]))
    user = log.user
    lines = [
        f"Verification #{log.id} needs a review ({log.get_review_source_display()})",
        '─' * 45,
        '',
        f"Driver:    {user.full_name} <{user.email}> (user {user.id})",
        f"Result:    {log.get_result_display()}",
        f"Checks:    {log.stage_results}",
        f"Reasons:   {', '.join(log.reason_codes or []) or '(none)'}",
    ]
    if log.driver_comment:
        lines += ['', "Driver's comment:", log.driver_comment]
    lines += ['', f"Review it in the admin: {admin_url}"]
    send_later(EmailMultiAlternatives(
        subject=f"[Komüte Verification] Review needed — #{log.id}",
        body='\n'.join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[settings.ADMIN_EMAIL],
    ), EmailLog.KIND_REVIEW_ADMIN)


def request_review(request, user, comment):
    """The driver asks a person to review their latest rejected verification
    (Quebec Law 25 s.12.1, GDPR Art. 22): it goes into the admin's review
    queue with their comment. Raises VerificationError."""
    if len(comment) > REVIEW_COMMENT_MAX_CHARS:
        raise VerificationError(f'Please keep your comment under {REVIEW_COMMENT_MAX_CHARS} characters.',
                                code='comment_too_long')
    log = latest_verification(user)
    if log and log.review_status == VerificationLog.REVIEW_PENDING:
        raise VerificationError('A review of your verification is already under way.', status=409,
                                code='review_pending')
    if not log or log.result != VerificationLog.RESULT_REJECTED or log.review_status:
        raise VerificationError('There is no rejected verification to review.', code='nothing_to_review')
    log.review_status = VerificationLog.REVIEW_PENDING
    log.review_source = VerificationLog.REVIEW_SOURCE_DRIVER
    log.driver_comment = comment or None
    log.review_requested_at = timezone.now()
    log.save(update_fields=['review_status', 'review_source', 'driver_comment', 'review_requested_at'])
    send_review_admin_email(request, log)
    return log
