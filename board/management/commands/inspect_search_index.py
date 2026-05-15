from django.core.management.base import BaseCommand, CommandError

from board.models import Board, PostSearchIndex


class Command(BaseCommand):
    help = "Show a sample of forum_post_search rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--board-id",
            type=int,
            help="Restrict output to one board id.",
        )
        parser.add_argument(
            "--board-title",
            help='Restrict output to one board selected by exact title. Use quotes if the title contains spaces.',
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="How many rows to print (default: 20).",
        )

    def handle(self, *args, **options):
        board_id = options.get("board_id")
        board_title = (options.get("board_title") or "").strip()
        limit = max(1, options.get("limit") or 20)

        if board_id is not None and board_title:
            raise CommandError("Use only one of --board-id or --board-title.")

        qs = PostSearchIndex.objects.select_related("post", "board", "topic", "author").order_by("post_id")
        if board_id is not None:
            if not Board.objects.filter(pk=board_id).exists():
                raise CommandError(f"Board id={board_id} does not exist.")
            qs = qs.filter(board_id=board_id)
            self.stdout.write(f"Podgląd indeksu dla board_id={board_id}")
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
            qs = qs.filter(board=board)
            self.stdout.write(f'Podgląd indeksu dla działu "{board.title}" (id={board.pk})')
        else:
            self.stdout.write("Podgląd indeksu dla wszystkich działów")

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
                f"post={row.post_id} board={row.board_id} topic={row.topic_id} author={author} "
                f"created={row.created_at:%Y-%m-%d %H:%M}"
            )
            self.stdout.write(f"  {preview}")
