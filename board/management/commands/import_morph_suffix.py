"""
Importuje morph_suffixes.csv do tabeli forum_morph_suffix.

Użycie:
    python manage.py import_morph_suffix morph_suffixes.csv
    python manage.py import_morph_suffix morph_suffixes.csv --clear

CSV format: suffix_len,suffix,lemma,family_id
- suffix: już znormalizowany (obliczony z normalize(lemma)[-slen:] przy budowie CSV)
- lemma:  surowy (normalizacja następuje tu przy imporcie → lemma_norm)
"""

import csv
import sys

from django.core.management.base import BaseCommand, CommandError

from board.models import MorphSuffix
from board.search_index import normalize_search_text


CHUNK = 10_000


class Command(BaseCommand):
    help = "Importuje plik morph_suffixes.csv do tabeli MorphSuffix"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Ścieżka do morph_suffixes.csv")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Wyczyść tabelę przed importem",
        )

    def handle(self, *args, **options):
        path = options["csv_path"]
        if options["clear"]:
            count = MorphSuffix.objects.all().delete()[0]
            self.stdout.write(f"Wyczyszczono {count} wierszy.")

        batch: list[MorphSuffix] = []
        inserted = 0
        skipped = 0

        try:
            f = open(path, encoding="utf-8", newline="")
        except OSError as e:
            raise CommandError(str(e))

        with f:
            reader = csv.DictReader(f)
            if not {"suffix_len", "suffix", "lemma", "family_id"}.issubset(reader.fieldnames or []):
                raise CommandError("CSV musi mieć kolumny: suffix_len, suffix, lemma, family_id")

            for row in reader:
                try:
                    suffix_len = int(row["suffix_len"])
                    family_id  = int(row["family_id"])
                except ValueError:
                    skipped += 1
                    continue

                suffix     = row["suffix"]
                lemma_norm = normalize_search_text(row["lemma"])

                if not suffix or not lemma_norm:
                    skipped += 1
                    continue

                batch.append(MorphSuffix(
                    suffix_len=suffix_len,
                    suffix=suffix,
                    lemma_norm=lemma_norm,
                    family_id=family_id,
                ))

                if len(batch) >= CHUNK:
                    MorphSuffix.objects.bulk_create(batch, ignore_conflicts=True)
                    inserted += len(batch)
                    batch.clear()
                    self.stdout.write(f"\r  {inserted:,} wierszy...", ending="")
                    sys.stdout.flush()

        if batch:
            MorphSuffix.objects.bulk_create(batch, ignore_conflicts=True)
            inserted += len(batch)

        self.stdout.write(
            f"\nGotowe: {inserted:,} wierszy wstawionych"
            f" (pominięto błędnych: {skipped})."
        )
