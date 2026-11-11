# The directory keeps a copy of each place's kind, time zone and day end, and how far the nightly
# jobs have got (draft 2 check, I6). Existing rows take the defaults; the next sweep repairs any copy
# that differs from its shard.

import datetime

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('directory', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='directoryclinic',
            name='kind',
            field=models.CharField(default='clinic', max_length=6),
        ),
        migrations.AddField(
            model_name='directoryclinic',
            name='timezone',
            field=models.CharField(default='Africa/Cairo', max_length=60),
        ),
        migrations.AddField(
            model_name='directoryclinic',
            name='day_ends_at',
            field=models.TimeField(default=datetime.time(4, 0)),
        ),
        migrations.AddField(
            model_name='directoryclinic',
            name='swept_through',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='directoryclinic',
            name='recomputed_through',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='directoryclinic',
            constraint=models.CheckConstraint(condition=models.Q(('kind__in', ['clinic', 'center'])), name='dir_clinic_kind_valid'),
        ),
    ]
