import re
import unicodedata
from dataclasses import dataclass

from .models import Post


_QUOTE_OPEN_RE = re.compile(
    r'\[(?P<qtype>quote|fquote)(?:=(?:"(?P<author_q>[^"]*)"|(?P<author_u>[^\]\s]+)))?(?P<attrs>[^\]]*)\]',
    re.IGNORECASE,
)
_QUOTE_CLOSE_RE = re.compile(r'\[/(?P<qtype>quote|fquote)\]', re.IGNORECASE)
_POST_ID_RE = re.compile(r'\bpost_id=(\d+)\b', re.IGNORECASE)
_BBCODE_TAG_RE = re.compile(r'\[/?\w+(?:[^\]]*?)\]', re.IGNORECASE)
_ELLIPSIS_RE = re.compile(r'(?:/\.\.\./|\(\.\.\.\)|\.\.\.|…)')


@dataclass
class ParsedQuote:
    qtype: str
    author: str
    post_id: int | None
    inner_text: str
    depth: int
    quote_index: int


def parse_quotes(content: str) -> list[ParsedQuote]:
    events = []
    for match in _QUOTE_OPEN_RE.finditer(content or ""):
        events.append((match.start(), 0, "open", match))
    for match in _QUOTE_CLOSE_RE.finditer(content or ""):
        events.append((match.start(), 1, "close", match))
    events.sort(key=lambda item: (item[0], item[1]))

    stack = []
    result = []
    quote_index = 0
    for _, _, kind, match in events:
        if kind == "open":
            attrs = match.group("attrs") or ""
            post_id_match = _POST_ID_RE.search(attrs)
            stack.append({
                "qtype": match.group("qtype").lower(),
                "author": (match.group("author_q") or match.group("author_u") or "").strip(),
                "post_id": int(post_id_match.group(1)) if post_id_match else None,
                "inner_start": match.end(),
                "depth": len(stack) + 1,
                "quote_index": quote_index,
            })
            quote_index += 1
            continue

        if not stack:
            continue
        opened = stack.pop()
        if opened["qtype"] != match.group("qtype").lower():
            continue
        result.append(ParsedQuote(
            qtype=opened["qtype"],
            author=opened["author"],
            post_id=opened["post_id"],
            inner_text=(content or "")[opened["inner_start"]:match.start()],
            depth=opened["depth"],
            quote_index=opened["quote_index"],
        ))

    return sorted(result, key=lambda q: q.quote_index)


def count_ellipsis(text: str) -> int:
    return len(_ELLIPSIS_RE.findall(text or ""))


def _strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_quote_text(text: str) -> str:
    text = _BBCODE_TAG_RE.sub("", text or "")
    text = _strip_diacritics(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def match_quote_text(quote_text_norm: str, source_text_norm: str) -> bool:
    if not quote_text_norm or not source_text_norm:
        return False

    fragments = [part.strip() for part in _ELLIPSIS_RE.split(quote_text_norm) if part.strip()]
    if not fragments:
        return False

    if len(fragments) == 1:
        frag = fragments[0]
        return len(frag) >= 8 and frag in source_text_norm

    search_from = 0
    matched = 0
    for frag in fragments:
        if len(frag) < 5:
            continue
        idx = source_text_norm.find(frag, search_from)
        if idx == -1:
            return False
        search_from = idx + len(frag)
        matched += 1
    return matched > 0


def validate_enriched_quotes(content: str) -> list[str]:
    quotes = [q for q in parse_quotes(content) if q.qtype == "quote"]
    if not quotes:
        return []

    errors = []
    post_ids = sorted({q.post_id for q in quotes if q.post_id is not None})
    source_posts = {
        post.pk: post
        for post in Post.objects.filter(pk__in=post_ids).select_related("author")
    }

    for quote in quotes:
        if quote.post_id is None:
            errors.append("Cytat [quote] musi zawierać post_id.")
            continue

        source_post = source_posts.get(quote.post_id)
        if source_post is None:
            errors.append(f"Cytat [quote] wskazuje na nieistniejący post_id={quote.post_id}.")
            continue

        expected_author = source_post.author.username if source_post.author else ""
        if not expected_author:
            errors.append(f"Cytat z post_id={quote.post_id} nie ma autora do weryfikacji.")
            continue

        if quote.author != expected_author:
            errors.append(
                f'Cytat z post_id={quote.post_id} musi mieć autora "{expected_author}", '
                f'a ma "{quote.author or "(brak)"}".'
            )
            continue

        quote_norm = normalize_quote_text(quote.inner_text)
        source_norm = normalize_quote_text(source_post.content_bbcode or "")
        if not match_quote_text(quote_norm, source_norm):
            errors.append(
                f"Cytowany fragment dla post_id={quote.post_id} nie pasuje do treści źródłowej."
            )

    return errors
