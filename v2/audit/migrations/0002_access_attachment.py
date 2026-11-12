# Access action 8: opening an attachment (design E3). The CHECK is inherited by every monthly partition, so
# dropping and adding it on the parent changes them all; adding it scans the partitions once (a small log in M2).

from django.db import migrations, models

ACTIONS = [(1, 'Open'), (2, 'Print'), (3, 'Export'), (4, 'Note'), (5, 'Prescription'), (6, 'Invoice'), (7, 'History'),
           (8, 'Attachment')]


def _check(high):
    return [
        'ALTER TABLE audit_access DROP CONSTRAINT IF EXISTS audit_access_action_check',
        f'ALTER TABLE audit_access ADD CONSTRAINT audit_access_action_check CHECK (action BETWEEN 1 AND {high})',
    ]


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterField(
                model_name='auditaccess',
                name='action',
                field=models.SmallIntegerField(choices=ACTIONS),
            )],
            database_operations=[migrations.RunSQL(sql=_check(8), reverse_sql=_check(7))],
        ),
    ]
