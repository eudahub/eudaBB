"""
Create a compressed pg_dump snapshot of the forum database.

Usage:
    python manage.py snapshot_create
    python manage.py snapshot_create --name after_import
    python manage.py snapshot_create --output /path/to/file.dump

Output: SNAPSHOT_DIR/forum_<name>.dump  (custom format, zlib-6 compressed)

Restore with: python manage.py snapshot_restore [--name NAME]
"""

import os
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create compressed pg_dump snapshot (custom format, zlib-6)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--name", default="snapshot",
            help="Snapshot name (default: snapshot → forum_snapshot.dump)",
        )
        parser.add_argument(
            "--output", default="",
            help="Override output path (ignores --name and SNAPSHOT_DIR)",
        )

    def handle(self, *args, **options):
        if not shutil.which("pg_dump"):
            raise CommandError("pg_dump nie znalezione w PATH.")

        db = settings.DATABASES["default"]
        snapshot_dir = Path(getattr(settings, "SNAPSHOT_DIR", "snapshots"))
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        if options["output"]:
            output = Path(options["output"])
        else:
            output = snapshot_dir / f"forum_{options['name']}.dump"

        env = os.environ.copy()
        if db.get("PASSWORD"):
            env["PGPASSWORD"] = db["PASSWORD"]

        cmd = [
            "pg_dump",
            "-Fc",           # custom format — required for pg_restore -j
            "-Z", "6",       # zlib compression level 6
            "-h", db.get("HOST", "localhost"),
            "-p", str(db.get("PORT", 5432)),
            "-U", db["USER"],
            "-f", str(output),
            db["NAME"],
        ]

        self.stdout.write(f"Tworzę snapshot bazy '{db['NAME']}' → {output} ...")
        started = datetime.now()

        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(f"pg_dump zakończony błędem:\n{result.stderr}")

        elapsed = (datetime.now() - started).total_seconds()
        size_mb = output.stat().st_size / 1024 / 1024
        self.stdout.write(self.style.SUCCESS(
            f"Gotowe! {size_mb:.1f} MB w {elapsed:.0f}s → {output}"
        ))
