"""
Create the root superadmin account.

Usage:
    python manage.py create_root

Uses ROOT_PASSWORD from .env / environment when available, otherwise prompts
for a password interactively (not echoed).
Root has no email, no email_hash, no password reset, cannot post.
Only one root account can exist (enforced by DB constraint).
"""

import getpass

from django.core.management.base import BaseCommand, CommandError
from decouple import config

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

        env_password = config("ROOT_PASSWORD", default="")
        password = env_password
        if password:
            self.stdout.write("Using ROOT_PASSWORD from environment/.env.")
        else:
            password = getpass.getpass("Password for root: ")
        if not password:
            raise CommandError("Password cannot be empty.")
        if not env_password:
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
        )
        root.set_password(prehash_password(password, "root"))
        root.save()

        self.stdout.write(self.style.SUCCESS(
            "Root account created. Log in at /admin/ with username 'root'."
        ))
