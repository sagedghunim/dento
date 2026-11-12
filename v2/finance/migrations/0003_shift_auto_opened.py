from django.db import migrations, models


class Migration(migrations.Migration):
    """F1: cash never fails for want of a shift; a cash entry opens one (float 0) and marks it."""

    dependencies = [
        ("finance", "0002_category_name_en"),
    ]

    operations = [
        migrations.AddField(
            model_name="shift",
            name="auto_opened",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
