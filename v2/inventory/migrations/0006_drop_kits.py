# Packages are gone (Saged, 2026-11-25): a procedure's materials now come into the visit's use list instead.
# Lines first, then the packages they point at.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0005_movement_appointment'),
    ]

    operations = [
        migrations.DeleteModel(name='KitLine'),
        migrations.DeleteModel(name='Kit'),
    ]
