import re

import django.contrib.auth.validators
import django.db.models.functions
from django.db import migrations, models


def unique_username(email, taken):
    base = re.sub(r"[^\w.@+-]", "", (email or "").split("@")[0])[:24] or "membre"
    name, n = base, 1
    while name.lower() in taken:
        n += 1
        name = f"{base}{n}"
    taken.add(name.lower())
    return name


def fill_usernames(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    taken = set()
    for user in User.objects.order_by("pk"):
        user.username = unique_username(user.email, taken)
        user.save(update_fields=["username"])


class Migration(migrations.Migration):

    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="user", name="email",
            field=models.EmailField(blank=True, max_length=254,
                                    verbose_name="E-mail de récupération (facultatif)"),
        ),
        migrations.AddField(
            model_name="user", name="username",
            field=models.CharField(max_length=30, null=True, verbose_name="Nom d’utilisateur"),
        ),
        migrations.RunPython(fill_usernames, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user", name="username",
            field=models.CharField(
                error_messages={"unique": "Ce nom d’utilisateur est déjà pris."},
                help_text="30 caractères maximum : lettres, chiffres et @ . + - _",
                max_length=30, unique=True,
                validators=[django.contrib.auth.validators.UnicodeUsernameValidator()],
                verbose_name="Nom d’utilisateur",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                django.db.models.functions.Lower("username"), name="unique_username_ci"),
        ),
    ]
