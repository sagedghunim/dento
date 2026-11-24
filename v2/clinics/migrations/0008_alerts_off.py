from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinics", "0007_booking_from_price_list_and_work_hours"),
    ]

    operations = [
        migrations.AddField(
            model_name="membership",
            name="alerts_off",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
