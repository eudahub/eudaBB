import re
import unicodedata

from django.db.models import Q

from .models import Post, PostSearchIndex


_TAG_RE = re.compile(r"\[/?[A-Za-z*][A-Za-z0-9]{0,15}(?:=[^\]\n]*)?\]")
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


def _strip_block_tags(content_bbcode: str) -> str:
    """
    Zwraca tekst autora — usuwa bloki [quote]/[fquote]/[bible]/[ai]/[code]
    (z zagnieżdżeniami). Zachowuje oryginalne białe znaki (w tym newliny).
    """
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

    return "".join(parts)


def extract_author_search_text(content_bbcode: str) -> str:
    text = _strip_block_tags(content_bbcode)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_content_user(content_bbcode: str) -> str:
    """
    Jak extract_author_search_text, ale zachowuje strukturę akapitów:
    wiele pustych linii → jedna pusta linia, pozostałe białe znaki normalizuje
    w obrębie linii.
    """
    text = _strip_block_tags(content_bbcode)
    # Normalizuj spacje/taby w obrębie każdej linii, zachowaj newliny
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    # Złącz linie z powrotem, spakuj serie pustych linii do jednej pustej
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip()


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
    Jeśli wpisana forma jest mianownikową formą rodziny (nom_form), szuka tylko
    tych rodzin — np. "anonim" → tylko sg, "anonimy" → tylko pl.
    Jeśli słowa nie ma w słowniku, próbuje analogii sufiksowej (fallback).
    """
    from .models import MorphForm

    try:
        families = list(
            MorphForm.objects
            .filter(form_norm=term_norm)
            .values("lemma_norm", "family_id", "nom_form")
            .distinct()
        )
    except Exception:
        return [term_norm]

    if not families:
        # Słowo nie w słowniku — spróbuj analogii sufiksowej
        analog = _expand_by_suffix_analogy(term_norm)
        return analog if analog else [term_norm]

    # Jeśli wpisana forma jest mianownikową (nom_form) jakiejś rodziny,
    # ogranicz do tych rodzin — np. "anonim" → sg, "anonimy" → pl.
    # Fallback na lemma_norm dla danych zaimportowanych bez nom_form.
    canonical = [e for e in families if e["nom_form"] == term_norm]
    if not canonical:
        canonical = [e for e in families if e["lemma_norm"] == term_norm]
    working = canonical if canonical else families

    q = Q()
    for entry in working:
        q |= Q(lemma_norm=entry["lemma_norm"], family_id=entry["family_id"])

    forms = sorted(set(
        MorphForm.objects.filter(q).values_list("form_norm", flat=True)
    ))
    return forms if forms else [term_norm]


def expand_morph_term_all(term_norm: str) -> list[str]:
    """
    Operator ++: wszystkie rodziny lematu (sg+pl, wszystkie rodzaje, wszystkie stopnie).
    Jeśli słowa nie ma w słowniku, używa analogii sufiksowej jak expand_morph_term.
    """
    from .models import MorphForm

    try:
        families = list(
            MorphForm.objects
            .filter(form_norm=term_norm)
            .values("lemma_norm", "family_id", "nom_form")
            .distinct()
        )
    except Exception:
        return [term_norm]

    if not families:
        analog = _expand_by_suffix_analogy(term_norm)
        return analog if analog else [term_norm]

    # Kanoniczny lemat: preferuj nom_form == term, fallback na lemma_norm == term
    canonical = [e for e in families if e["nom_form"] == term_norm]
    if not canonical:
        canonical = [e for e in families if e["lemma_norm"] == term_norm]
    working_lemmas = {e["lemma_norm"] for e in (canonical if canonical else families)}

    # Wszystkie formy ze wszystkich rodzin danego lematu
    q = Q()
    for lemma in working_lemmas:
        q |= Q(lemma_norm=lemma)

    forms = sorted(set(
        MorphForm.objects.filter(q).values_list("form_norm", flat=True)
    ))
    return forms if forms else [term_norm]


def _expand_by_suffix_analogy(term_norm: str) -> list[str]:
    """
    Fallback dla słów spoza MorphForm.
    Szuka rodzin (subst:sg, adj:sg:n, adj:pl:nonvir) o tym samym sufiksie formy nominalnej
    i stosuje ich wzorzec odmiany do term_norm.
    Zwraca pustą listę gdy brak analogów (wywołujący powinien wtedy zwrócić [term_norm]).
    """
    from collections import defaultdict
    from .models import MorphForm, MorphSuffix

    MAX_ANALOGS = 20   # max rodzin analogicznych na sufiks
    SUFFIX_LENS = (4, 3, 2)

    results: set[str] = set()

    for slen in SUFFIX_LENS:
        if len(term_norm) <= slen:
            continue
        suffix = term_norm[-slen:]
        unknown_stem = term_norm[:-slen]

        try:
            analogs = list(
                MorphSuffix.objects
                .filter(suffix_len=slen, suffix=suffix)
                .values("lemma_norm", "family_id")[:MAX_ANALOGS]
            )
        except Exception:
            continue

        if not analogs:
            continue

        # Pobierz wszystkie formy analogicznych rodzin jednym zapytaniem
        q = Q()
        for entry in analogs:
            q |= Q(lemma_norm=entry["lemma_norm"], family_id=entry["family_id"])

        try:
            all_rows = list(
                MorphForm.objects.filter(q)
                .values_list("lemma_norm", "family_id", "form_norm")
            )
        except Exception:
            continue

        # Grupuj formy po rodzinie, wyklucz formy z myślnikiem
        by_family: dict[tuple, list[str]] = defaultdict(list)
        for lemma_norm, fid, form_norm in all_rows:
            if "-" not in form_norm:
                by_family[(lemma_norm, fid)].append(form_norm)

        for forms in by_family.values():
            # Znajdź kotwicę: forma kończąca się na szukany sufiks (mianownik)
            anchors = [f for f in forms if f.endswith(suffix)]
            if not anchors:
                continue
            anchor = min(anchors, key=len)   # najkrótsza = mianownikowa
            anchor_stem = anchor[:-slen]
            for form in forms:
                if form.startswith(anchor_stem):
                    results.add(unknown_stem + form[len(anchor_stem):])

        if results:
            break

    return sorted(results)
