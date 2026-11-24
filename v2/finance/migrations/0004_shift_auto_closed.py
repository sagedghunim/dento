from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0003_shift_auto_opened"),
    ]

    operations = [
        migrations.AddField(
            model_name="shift",
            name="auto_closed",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
