from .models import Post, QuoteReference
from .quote_validation import count_ellipsis, parse_quotes


def parse_quote_references(content: str):
    """Parse quote/fquote tags with nesting depth from BBCode content."""
    refs = []
    for quote in parse_quotes(content or ""):
        refs.append({
            "quote_type": quote.qtype,
            "quoted_username": quote.author,
            "source_post_id": quote.post_id,
            "depth": quote.depth,
            "ellipsis_count": count_ellipsis(quote.inner_text),
            "quote_index": quote.quote_index,
        })
    return refs


def rebuild_quote_references_for_posts(posts, chunk_size: int = 500) -> int:
    """Rebuild QuoteReference rows for an iterable/queryset of posts."""
    processed = 0
    delete_ids = []
    insert_specs = []

    def flush():
        nonlocal delete_ids, insert_specs
        if delete_ids:
            QuoteReference.objects.filter(post_id__in=delete_ids).delete()
        if insert_specs:
            source_ids = {
                spec["source_post_id"]
                for spec in insert_specs
                if spec["source_post_id"] is not None
            }
            existing_source_ids = set()
            if source_ids:
                existing_source_ids = set(
                    Post.objects.filter(pk__in=source_ids).values_list("pk", flat=True)
                )

            inserts = []
            for spec in insert_specs:
                source_post_id = spec["source_post_id"]
                if source_post_id not in existing_source_ids:
                    source_post_id = None
                inserts.append(QuoteReference(
                    post_id=spec["post_id"],
                    source_post_id=source_post_id,
                    quote_type=spec["quote_type"],
                    quoted_username=spec["quoted_username"],
                    depth=spec["depth"],
                    ellipsis_count=spec["ellipsis_count"],
                    quote_index=spec["quote_index"],
                ))
            QuoteReference.objects.bulk_create(inserts, batch_size=1000)
        delete_ids = []
        insert_specs = []

    iterator = posts.iterator(chunk_size=chunk_size) if hasattr(posts, "iterator") else iter(posts)
    for post in iterator:
        delete_ids.append(post.pk)
        for ref in parse_quote_references(post.content_bbcode or ""):
            insert_specs.append({
                "post_id": post.pk,
                "source_post_id": ref["source_post_id"],
                "quote_type": ref["quote_type"],
                "quoted_username": ref["quoted_username"],
                "depth": ref["depth"],
                "ellipsis_count": ref["ellipsis_count"],
                "quote_index": ref["quote_index"],
            })
        processed += 1
        if len(delete_ids) >= chunk_size:
            flush()

    flush()
    return processed


def rebuild_quote_references_for_post(post: Post) -> None:
    rebuild_quote_references_for_posts([post], chunk_size=1)
