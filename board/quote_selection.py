import re
from dataclasses import dataclass, field


_TAG_RE = re.compile(r"\[/?[A-Za-z*][^\]]*\]")
_OPEN_TAG_NAME_RE = re.compile(r"^\[(?P<name>[A-Za-z*]+)")
_CLOSE_TAG_NAME_RE = re.compile(r"^\[/(?P<name>[A-Za-z*]+)\]")
_QUOTE_OPEN_RE = re.compile(
    r'\[(?P<qtype>quote|fquote)(?:=(?:"(?P<author_q>[^"]*)"|(?P<author_u>[^\]\s]+)))?(?P<attrs>[^\]]*)\]',
    re.IGNORECASE,
)
_QUOTE_CLOSE_RE = re.compile(r'\[/(?P<qtype>quote|fquote)\]', re.IGNORECASE)
_POST_ID_RE = re.compile(r'\bpost_id=(\d+)\b', re.IGNORECASE)

_SAFE_INLINE_TAGS = {"b", "i", "u", "s", "color", "size", "font", "url"}
_UNSAFE_NONQUOTE_TAGS = {
    "fquote", "bible", "ai", "spoiler", "code",
    "img", "youtube", "list", "*", "center", "hr", "sub", "sup",
}


@dataclass
class QuoteBlock:
    qtype: str
    author: str
    post_id: int | None
    open_start: int
    open_end: int
    close_start: int
    close_end: int
    open_raw: str
    close_raw: str
    parent: "QuoteBlock | None" = None
    children: list["QuoteBlock"] = field(default_factory=list)


def normalize_selected_text(text: str) -> str:
    return (
        (text or "")
        .replace("\r\n", "\n")
        .replace("\xa0", " ")
        .strip()
    )


def _tokenize_bbcode(source: str):
    tokens = []
    pos = 0
    for match in _TAG_RE.finditer(source):
        if match.start() > pos:
            tokens.append({
                "kind": "text",
                "raw": source[pos:match.start()],
                "start": pos,
                "end": match.start(),
            })
        raw = match.group(0)
        lower = raw.lower()
        close_match = _CLOSE_TAG_NAME_RE.match(lower)
        if close_match:
            tokens.append({
                "kind": "tag",
                "raw": raw,
                "start": match.start(),
                "end": match.end(),
                "tag_name": close_match.group("name"),
                "is_open": False,
            })
        else:
            open_match = _OPEN_TAG_NAME_RE.match(lower)
            tokens.append({
                "kind": "tag",
                "raw": raw,
                "start": match.start(),
                "end": match.end(),
                "tag_name": (open_match.group("name") if open_match else ""),
                "is_open": True,
            })
        pos = match.end()
    if pos < len(source):
        tokens.append({
            "kind": "text",
            "raw": source[pos:],
            "start": pos,
            "end": len(source),
        })
    return tokens


def _find_unique_text_range(source: str, selected_text: str):
    """Map plaintext selection to source offsets using text tokens only."""
    tokens = _tokenize_bbcode(source)
    text_only = "".join(t["raw"] for t in tokens if t["kind"] == "text")
    start = text_only.find(selected_text)
    if start == -1 or text_only.find(selected_text, start + 1) != -1:
        return None

    end = start + len(selected_text)
    text_pos = 0
    source_start = None
    source_end = None

    for token in tokens:
        if token["kind"] != "text":
            continue
        token_text = token["raw"]
        token_len = len(token_text)
        token_text_start = text_pos
        token_text_end = text_pos + token_len

        if source_start is None and token_text_start <= start < token_text_end:
            source_start = token["start"] + (start - token_text_start)
        if source_end is None and token_text_start < end <= token_text_end:
            source_end = token["start"] + (end - token_text_start)
        text_pos = token_text_end

    if source_start is None or source_end is None:
        return None
    return tokens, source_start, source_end


def _active_open_tags(tokens, pos: int):
    stack = []
    for token in tokens:
        if token["start"] >= pos:
            break
        if token["kind"] != "tag":
            continue
        name = token["tag_name"]
        if name not in _SAFE_INLINE_TAGS:
            continue
        if token["is_open"]:
            stack.append(token["raw"])
        else:
            for idx in range(len(stack) - 1, -1, -1):
                opened = _OPEN_TAG_NAME_RE.match(stack[idx].lower())
                if opened and opened.group("name") == name:
                    del stack[idx]
                    break
    return stack


def _close_tags_for_fragment(fragment: str):
    stack = []
    for token in _tokenize_bbcode(fragment):
        if token["kind"] != "tag":
            continue
        name = token["tag_name"]
        if name not in _SAFE_INLINE_TAGS:
            continue
        if token["is_open"]:
            stack.append(name)
        else:
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx] == name:
                    del stack[idx]
                    break
    return "".join(f"[/{name}]" for name in reversed(stack))


