"""Fetch TOR exit node IPs from dan.me.uk and update the local DB table.

Run hourly via cron:
    0 * * * * /path/to/venv/bin/python manage.py refresh_tor_list

Or on-demand:
    python manage.py refresh_tor_list
"""
import urllib.request
import urllib.error
from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.utils import timezone

from board.models import TorExitNode

TOR_LIST_URL = "https://www.dan.me.uk/torlist/?exit"
CACHE_KEY = "tor_exit_ips"
CACHE_TIMEOUT = 7200  # 2h — covers gaps between hourly refreshes


class Command(BaseCommand):
    help = "Fetch TOR exit node IPs and update the DB cache."

    def handle(self, *args, **options):
        self.stdout.write("Fetching TOR exit node list...")
        try:
            req = urllib.request.Request(
                TOR_LIST_URL,
                headers={"User-Agent": "eudaBB-tor-check/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("ascii", errors="ignore")
        except urllib.error.URLError as exc:
            self.stderr.write(f"Fetch failed: {exc} — keeping existing list.")
            return

        ips = {line.strip() for line in body.splitlines() if line.strip()}
        if not ips:
            self.stderr.write("Empty IP list returned — keeping existing list.")
            return

        # Upsert: add new IPs, remove stale ones
        existing = set(TorExitNode.objects.values_list("ip_address", flat=True))
        to_add = ips - existing
        to_remove = existing - ips

        if to_add:
            TorExitNode.objects.bulk_create(
                [TorExitNode(ip_address=ip) for ip in to_add],
                ignore_conflicts=True,
            )
        if to_remove:
            TorExitNode.objects.filter(ip_address__in=to_remove).delete()

        # Refresh cache immediately so middleware picks up new list without delay
        cache.set(CACHE_KEY, frozenset(ips), CACHE_TIMEOUT)

        self.stdout.write(
            self.style.SUCCESS(
                f"TOR list updated: {len(ips)} IPs total "
                f"(+{len(to_add)} added, -{len(to_remove)} removed)."
            )
        )
