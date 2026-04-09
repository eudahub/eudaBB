import re
import unicodedata

from django.db.models import Q

from .models import Post, PostSearchIndex


_TAG_RE = re.compile(r"\[/?[A-Za-z*][^\]]*\]")
_OPEN_TAG_NAME_RE = re.compile(r"^\[(?P<name>[A-Za-z*]+)")
_CLOSE_TAG_NAME_RE = re.compile(r"^\[/(?P<name>[A-Za-z*]+)\]")
_SKIP_BLOCK_TAGS = {"quote", "fquote", "bible", "ai", "code"}
_URL_TAG_RE = re.compile(r"\[url(?:=[^\]]*)?\]", re.IGNORECASE)
_YOUTUBE_TAG_RE = re.compile(r"\[(?:youtube|yt)(?:=[^\]]*)?\]", re.IGNORECASE)


def strip_diacritics(text: str) -> str:
    text = (text or "").replace("ł", "l").replace("Ł", "L")
    nfkd = unicodedata.normalize("NFKD", text)
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


def detect_search_features(content_bbcode: str) -> tuple[bool, bool]:
    content = content_bbcode or ""
    return (
        bool(_URL_TAG_RE.search(content)),
        bool(_YOUTUBE_TAG_RE.search(content)),
    )


def build_post_search_payload(post: Post) -> dict:
    has_link, has_youtube = detect_search_features(post.content_bbcode or "")
    author_only = extract_author_search_text(post.content_bbcode or "")
    return {
        "topic": post.topic,
        "forum": post.topic.forum,
        "author": post.author,
        "created_at": post.created_at,
        "has_link": has_link,
        "has_youtube": has_youtube,
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


def expand_morph_term(term_norm: str) -> list[str]:
    """
    Zwraca wszystkie znormalizowane formy z tej samej rodziny morfologicznej.
    Jeśli słowa nie ma w słowniku (tabela pusta lub brak wpisu), zwraca [term_norm].
    """
    from .models import MorphForm

    try:
        families = list(
            MorphForm.objects
            .filter(form_norm=term_norm)
            .values("lemma_norm", "family_id")
            .distinct()
        )
    except Exception:
        return [term_norm]

    if not families:
        return [term_norm]

    q = Q()
    for entry in families:
        q |= Q(lemma_norm=entry["lemma_norm"], family_id=entry["family_id"])

    forms = sorted(set(
        MorphForm.objects.filter(q).values_list("form_norm", flat=True)
    ))
    return forms if forms else [term_norm]
