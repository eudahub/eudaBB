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
import os
import sys
import time

from django.db import connection
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
        fast_clear = options["clear"]  # przy --clear: TRUNCATE + drop/recreate indexes

        if fast_clear:
            self._clear_and_drop_indexes()


        # Wczytaj towarzyszący plik nom_form (morph_families_nom.csv)
        nom_path = path.replace("morph_families.csv", "morph_families_nom.csv")
        if not nom_path.endswith("morph_families_nom.csv"):
            # fallback gdy ścieżka nie zawiera dokładnie morph_families.csv
            nom_path = path.replace(".csv", "_nom.csv")
        nom_map: dict[int, str] = {}
        if os.path.exists(nom_path):
            with open(nom_path, encoding="utf-8", newline="") as nf:
                for row in csv.DictReader(nf):
                    try:
                        nom_map[int(row["family_id"])] = row["nom_form"]
                    except (KeyError, ValueError):
                        pass
            self.stdout.write(f"Wczytano nom_form: {len(nom_map):,} rodzin z {nom_path}")
        else:
            self.stdout.write(
                f"Uwaga: brak {nom_path} — pole nom_form będzie puste "
                "(wyszukiwanie + będzie używać starej logiki lematu)."
            )

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

                nom_form = nom_map.get(family_id, "")

                batch.append(MorphForm(
                    form_norm=form_norm,
                    lemma_norm=lemma_norm,
                    family_id=family_id,
                    nom_form=nom_form,
                ))

                if len(batch) >= CHUNK:
                    MorphForm.objects.bulk_create(batch, ignore_conflicts=not fast_clear)
                    inserted += len(batch)
                    batch.clear()
                    self.stdout.write(
                        f"\r  {inserted:,} wierszy...", ending=""
                    )
                    sys.stdout.flush()

        if batch:
            MorphForm.objects.bulk_create(batch, ignore_conflicts=not fast_clear)
            inserted += len(batch)

        self.stdout.write(f"\n  Wstawiono: {inserted:,} (pominięto błędnych: {skipped})")

        if fast_clear:
            self._recreate_indexes()

        self.stdout.write(self.style.SUCCESS("Gotowe."))
        self.stdout.write(
            "Pamiętaj: po tej zmianie normalizacji uruchom też:\n"
            "  python manage.py build_search_index"
        )

    def _clear_and_drop_indexes(self):
        table = MorphForm._meta.db_table
        t0 = time.monotonic()
        with connection.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {table}")
            # Drop regular indexes (PK dropped last — needed by TRUNCATE above)
            cur.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename=%s AND indexname NOT LIKE '%%_pkey'",
                [table],
            )
            self._dropped_indexes = [r[0] for r in cur.fetchall()]
            for idx in self._dropped_indexes:
                cur.execute(f"DROP INDEX IF EXISTS {idx}")
            # Drop PK constraint
            cur.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pkey"
            )
        self.stdout.write(
            f"TRUNCATE + usunięto {len(self._dropped_indexes)+1} indeksów "
            f"({time.monotonic()-t0:.1f}s)"
        )

    def _recreate_indexes(self):
        table = MorphForm._meta.db_table
        self.stdout.write("Usuwam duplikaty...")
        t0 = time.monotonic()
        with connection.cursor() as cur:
            cur.execute(f"""
                DELETE FROM {table} a
                USING {table} b
                WHERE a.ctid < b.ctid
                  AND a.form_norm  = b.form_norm
                  AND a.lemma_norm = b.lemma_norm
                  AND a.family_id  = b.family_id
            """)
            removed = cur.rowcount
            if removed:
                self.stdout.write(f"  Usunięto {removed} duplikatów ({time.monotonic()-t0:.1f}s)")
        self.stdout.write("Tworzę indeksy...")
        t0 = time.monotonic()
        with connection.cursor() as cur:
            cur.execute(
                f"ALTER TABLE {table} ADD PRIMARY KEY (form_norm, lemma_norm, family_id)"
            )
            self.stdout.write(f"  PRIMARY KEY ({time.monotonic()-t0:.1f}s)")
            t1 = time.monotonic()
            cur.execute(
                f"CREATE INDEX forum_morph_form_no_627cb1_idx ON {table} (form_norm)"
            )
            self.stdout.write(f"  form_norm index ({time.monotonic()-t1:.1f}s)")
            t2 = time.monotonic()
            cur.execute(
                f"CREATE INDEX forum_morph_lemma_n_0eff89_idx ON {table} (lemma_norm, family_id)"
            )
            self.stdout.write(f"  lemma_norm/family_id index ({time.monotonic()-t2:.1f}s)")
        self.stdout.write(f"Indeksy gotowe ({time.monotonic()-t0:.1f}s łącznie)")
