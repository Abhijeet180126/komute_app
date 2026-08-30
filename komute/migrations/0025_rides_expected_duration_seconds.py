from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('komute', '0024_rides_tz'),
    ]

    operations = [
        migrations.AddField(
            model_name='rides',
            name='expected_duration_seconds',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
