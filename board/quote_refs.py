import re

from .models import Post, QuoteReference


_OPEN_TAG_RE = re.compile(
    r'\[(?P<qtype>quote|fquote|Bible)(?:=(?:"(?P<author_q>[^"]*)"|(?P<author_u>[^\]\s]+)))?(?P<attrs>[^\]]*)\]',
    re.IGNORECASE,
)
_CLOSE_TAG_RE = re.compile(r'\[/(?P<qtype>quote|fquote|Bible)\]', re.IGNORECASE)
_POST_ID_RE = re.compile(r'\bpost_id=(\d+)\b', re.IGNORECASE)


def parse_quote_references(content: str):
    """Parse quote/fquote tags with nesting depth from BBCode content."""
    if not content:
        return []

    events = []
    for m in _OPEN_TAG_RE.finditer(content):
        events.append((m.start(), 0, "open", m))
    for m in _CLOSE_TAG_RE.finditer(content):
        events.append((m.start(), 1, "close", m))
    events.sort(key=lambda item: (item[0], item[1]))

    refs = []
    stack = []
    quote_index = 0

    for _, _, kind, match in events:
        if kind == "open":
            qtype = match.group("qtype").lower()
            depth = len(stack) + 1
            stack.append(qtype)

            if qtype not in {"quote", "fquote"}:
                continue

            attrs = match.group("attrs") or ""
            author = (match.group("author_q") or match.group("author_u") or "").strip()
            post_id_match = _POST_ID_RE.search(attrs)
            refs.append({
                "quote_type": qtype,
                "quoted_username": author,
                "source_post_id": int(post_id_match.group(1)) if post_id_match else None,
                "depth": depth,
                "quote_index": quote_index,
            })
            quote_index += 1
            continue

        if stack:
            stack.pop()

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
                "quote_index": ref["quote_index"],
            })
        processed += 1
        if len(delete_ids) >= chunk_size:
            flush()

    flush()
    return processed


def rebuild_quote_references_for_post(post: Post) -> None:
    rebuild_quote_references_for_posts([post], chunk_size=1)
