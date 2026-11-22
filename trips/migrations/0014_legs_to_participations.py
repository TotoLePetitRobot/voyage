from django.db import migrations


def move_prices_to_participations(apps, schema_editor):
    Leg = apps.get_model("trips", "Leg")
    LegParticipant = apps.get_model("trips", "LegParticipant")
    for leg in Leg.objects.exclude(transport="car").exclude(proposed_by=None):
        LegParticipant.objects.get_or_create(
            leg=leg, member_id=leg.proposed_by_id, defaults={"price": leg.price})
        if leg.price is not None:
            leg.price = None
            leg.save(update_fields=["price"])

class Migration(migrations.Migration):
    dependencies = [
        ("trips", "0013_alter_leg_options_remove_leg_position_legparticipant"),
    ]

    operations = [
        migrations.RunPython(move_prices_to_participations, migrations.RunPython.noop),
    ]
