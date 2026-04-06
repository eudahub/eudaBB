from django.core.management.base import BaseCommand

from board.models import Post
from board.quote_refs import rebuild_quote_references_for_posts


class Command(BaseCommand):
    help = "Rebuild forum_quote_refs from Post.content_bbcode."

    def handle(self, *args, **options):
        total = rebuild_quote_references_for_posts(
            Post.objects.only("pk", "content_bbcode").order_by("pk")
        )
        self.stdout.write(self.style.SUCCESS(
            f"Gotowe. Przeindeksowano {total} postów."
        ))
