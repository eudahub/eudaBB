"""
Importuje morph_families.csv do tabeli forum_morph_form.

Użycie:
    python manage.py import_morph_csv morph_families.csv
    python manage.py import_morph_csv morph_families.csv --clear

Normalizacja (lowercase + diakrytyki + ł→l) wykonywana przy imporcie.
Po zmianie normalizacji (ł→l) konieczny też rebuild indeksu wyszukiwania:
    python manage.py build_search_index
"""

import csv
import sys

from django.core.management.base import BaseCommand, CommandError

from board.models import MorphForm
from board.search_index import normalize_search_text


CHUNK = 10_000


class Command(BaseCommand):
    help = "Importuje plik morph_families.csv do tabeli MorphForm"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Ścieżka do morph_families.csv")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Wyczyść tabelę przed importem",
        )

    def handle(self, *args, **options):
        path = options["csv_path"]
        if options["clear"]:
            count = MorphForm.objects.all().delete()[0]
            self.stdout.write(f"Wyczyszczono {count} wierszy.")

        batch: list[MorphForm] = []
        inserted = 0
        skipped = 0

        try:
            f = open(path, encoding="utf-8", newline="")
        except OSError as e:
            raise CommandError(str(e))

        with f:
            reader = csv.DictReader(f)
            if not {"form", "lemma", "family_id"}.issubset(reader.fieldnames or []):
                raise CommandError(
                    "CSV musi mieć kolumny: form, lemma, family_id"
                )

            for lineno, row in enumerate(reader, 2):
                form_norm  = normalize_search_text(row["form"])
                lemma_norm = normalize_search_text(row["lemma"])
                try:
                    family_id = int(row["family_id"])
                except ValueError:
                    skipped += 1
                    continue

                if not form_norm or not lemma_norm:
                    skipped += 1
                    continue

                batch.append(MorphForm(
                    form_norm=form_norm,
                    lemma_norm=lemma_norm,
                    family_id=family_id,
                ))

                if len(batch) >= CHUNK:
                    MorphForm.objects.bulk_create(batch, ignore_conflicts=True)
                    inserted += len(batch)
                    batch.clear()
                    self.stdout.write(
                        f"\r  {inserted:,} wierszy...", ending=""
                    )
                    sys.stdout.flush()

        if batch:
            MorphForm.objects.bulk_create(batch, ignore_conflicts=True)
            inserted += len(batch)

        self.stdout.write(
            f"\nGotowe: {inserted:,} wierszy wstawionych"
            f" (pominięto błędnych: {skipped})."
        )
        self.stdout.write(
            "Pamiętaj: po tej zmianie normalizacji uruchom też:\n"
            "  python manage.py build_search_index"
        )
