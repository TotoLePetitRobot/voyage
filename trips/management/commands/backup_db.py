from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from trips.backup import backup_sqlite


class Command(BaseCommand):
    help = "Sauvegarde la base SQLite et conserve les N copies les plus récentes."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=str(Path.home() / "backups"))
        parser.add_argument("--keep", type=int, default=7)

    def handle(self, *args, **opts):
        db = settings.DATABASES["default"]
        if db["ENGINE"] != "django.db.backends.sqlite3":
            raise CommandError("Cette commande ne gère que SQLite.")
        try:
            dest = backup_sqlite(db["NAME"], opts["dir"], opts["keep"])
        except (OSError, ValueError, RuntimeError) as e:
            raise CommandError(str(e))
        size = dest.stat().st_size / 1024
        self.stdout.write(self.style.SUCCESS(f"Sauvegarde créée : {dest} ({size:.0f} Ko)"))
