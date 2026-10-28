"""
Audit P-5/P-13: the driver's licence photo is no longer stored. It is taken in
the verification step, sent to DL-Verification and dropped. This deletes the
licence files already on disk, removes the two URL fields, and encrypts the
licence number and province.
"""
import encrypted_model_fields.fields
from django.db import migrations


def delete_licence_files(apps, schema_editor):
    from komute.private_media import delete_stored_file

    CustomUser = apps.get_model('komute', 'CustomUser')
    users = CustomUser.objects.exclude(license_front_url__isnull=True, license_back_url__isnull=True)
    for user in users.only('id', 'license_front_url', 'license_back_url'):
        delete_stored_file(user.license_front_url)
        delete_stored_file(user.license_back_url)


def encrypt_licence_fields(apps, schema_editor):
    # Plain values still read back as-is (the field ignores tokens it can't
    # decrypt); saving them again writes them encrypted.
    CustomUser = apps.get_model('komute', 'CustomUser')
    users = CustomUser.objects.exclude(license_number__isnull=True, license_province__isnull=True)
    for user in users.only('id', 'license_number', 'license_province'):
        user.save(update_fields=['license_number', 'license_province'])


class Migration(migrations.Migration):

    dependencies = [
        ('komute', '0039_set_site_domain'),
    ]

    operations = [
        migrations.RunPython(delete_licence_files, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='customuser',
            name='license_front_url',
        ),
        migrations.RemoveField(
            model_name='customuser',
            name='license_back_url',
        ),
        migrations.AlterField(
            model_name='customuser',
            name='license_number',
            field=encrypted_model_fields.fields.EncryptedTextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='customuser',
            name='license_province',
            field=encrypted_model_fields.fields.EncryptedTextField(blank=True, null=True),
        ),
        migrations.RunPython(encrypt_licence_fields, migrations.RunPython.noop),
    ]
