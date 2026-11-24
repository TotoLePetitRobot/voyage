from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [("trips", "0014_legs_to_participations")]

    operations = [migrations.AddField(
                  model_name="trip", name="tabs_config",
                  field=models.JSONField(blank=True, default=dict, 
                                         verbose_name="Configuration des onglets")),
                  migrations.AlterField(
                  model_name="member",name="user",
                  field=models.ForeignKey(blank=True, null=True,
                                          on_delete=django.db.models.deletion.CASCADE,
                                          related_name="memberships", to=settings.AUTH_USER_MODEL)),
                  migrations.AlterConstraint(
                  model_name="member", name="unique_member_per_trip",
                  constraint=models.UniqueConstraint(
                      condition=models.Q(user__isnull=False),
                      fields=["trip", "user"],
                      name="unique_member_per_trip"))]
