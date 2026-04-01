"""
Create the root superadmin account.

Usage:
    python manage.py create_root

Prompts for a password interactively (not echoed).
Root has no email, no email_hash, no password reset, cannot post.
Only one root account can exist (enforced by DB constraint).
"""

import getpass

from django.core.management.base import BaseCommand, CommandError

from board.models import User
from board.auth_utils import prehash_password


class Command(BaseCommand):
    help = "Create the root superadmin account (username='root')"

    def handle(self, *args, **options):
        if User.objects.filter(is_root=True).exists():
            raise CommandError("Root account already exists.")

        if User.objects.filter(username="root").exists():
            raise CommandError(
                "A user named 'root' already exists but is not flagged is_root. "
                "Remove or rename it first."
            )

        password = getpass.getpass("Password for root: ")
        if not password:
            raise CommandError("Password cannot be empty.")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise CommandError("Passwords do not match.")

        root = User(
            username="root",
            is_root=True,
            is_superuser=True,
            is_staff=True,
            is_active=True,
            email="",
            email_hash="",
            email_mask="",
        )
        root.set_password(prehash_password(password, "root"))
        root.save()

        self.stdout.write(self.style.SUCCESS(
            "Root account created. Log in at /admin/ with username 'root'."
        ))
