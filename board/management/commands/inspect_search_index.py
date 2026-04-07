from django.core.management.base import BaseCommand, CommandError

from board.models import Forum, PostSearchIndex


class Command(BaseCommand):
    help = "Show a sample of forum_post_search rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--forum-id",
            type=int,
            help="Restrict output to one forum id.",
        )
        parser.add_argument(
            "--forum-title",
            help='Restrict output to one forum selected by exact title. Use quotes if the title contains spaces.',
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="How many rows to print (default: 20).",
        )

    def handle(self, *args, **options):
        forum_id = options.get("forum_id")
        forum_title = (options.get("forum_title") or "").strip()
        limit = max(1, options.get("limit") or 20)

        if forum_id is not None and forum_title:
            raise CommandError("Use only one of --forum-id or --forum-title.")

        qs = PostSearchIndex.objects.select_related("post", "forum", "topic", "author").order_by("post_id")
        if forum_id is not None:
            if not Forum.objects.filter(pk=forum_id).exists():
                raise CommandError(f"Forum id={forum_id} does not exist.")
            qs = qs.filter(forum_id=forum_id)
            self.stdout.write(f"Podgląd indeksu dla forum_id={forum_id}")
        elif forum_title:
            forums = list(Forum.objects.filter(title=forum_title))
            if not forums:
                raise CommandError(f'Forum with title "{forum_title}" does not exist.')
            if len(forums) > 1:
                raise CommandError(
                    f'Forum title "{forum_title}" is ambiguous ({len(forums)} matches). '
                    "Use --forum-id instead."
                )
            forum = forums[0]
            qs = qs.filter(forum=forum)
            self.stdout.write(f'Podgląd indeksu dla forum "{forum.title}" (id={forum.pk})')
        else:
            self.stdout.write("Podgląd indeksu dla wszystkich forów")

        rows = list(qs[:limit])
        if not rows:
            self.stdout.write("Brak rekordów.")
            return

        for row in rows:
            author = row.author.username if row.author else "[usunięty]"
            preview = (row.content_search_author or "").replace("\n", " ").strip()
            if len(preview) > 180:
                preview = preview[:177] + "..."
            self.stdout.write(
                f"post={row.post_id} forum={row.forum_id} topic={row.topic_id} author={author} "
                f"created={row.created_at:%Y-%m-%d %H:%M}"
            )
            self.stdout.write(f"  {preview}")
