# Whose number a file has (Saged, 2026-11-17): existing files keep it as their own (phone_owner "self"). The name
# skeleton is a stored generated column, so adding it rewrites the table once; its index serves search with a slip.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clinics', '0005_money_view'),
        ('patients', '0003_tag_name_en'),
    ]

    operations = [
        migrations.AddField(
            model_name='patient',
            name='guardian_relation',
            field=models.CharField(blank=True, choices=[('', 'None'), ('father', 'Father'), ('mother', 'Mother'), ('other', 'Other')], db_default='', default='', max_length=8),
        ),
        migrations.AddField(
            model_name='patient',
            name='name_skel',
            field=models.GeneratedField(db_persist=True, expression=models.Func(models.Func(models.F('name_norm'), models.Value('[اويءaeiouy ]'), models.Value(''), models.Value('g'), function='REGEXP_REPLACE'), models.Value('(.)\\1+'), models.Value('\\1'), models.Value('g'), function='REGEXP_REPLACE'), output_field=models.CharField(db_collation='C', max_length=240)),
        ),
        migrations.AddField(
            model_name='patient',
            name='phone_owner',
            field=models.CharField(choices=[('self', 'Self'), ('guardian', 'Guardian')], db_default='self', default='self', max_length=8),
        ),
        migrations.AddIndex(
            model_name='patient',
            index=models.Index(fields=['clinic', 'name_skel'], include=('id', 'full_name', 'phone', 'file_no'), name='patient_skel_search'),
        ),
        migrations.AddConstraint(
            model_name='patient',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('guardian_relation', ''), ('phone_owner', 'self')), models.Q(('guardian_relation__in', ['father', 'mother', 'other']), ('phone_owner', 'guardian')), _connector='OR'), name='patient_phone_owner_valid'),
        ),
    ]
