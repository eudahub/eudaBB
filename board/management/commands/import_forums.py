"""
Import sections and forums from sfiniabb.db SQLite archive.

Usage:
    python manage.py import_forums /path/to/sfiniabb.db [--clear]

Options:
    --clear   Delete all existing sections and forums before import.

The command handles subforum nesting (parent_forum_id) and runs in two
passes so parents always exist before children.
"""

import sqlite3

from django.core.management.base import BaseCommand, CommandError

from board.models import Forum, Section


class Command(BaseCommand):
    help = "Import sections and forums from sfiniabb.db"

    def add_arguments(self, parser):
        parser.add_argument("db_path", help="Path to sfiniabb.db")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear existing sections and forums before import",
        )

    def handle(self, *args, **options):
        db_path = options["db_path"]

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        if options["clear"]:
            Forum.objects.all().delete()
            Section.objects.all().delete()
            self.stdout.write("Cleared existing sections and forums.")

        # --- Sections ---
        rows = conn.execute("SELECT section_id, title FROM sections ORDER BY section_id").fetchall()
        section_map = {}  # source section_id → Section instance
        for i, row in enumerate(rows):
            section, created = Section.objects.get_or_create(
                title=row["title"],
                defaults={"order": i},
            )
            section_map[row["section_id"]] = section

        self.stdout.write(f"Sections: {len(section_map)}")

        # --- Forums (two passes for parent/child) ---
        forums = conn.execute(
            "SELECT forum_id, section_id, parent_forum_id, title, description, "
            "topic_count, post_count, visibility "
            "FROM forums ORDER BY forum_id"
        ).fetchall()

        forum_map = {}  # source forum_id → Forum instance

        def get_section(row):
            """Return Section for this forum, falling back to parent's section."""
            if row["section_id"] and row["section_id"] in section_map:
                return section_map[row["section_id"]]
            if row["parent_forum_id"] and row["parent_forum_id"] in forum_map:
                return forum_map[row["parent_forum_id"]].section
            # Last resort: first section
            return next(iter(section_map.values()))

        def import_row(row):
            parent = None
            if row["parent_forum_id"]:
                parent = forum_map.get(row["parent_forum_id"])
                if parent is None:
                    return False  # parent not yet imported

            section = get_section(row)
            forum, created = Forum.objects.get_or_create(
                title=row["title"],
                section=section,
                defaults={
                    "description": row["description"] or "",
                    "parent": parent,
                    "order": row["forum_id"],
                    "is_visible": row["visibility"] == 0,
                    "topic_count": row["topic_count"] or 0,
                    "post_count": row["post_count"] or 0,
                },
            )
            forum_map[row["forum_id"]] = forum
            return True

        # Pass 1: forums without parents (or with already-imported parents)
        remaining = list(forums)
        max_passes = 10
        for _ in range(max_passes):
            if not remaining:
                break
            still_remaining = []
            for row in remaining:
                if not import_row(row):
                    still_remaining.append(row)
            if len(still_remaining) == len(remaining):
                # No progress — circular or missing parents
                titles = [r["title"] for r in still_remaining]
                self.stderr.write(f"Could not resolve parents for: {titles}")
                break
            remaining = still_remaining

        conn.close()
        self.stdout.write(f"Forums imported: {len(forum_map)}")
        self.stdout.write(self.style.SUCCESS("Done."))
