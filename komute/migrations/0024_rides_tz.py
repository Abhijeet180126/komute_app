from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('komute', '0023_encrypt_biometric_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='rides',
            name='tz',
            field=models.TextField(blank=True, null=True),
        ),
    ]
