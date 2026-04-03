"""Null out author_ip on posts whose retention period has expired.

Run daily via cron:
    0 3 * * * /path/to/venv/bin/python manage.py purge_expired_ips

Normal posts: IP kept for IP_RETAIN_NORMAL_DAYS (default 30).
Flagged posts: IP kept for IP_RETAIN_DANGEROUS_DAYS (default 90).
After expiry, author_ip is set to NULL — the post itself remains.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from board.models import Post


class Command(BaseCommand):
    help = "Purge author_ip from posts whose retention period has expired."

    def handle(self, *args, **options):
        now = timezone.now()
        expired = Post.objects.filter(
            author_ip__isnull=False,
            ip_retain_until__isnull=False,
            ip_retain_until__lte=now,
        )
        count = expired.update(author_ip=None, ip_retain_until=None)
        self.stdout.write(
            self.style.SUCCESS(f"Purged author_ip from {count} posts.")
        )
