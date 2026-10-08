from datetime import timedelta

from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def backfill_driver_verified_at(apps, schema_editor):
    # Drivers verified before this field existed: start their clock at their
    # last approved verification (today if no log survives), but no earlier
    # than the start of the reminder window, so every existing driver still
    # gets the reminder email and the full notice period before lapsing.
    CustomUser = apps.get_model('komute', 'CustomUser')
    VerificationLog = apps.get_model('komute', 'VerificationLog')
    now = timezone.now()
    earliest = now - timedelta(
        days=settings.DRIVER_REVERIFY_DAYS - settings.DRIVER_REVERIFY_REMINDER_DAYS
    )
    for user in CustomUser.objects.filter(driver_verified=True, driver_verified_at__isnull=True):
        last = (
            VerificationLog.objects
            .filter(user_id=user.id, result='approved')
            .order_by('-created_at')
            .values_list('created_at', flat=True)
            .first()
        )
        user.driver_verified_at = max(last or now, earliest)
        user.save(update_fields=['driver_verified_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('komute', '0032_alter_customuser_managers'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='driver_verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='customuser',
            name='reverify_reminder_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_driver_verified_at, migrations.RunPython.noop),
    ]
