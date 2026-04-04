"""
Restore database from a pg_dump snapshot (custom format).

Usage:
    python manage.py snapshot_restore
    python manage.py snapshot_restore --name after_import
    python manage.py snapshot_restore --input /path/to/file.dump
    python manage.py snapshot_restore --yes            # skip confirmation

WARNING: drops and recreates the entire database.
Stop the Django dev server before running this command.

Slow part: rebuilding indexes after INSERT (same as re-import, but no network).
Speed up with --jobs N (parallel index builds, default: 2).
"""

import os
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

from django import db as django_db
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Restore database from compressed pg_dump snapshot"

    def add_arguments(self, parser):
        parser.add_argument(
            "--name", default="snapshot",
            help="Snapshot name (default: snapshot → forum_snapshot.dump)",
        )
        parser.add_argument(
            "--input", default="",
            help="Override input path (ignores --name and SNAPSHOT_DIR)",
        )
        parser.add_argument(
            "--yes", action="store_true",
            help="Skip confirmation prompt",
        )
        parser.add_argument(
            "--jobs", "-j", type=int, default=2,
            help="Parallel pg_restore jobs for index rebuilds (default: 2)",
        )

    def handle(self, *args, **options):
        for tool in ("dropdb", "createdb", "pg_restore"):
            if not shutil.which(tool):
                raise CommandError(f"{tool} nie znalezione w PATH.")

        db = settings.DATABASES["default"]
        snapshot_dir = Path(getattr(settings, "SNAPSHOT_DIR", "snapshots"))

        if options["input"]:
            input_file = Path(options["input"])
        else:
            input_file = snapshot_dir / f"forum_{options['name']}.dump"

        if not input_file.exists():
            raise CommandError(
                f"Plik snapshotu nie istnieje: {input_file}\n"
                f"Utwórz go przez: python manage.py snapshot_create --name {options['name']}"
            )

        size_mb = input_file.stat().st_size / 1024 / 1024

        if not options["yes"]:
            self.stdout.write(self.style.WARNING(
                f"\n{'='*60}\n"
                f"UWAGA: Baza '{db['NAME']}' zostanie CAŁKOWICIE ZASTĄPIONA.\n"
                f"Snapshot: {input_file} ({size_mb:.1f} MB)\n"
                f"Zatrzymaj serwer Django przed kontynuowaniem!\n"
                f"{'='*60}\n"
            ))
            confirm = input("Wpisz 'tak' aby kontynuować: ").strip().lower()
            if confirm != "tak":
                self.stdout.write("Anulowano.")
                return

        env = os.environ.copy()
        if db.get("PASSWORD"):
            env["PGPASSWORD"] = db["PASSWORD"]

        host = db.get("HOST", "localhost")
        port = str(db.get("PORT", 5432))
        user = db["USER"]
        dbname = db["NAME"]

        # Close all Django DB connections before drop
        django_db.connections.close_all()

        started = datetime.now()

        # Drop existing DB
        self.stdout.write("1/3  Usuwam bazę danych...")
        result = subprocess.run(
            ["dropdb", "-h", host, "-p", port, "-U", user, dbname],
            env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise CommandError(f"dropdb błąd: {result.stderr}")

        # Recreate empty DB
        self.stdout.write("2/3  Tworzę pustą bazę...")
        result = subprocess.run(
            ["createdb", "-h", host, "-p", port, "-U", user, dbname],
            env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise CommandError(f"createdb błąd: {result.stderr}")

        # Restore data + indexes
        self.stdout.write(
            f"3/3  Przywracam dane i odbudowuję indeksy "
            f"(--jobs {options['jobs']}, to najwolniejszy krok)..."
        )
        result = subprocess.run(
            [
                "pg_restore",
                "-h", host, "-p", port, "-U", user,
                "-d", dbname,
                "-j", str(options["jobs"]),
                "--no-owner",
                str(input_file),
            ],
            env=env, capture_output=True, text=True,
        )
        # pg_restore exits non-zero even for harmless warnings — only fail on real errors
        if result.returncode not in (0, 1):
            raise CommandError(f"pg_restore błąd (kod {result.returncode}):\n{result.stderr}")
        if result.stderr:
            self.stdout.write(f"Ostrzeżenia pg_restore (ignorowalne):\n{result.stderr[:400]}")

        elapsed = (datetime.now() - started).total_seconds()
        self.stdout.write(self.style.SUCCESS(
            f"\nSnapshot przywrócony w {elapsed:.0f}s.\n"
            f"Uruchom serwer Django: python manage.py runserver"
        ))
