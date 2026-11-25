# A procedure's materials come off the stock when the visit is finished, not when it is recorded (Saged,
# 2026-11-25). Records made before this already took theirs, so the column comes in true (a constant default: no
# table rewrite) and stays true by default: the code running until the new one is live still deducts at recording,
# so its rows must not be taken again. The new code writes false itself.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clinical', '0009_procedure_family'),
    ]

    operations = [
        migrations.AddField(
            model_name='visitprocedure',
            name='stock_taken',
            field=models.BooleanField(db_default=True),
        ),
    ]
