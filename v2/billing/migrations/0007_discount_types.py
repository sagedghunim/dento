# Discount types (Saged, 2026-11-13): the clinic's own discounts, and the type an invoice carries.
# Written from Django 6.1.3's output on 2026-11-13, reordered so the type's (clinic_id, id) key exists before the
# invoice's foreign key to it, with row-level security on the new table.

import core.constraints
import django.db.models.deletion
import django.db.models.expressions
import django.utils.timezone
from django.db import migrations, models

from core.rls import EnableRLS


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0006_patient_package_void'),
        ('clinics', '0004_fill_english'),
        ('patients', '0003_tag_name_en'),
        ('scheduling', '0004_visit_stages'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiscountType',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=60)),
                ('kind', models.CharField(max_length=7)),
                ('value', models.BigIntegerField()),
                ('desk_can_apply', models.BooleanField(default=True)),
                ('is_active', models.BooleanField(default=True)),
                ('sort', models.SmallIntegerField(default=0)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('clinic', models.ForeignKey(db_constraint=False, db_index=False, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='clinics.clinic')),
            ],
            options={
                'db_table': 'billing_discount_type',
            },
        ),
        migrations.AddConstraint(
            model_name='discounttype',
            constraint=models.UniqueConstraint(fields=('clinic', 'id'), name='discount_type_clinic_id_uniq'),
        ),
        migrations.AddConstraint(
            model_name='discounttype',
            constraint=models.UniqueConstraint(condition=models.Q(('is_active', True)), fields=('clinic', 'name'), name='discount_type_active_name_uniq'),
        ),
        migrations.AddConstraint(
            model_name='discounttype',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('kind', 'percent'), ('value__gte', 1), ('value__lte', 100)), models.Q(('kind', 'fixed'), ('value__gte', 1)), _connector='OR'), name='discount_type_value_valid'),
        ),
        EnableRLS("billing_discount_type"),
        # Three nullable or defaulted columns: no rewrite of the invoice table.
        migrations.AddField(
            model_name='invoice',
            name='discount_name',
            field=models.CharField(blank=True, db_default='', max_length=60),
        ),
        migrations.AddField(
            model_name='invoice',
            name='discount_percent',
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='discount_type',
            field=models.ForeignKey(blank=True, db_constraint=False, db_index=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='billing.discounttype'),
        ),
        migrations.AddConstraint(
            model_name='invoice',
            constraint=core.constraints.CompositeForeignKey(fields=('clinic', 'discount_type'), name='invoice_discount_type_fk', to='billing.DiscountType', to_fields=('clinic', 'id')),
        ),
        migrations.AddConstraint(
            model_name='invoice',
            constraint=models.CheckConstraint(condition=models.Q(('discount_type__isnull', False), models.Q(('discount_name', ''), ('discount_percent__isnull', True)), _connector='OR'), name='invoice_discount_type_valid'),
        ),
        migrations.AddConstraint(
            model_name='invoice',
            constraint=models.CheckConstraint(condition=models.Q(('discount_percent__isnull', True), models.Q(('discount', django.db.models.expressions.CombinedExpression(django.db.models.expressions.CombinedExpression(django.db.models.expressions.CombinedExpression(models.F('subtotal'), '*', models.F('discount_percent')), '+', models.Value(50)), '/', models.Value(100))), ('discount_percent__gte', 1), ('discount_percent__lte', 100)), _connector='OR'), name='invoice_discount_percent_valid'),
        ),
    ]
