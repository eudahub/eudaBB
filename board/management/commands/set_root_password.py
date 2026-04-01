"""Reset root password using client-side prehash scheme."""

import getpass

from django.core.management.base import BaseCommand, CommandError

from board.models import User
from board.auth_utils import prehash_password


class Command(BaseCommand):
    help = "Reset root password (prehash-aware)"

    def handle(self, *args, **options):
        try:
            root = User.objects.get(username="root", is_root=True)
        except User.DoesNotExist:
            raise CommandError("Root account not found. Run create_root first.")

        password = getpass.getpass("New password for root: ")
        if not password:
            raise CommandError("Password cannot be empty.")
        confirm = getpass.getpass("Confirm: ")
        if password != confirm:
            raise CommandError("Passwords do not match.")

        root.set_password(prehash_password(password, "root"))
        root.save(update_fields=["password"])
        self.stdout.write(self.style.SUCCESS("Root password updated."))
