import re
import unicodedata

from .models import Post, PostSearchIndex


_TAG_RE = re.compile(r"\[/?[A-Za-z*][^\]]*\]")
_OPEN_TAG_NAME_RE = re.compile(r"^\[(?P<name>[A-Za-z*]+)")
_CLOSE_TAG_NAME_RE = re.compile(r"^\[/(?P<name>[A-Za-z*]+)\]")
_SKIP_BLOCK_TAGS = {"quote", "fquote", "bible", "ai", "code"}


def strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_search_text(text: str) -> str:
    text = strip_diacritics((text or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_author_search_text(content_bbcode: str) -> str:
    content = content_bbcode or ""
    parts = []
    stack = []
    pos = 0

    for match in _TAG_RE.finditer(content):
        if match.start() > pos and not stack:
            parts.append(content[pos:match.start()])

        raw = match.group(0)
        lower = raw.lower()
        close_match = _CLOSE_TAG_NAME_RE.match(lower)
        if close_match:
            name = close_match.group("name")
            if name in _SKIP_BLOCK_TAGS:
                for idx in range(len(stack) - 1, -1, -1):
                    if stack[idx] == name:
                        del stack[idx]
                        break
        else:
            open_match = _OPEN_TAG_NAME_RE.match(lower)
            name = open_match.group("name") if open_match else ""
            if name in _SKIP_BLOCK_TAGS:
                stack.append(name)

        pos = match.end()

    if pos < len(content) and not stack:
        parts.append(content[pos:])

    text = "".join(parts)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_post_search_payload(post: Post) -> dict:
    author_only = extract_author_search_text(post.content_bbcode or "")
    return {
        "topic": post.topic,
        "forum": post.topic.forum,
        "author": post.author,
        "created_at": post.created_at,
        "content_search_author": author_only,
        "content_search_author_normalized": normalize_search_text(author_only),
    }


def update_post_search_index(post: Post) -> None:
    payload = build_post_search_payload(post)
    PostSearchIndex.objects.update_or_create(post=post, defaults=payload)


def rebuild_post_search_index_for_posts(posts, chunk_size: int = 500) -> int:
    total = 0
    batch = []
    post_ids = []

    def flush():
        nonlocal batch, post_ids, total
        if not batch:
            return
        PostSearchIndex.objects.filter(post_id__in=post_ids).delete()
        PostSearchIndex.objects.bulk_create(batch, batch_size=1000)
        total += len(batch)
        batch = []
        post_ids = []

    for post in posts.iterator(chunk_size=chunk_size) if hasattr(posts, "iterator") else posts:
        payload = build_post_search_payload(post)
        batch.append(PostSearchIndex(post=post, **payload))
        post_ids.append(post.pk)
        if len(batch) >= chunk_size:
            flush()

    flush()
    return total
