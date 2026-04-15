"""Import spamboxes table from spamboxes.db (SQLite) into Django PostgreSQL DB."""

import sqlite3
from django.core.management.base import BaseCommand
from board.models import SpamDomain


SPAMBOXES_DB = "/home/andrzej/wazne/gitmy/phpbb-archiver/spamboxes.db"


class Command(BaseCommand):
    help = "Import spamboxes from spamboxes.db into forum_spam_domain table"

    def handle(self, *args, **options):
        conn = sqlite3.connect(SPAMBOXES_DB)
        cur = conn.cursor()
        cur.execute("SELECT domain, spam FROM spamboxes WHERE spam IS NOT NULL")
        rows = cur.fetchall()
        conn.close()

        SpamDomain.objects.all().delete()
        objs = [SpamDomain(domain=domain, spam=spam) for domain, spam in rows]
        SpamDomain.objects.bulk_create(objs, batch_size=1000)

        self.stdout.write(self.style.SUCCESS(f"Zaimportowano {len(objs)} domen."))