def _extract_safe_nonquote_range(tokens, source_start: int, source_end: int):
    if source_start >= source_end:
        return ""

    for token in tokens:
        if token["kind"] != "tag":
            continue
        if token["end"] <= source_start or token["start"] >= source_end:
            continue
        name = token["tag_name"]
        if name in _SAFE_INLINE_TAGS:
            continue
        if name in {"quote", "fquote"}:
            return None
        if name in _UNSAFE_NONQUOTE_TAGS:
            return None
        return None

    prefix_tags = "".join(_active_open_tags(tokens, source_start))
    fragment = ""
    for token in tokens:
        if token["end"] <= source_start or token["start"] >= source_end:
            continue
        if token["kind"] == "text":
            part_start = max(source_start, token["start"]) - token["start"]
            part_end = min(source_end, token["end"]) - token["start"]
            fragment += token["raw"][part_start:part_end]
            continue
        if token["tag_name"] in _SAFE_INLINE_TAGS:
            fragment += token["raw"]
            continue
        return None

    suffix_tags = _close_tags_for_fragment(prefix_tags + fragment)
    return prefix_tags + fragment + suffix_tags


def _parse_quote_blocks(source: str):
    events = []
    for match in _QUOTE_OPEN_RE.finditer(source or ""):
        events.append((match.start(), 0, "open", match))
    for match in _QUOTE_CLOSE_RE.finditer(source or ""):
        events.append((match.start(), 1, "close", match))
    events.sort(key=lambda item: (item[0], item[1]))

    roots = []
    stack = []
    for _, _, kind, match in events:
        if kind == "open":
            attrs = match.group("attrs") or ""
            post_id_match = _POST_ID_RE.search(attrs)
            parent = stack[-1] if stack else None
            block = QuoteBlock(
                qtype=(match.group("qtype") or "").lower(),
                author=(match.group("author_q") or match.group("author_u") or "").strip(),
                post_id=int(post_id_match.group(1)) if post_id_match else None,
                open_start=match.start(),
                open_end=match.end(),
                close_start=match.end(),
                close_end=match.end(),
                open_raw=match.group(0),
                close_raw="",
                parent=parent,
            )
            if parent is not None:
                parent.children.append(block)
            else:
                roots.append(block)
            stack.append(block)
            continue

        if not stack:
            continue
        block = stack.pop()
        if block.qtype != (match.group("qtype") or "").lower():
            continue
        block.close_start = match.start()
        block.close_end = match.end()
        block.close_raw = match.group(0)

    return roots


def _ellipsis_prefix(needs_prefix: bool) -> str:
    return "(...)\n" if needs_prefix else ""


def _ellipsis_suffix(needs_suffix: bool) -> str:
    return "\n(...)" if needs_suffix else ""


def _build_fragment_in_range(tokens, range_start: int, range_end: int, child_blocks):
    parts = []
    cursor = range_start

    for child in child_blocks:
        if child.close_end <= range_start or child.open_start >= range_end:
            continue

        if cursor < child.open_start:
            plain_fragment = _extract_safe_nonquote_range(
                tokens, cursor, min(range_end, child.open_start)
            )
            if plain_fragment is None:
                return None
            parts.append(plain_fragment)

        inner_start = max(range_start, child.open_end)
        inner_end = min(range_end, child.close_start)
        if inner_end <= inner_start:
            cursor = max(cursor, child.close_end)
            continue

        if child.qtype != "quote" or child.post_id is None or not child.author:
            return None

        inner_fragment = _build_fragment_in_range(tokens, inner_start, inner_end, child.children)
        if inner_fragment is None:
            return None

        rebuilt_body = (
            _ellipsis_prefix(inner_start > child.open_end)
            + inner_fragment
            + _ellipsis_suffix(inner_end < child.close_start)
        )
        parts.append(f'{child.open_raw}{rebuilt_body}{child.close_raw}')
        cursor = max(cursor, child.close_end)

    if cursor < range_end:
        plain_fragment = _extract_safe_nonquote_range(tokens, cursor, range_end)
        if plain_fragment is None:
            return None
        parts.append(plain_fragment)

    return "".join(parts)


def extract_exact_quote_fragment(source: str, selected_text: str):
    """Return source BBCode fragment for a plaintext selection when safe."""
    selected_text = normalize_selected_text(selected_text)
    if not source or not selected_text:
        return None

    found = _find_unique_text_range(source, selected_text)
    if not found:
        return None

    tokens, source_start, source_end = found
    quote_blocks = _parse_quote_blocks(source)
    return _build_fragment_in_range(tokens, source_start, source_end, quote_blocks)
