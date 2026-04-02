"""
Set archive_level on forums that should be visible only to spam-class users.

SOFT (1) — visible to GRAY + WEB users:
  Blog: IroB, Blog: hushek

HARD (2) — visible to WEB users only:
  Śmietnik, Więzienie, Magiel więzienny, Gwiezdne wojny

Usage:
    python manage.py set_spam_forums [--dry-run]
"""

from django.core.management.base import BaseCommand
from board.models import Forum

SOFT_FORUMS = ["Blog: IroB", "Blog: hushek"]
HARD_FORUMS = ["Śmietnik", "Więzienie", "Magiel więzienny", "Gwiezdne Wojny"]


class Command(BaseCommand):
    help = "Set archive_level on spam/restricted forums"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only print, don't save")

    def handle(self, *args, **options):
        dry = options["dry_run"]

        for title in SOFT_FORUMS:
            self._set(title, Forum.ArchiveLevel.SOFT, dry)
        for title in HARD_FORUMS:
            self._set(title, Forum.ArchiveLevel.HARD, dry)

    def _set(self, title, level, dry):
        try:
            forum = Forum.objects.get(title=title)
        except Forum.DoesNotExist:
            self.stdout.write(self.style.WARNING(f"  Nie znaleziono: '{title}'"))
            return
        except Forum.MultipleObjectsReturned:
            self.stdout.write(self.style.WARNING(f"  Duplikat tytułu: '{title}' — pomiń"))
            return

        label = dict(Forum.ArchiveLevel.choices)[level]
        if dry:
            self.stdout.write(f"  [dry] '{title}' → {label}")
        else:
            forum.archive_level = level
            forum.save(update_fields=["archive_level"])
            self.stdout.write(self.style.SUCCESS(f"  '{title}' → {label}"))
