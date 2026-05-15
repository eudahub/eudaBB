"""
Import sections and forums from sfiniabb.db SQLite archive.

Usage:
    python manage.py import_forums /path/to/sfiniabb.db [--clear]

Options:
    --clear   Delete all existing sections and forums before import.

The command handles subforum nesting (parent_board_id) and runs in two
passes so parents always exist before children.
"""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from board.models import Board, Section

_UTC = ZoneInfo("UTC")


def _dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC)
    except ValueError:
        return None


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
            Board.objects.all().delete()
            Section.objects.all().delete()
            self.stdout.write("Cleared existing sections and forums.")

        # --- Sections ---
        # Używamy kolumny "order" jeśli istnieje, fallback na section_id
        section_cols = {r[1] for r in conn.execute("PRAGMA table_info(sections)").fetchall()}
        if "order" in section_cols:
            sections_sql = 'SELECT section_id, title, "order" FROM sections ORDER BY "order"'
        else:
            sections_sql = "SELECT section_id, title, section_id AS \"order\" FROM sections ORDER BY section_id"
        rows = conn.execute(sections_sql).fetchall()
        section_map = {}  # source section_id → Section instance
        for row in rows:
            section, created = Section.objects.get_or_create(
                title=row["title"],
                defaults={"order": row["order"]},
            )
            if not created and section.order != row["order"]:
                section.order = row["order"]
                section.save(update_fields=["order"])
            section_map[row["section_id"]] = section

        self.stdout.write(f"Sections: {len(section_map)}")

        # --- Boards (two passes for parent/child) ---
        # Używamy kolumny "order" jeśli istnieje, fallback na board_id
        board_cols = {r[1] for r in conn.execute("PRAGMA table_info(boards)").fetchall()}
        order_col = '"order"' if "order" in board_cols else "board_id"
        has_last_post_at = "last_post_at" in board_cols
        last_post_at_col = ", last_post_at" if has_last_post_at else ""
        # Note: topic_count, post_count are NOT imported — maintained by the app.
        # Note: moderator_names, last_post_author, last_post_author_url, last_post_url
        # have no corresponding Django Board model fields — they are not imported.
        boards = conn.execute(
            f"SELECT board_id, section_id, parent_board_id, title, description, "
            f"visibility AS visibility_class, {order_col} AS display_order"
            f"{last_post_at_col} "
            f"FROM boards ORDER BY board_id"
        ).fetchall()

        board_map = {}  # source board_id → Board instance

        def get_section(row):
            """Return Section for this forum, falling back to parent's section."""
            if row["section_id"] and row["section_id"] in section_map:
                return section_map[row["section_id"]]
            if row["parent_board_id"] and row["parent_board_id"] in board_map:
                return board_map[row["parent_board_id"]].section
            # Last resort: first section
            return next(iter(section_map.values()))

        def import_row(row):
            parent = None
            if row["parent_board_id"]:
                parent = board_map.get(row["parent_board_id"])
                if parent is None:
                    return False  # parent not yet imported

            section = get_section(row)
            last_post_at_val = _dt(row["last_post_at"]) if has_last_post_at else None
            board, created = Board.objects.get_or_create(
                title=row["title"],
                section=section,
                defaults={
                    "description": row["description"] or "",
                    "parent": parent,
                    "order": row["display_order"],
                    # visibility_class from phpBB: 0=public, 1=registered, 2=admin
                    "access_level": row["visibility_class"] or 0,
                    "last_post_at": last_post_at_val,
                },
            )
            if not created:
                update_fields = []
                if board.order != row["display_order"]:
                    board.order = row["display_order"]
                    update_fields.append("order")
                if has_last_post_at:
                    board.last_post_at = last_post_at_val
                    update_fields.append("last_post_at")
                if update_fields:
                    board.save(update_fields=update_fields)
            board_map[row["board_id"]] = board
            return True

        # Pass 1: boards without parents (or with already-imported parents)
        remaining = list(boards)
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
        self.stdout.write(f"Boards imported: {len(board_map)}")
        self.stdout.write(self.style.SUCCESS("Done."))
