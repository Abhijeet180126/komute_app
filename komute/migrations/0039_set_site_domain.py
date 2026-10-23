from django.conf import settings
from django.db import migrations


def set_site(apps, schema_editor):
    # Django creates Site 1 as example.com. allauth and the admin read it,
    # so point it at the real domain (audit S-18).
    Site = apps.get_model('sites', 'Site')
    Site.objects.update_or_create(
        id=settings.SITE_ID,
        defaults={'domain': 'komute.net', 'name': 'Komüte'},
    )


class Migration(migrations.Migration):

    dependencies = [
        ('komute', '0038_email_log'),
        ('sites', '0002_alter_domain_unique'),
    ]

    operations = [
        migrations.RunPython(set_site, migrations.RunPython.noop),
    ]
