from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('komute', '0007_ridestop'),
    ]

    operations = [
        migrations.AddField(
            model_name='vehicle',
            name='vehicle_verified',
            field=models.BooleanField(default=False),
        ),
    ]
