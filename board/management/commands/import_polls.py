import sqlite3

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from board.models import Forum, Poll, PollOption, Topic
from board.polls import parse_archive_datetime, parse_poll_results_text


class Command(BaseCommand):
    help = "Import archived read-only polls from sfiniabb.db"

    def add_arguments(self, parser):
        parser.add_argument("archive_db", help="Path to sfiniabb.db")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete existing polls before import",
        )

    def handle(self, *args, **options):
        db_path = options["archive_db"]
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as exc:
            raise CommandError(f"Cannot open {db_path}: {exc}")

        if options["clear"]:
            Poll.objects.all().delete()
            self.stdout.write("Cleared existing polls.")

        archive_forums = {
            row["forum_id"]: row["title"]
            for row in conn.execute("SELECT forum_id, title FROM forums").fetchall()
        }
        topic_rows = {
            row["topic_id"]: row
            for row in conn.execute(
                "SELECT topic_id, forum_id, title, has_poll FROM topics WHERE has_poll = 1"
            ).fetchall()
        }
        poll_rows = conn.execute("SELECT * FROM topic_polls ORDER BY topic_id").fetchall()
        conn.close()

        imported = 0
        skipped = []

        with transaction.atomic():
            for row in poll_rows:
                topic_meta = topic_rows.get(row["topic_id"])
                if not topic_meta:
                    skipped.append((row["topic_id"], "missing topic metadata"))
                    continue

                topic = Topic.objects.filter(archive_topic_id=row["topic_id"]).first()
                if topic is None:
                    forum_title = archive_forums.get(row["forum_id"])
                    if not forum_title:
                        skipped.append((row["topic_id"], "missing forum title"))
                        continue

                    topics = Topic.objects.filter(
                        forum__title=forum_title,
                        title=topic_meta["title"],
                    )
                    if not topics.exists():
                        skipped.append((row["topic_id"], "topic not found in Django"))
                        continue
                    if topics.count() > 1:
                        skipped.append((row["topic_id"], f"ambiguous topic match ({topics.count()})"))
                        continue
                    topic = topics.first()
                parsed = parse_poll_results_text(row["results_text"] or "")
                question = (row["question_text"] or parsed["question"]).strip()

                poll, _ = Poll.objects.update_or_create(
                    topic=topic,
                    defaults={
                        "question": question,
                        "created_at": topic.created_at,
                        "ends_at": None,
                        "is_closed": True,
                        "allow_vote_change": False,
                        "allow_multiple_choice": False,
                        "is_archived_import": True,
                        "total_votes": row["total_votes"] or parsed["total_votes"],
                        "source_visibility": row["source_visibility"] or 0,
                        "imported_results_text": row["results_text"] or "",
                        "imported_fetched_at": parse_archive_datetime(row["fetched_at"]),
                    },
                )
                poll.options.all().delete()
                PollOption.objects.bulk_create([
                    PollOption(
                        poll=poll,
                        option_text=option["option_text"],
                        vote_count=option["vote_count"],
                        sort_order=index,
                    )
                    for index, option in enumerate(parsed["options"], start=1)
                ])
                imported += 1

        self.stdout.write(self.style.SUCCESS(f"Imported polls: {imported}"))
        if skipped:
            self.stdout.write(f"Skipped: {len(skipped)}")
            for topic_id, reason in skipped[:20]:
                self.stdout.write(f"  topic_id={topic_id}: {reason}")
            if len(skipped) > 20:
                self.stdout.write("  ...")
