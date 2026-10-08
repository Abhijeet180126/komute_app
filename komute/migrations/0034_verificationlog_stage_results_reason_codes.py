from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('komute', '0033_customuser_driver_verified_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='verificationlog',
            name='stage_results',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='verificationlog',
            name='reason_codes',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
