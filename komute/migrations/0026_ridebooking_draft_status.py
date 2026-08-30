from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('komute', '0025_rides_expected_duration_seconds'),
    ]

    operations = [
        migrations.AlterField(
            model_name='ridebooking',
            name='status',
            field=models.CharField(
                choices=[
                    ('draft', 'Draft'),
                    ('requested', 'Requested'),
                    ('confirmed', 'Confirmed'),
                    ('rejected', 'Rejected'),
                    ('cancelled', 'Cancelled'),
                    ('completed', 'Completed'),
                ],
                default='requested',
                max_length=20,
            ),
        ),
    ]
