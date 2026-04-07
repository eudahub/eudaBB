from django.core.management.base import BaseCommand, CommandError

from board.models import Forum, Post
from board.search_index import rebuild_post_search_index_for_posts


class Command(BaseCommand):
    help = "Build forum_post_search from existing posts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--forum-id",
            type=int,
            help="Restrict rebuild to one forum (recommended for first runs).",
        )
        parser.add_argument(
            "--forum-title",
            help='Restrict rebuild to one forum selected by exact title. Use quotes if the title contains spaces.',
        )

    def handle(self, *args, **options):
        posts = Post.objects.select_related("topic", "topic__forum", "author").order_by("pk")
        forum_id = options.get("forum_id")
        forum_title = (options.get("forum_title") or "").strip()

        if forum_id is not None and forum_title:
            raise CommandError("Use only one of --forum-id or --forum-title.")

        if forum_id is not None:
            if not Forum.objects.filter(pk=forum_id).exists():
                raise CommandError(f"Forum id={forum_id} does not exist.")
            posts = posts.filter(topic__forum_id=forum_id)
            self.stdout.write(f"Buduję indeks wyszukiwania dla forum_id={forum_id}…")
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
            posts = posts.filter(topic__forum=forum)
            self.stdout.write(
                f'Buduję indeks wyszukiwania dla forum "{forum.title}" (id={forum.pk})…'
            )
        else:
            self.stdout.write("Buduję indeks wyszukiwania dla wszystkich forów…")

        total = rebuild_post_search_index_for_posts(posts)
        self.stdout.write(self.style.SUCCESS(
            f"Gotowe. Zbudowano rekordów indeksu: {total}"
        ))
