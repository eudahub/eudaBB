"""
Czyści wszystkie tabele aplikacji POZA słownikiem morfologicznym (MorphForm, MorphSuffix).
Używa TRUNCATE … RESTART IDENTITY CASCADE (PostgreSQL).

Użycie:
    python manage.py flush_except_morph
    python manage.py flush_except_morph --no-input   # bez potwierdzenia
"""

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection

from board.models import MorphForm, MorphSuffix


PRESERVE_TABLES = frozenset({
    MorphForm._meta.db_table,
    MorphSuffix._meta.db_table,
})


class Command(BaseCommand):
    help = "Truncate all app tables except MorphForm/MorphSuffix (preserves morph dictionary)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-input", "--noinput",
            action="store_true",
            dest="no_input",
            help="Nie pytaj o potwierdzenie.",
        )

    def handle(self, *args, **options):
        if not options["no_input"]:
            answer = input(
                "Czy na pewno usunąć wszystkie dane (poza morfologią)? [tak/N] "
            ).strip().lower()
            if answer != "tak":
                self.stdout.write("Anulowano.")
                return

        # Collect model tables, but only those that already exist in the DB
        # (new migrations may define tables not yet created).
        model_tables = {
            m._meta.db_table
            for m in apps.get_models()
            if m._meta.db_table not in PRESERVE_TABLES
            and m._meta.managed
        }
        with connection.cursor() as c:
            c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            existing = {row[0] for row in c.fetchall()}

        tables = sorted(model_tables & existing)
        if not tables:
            self.stdout.write("Brak tabel do wyczyszczenia.")
            return

        tables_sql = ", ".join(f'"{t}"' for t in tables)
        with connection.cursor() as c:
            c.execute(f"TRUNCATE {tables_sql} RESTART IDENTITY CASCADE")

        self.stdout.write(self.style.SUCCESS(
            f"Wyczyszczono {len(tables)} tabel (morfologia zachowana)."
        ))
