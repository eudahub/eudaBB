import time

from django.core.management.base import BaseCommand, CommandError

from board.models import Board, Post
from board.search_index import rebuild_post_search_index_for_posts


class Command(BaseCommand):
    help = "Build forum_post_search from existing posts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--board-id",
            type=int,
            help="Restrict rebuild to one board (recommended for first runs).",
        )
        parser.add_argument(
            "--board-title",
            help='Restrict rebuild to one board selected by exact title. Use quotes if the title contains spaces.',
        )

    def handle(self, *args, **options):
        started = time.monotonic()
        posts = Post.objects.select_related("topic", "topic__board", "author").order_by("pk")
        board_id = options.get("board_id")
        board_title = (options.get("board_title") or "").strip()

        if board_id is not None and board_title:
            raise CommandError("Use only one of --board-id or --board-title.")

        if board_id is not None:
            if not Board.objects.filter(pk=board_id).exists():
                raise CommandError(f"Board id={board_id} does not exist.")
            posts = posts.filter(topic__board_id=board_id)
            self.stdout.write(f"Buduję indeks wyszukiwania dla board_id={board_id}…")
        elif board_title:
            boards = list(Board.objects.filter(title=board_title))
            if not boards:
                raise CommandError(f'Board with title "{board_title}" does not exist.')
            if len(boards) > 1:
                raise CommandError(
                    f'Board title "{board_title}" is ambiguous ({len(boards)} matches). '
                    "Use --board-id instead."
                )
            board = boards[0]
            posts = posts.filter(topic__board=board)
            self.stdout.write(
                f'Buduję indeks wyszukiwania dla działu "{board.title}" (id={board.pk})…'
            )
        else:
            self.stdout.write("Buduję indeks wyszukiwania dla wszystkich działów…")

        total = rebuild_post_search_index_for_posts(posts)
        elapsed = time.monotonic() - started
        self.stdout.write(self.style.SUCCESS(
            f"Gotowe. Zbudowano rekordów indeksu: {total}"
        ))
        self.stdout.write(
            f"Czas: {elapsed:.1f} s ({elapsed / 60:.2f} min)"
        )
