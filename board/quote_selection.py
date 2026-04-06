import re


_TAG_RE = re.compile(r"\[/?[A-Za-z*][^\]]*\]")
_OPEN_TAG_NAME_RE = re.compile(r"^\[(?P<name>[A-Za-z*]+)")
_CLOSE_TAG_NAME_RE = re.compile(r"^\[/(?P<name>[A-Za-z*]+)\]")

_SAFE_INLINE_TAGS = {"b", "i", "u", "s", "color", "size", "font", "url"}
_UNSAFE_TAGS = {
    "quote", "fquote", "bible", "ai", "spoiler", "code",
    "img", "youtube", "list", "*", "center", "hr", "sub", "sup",
}


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


def _is_safe_selection(tokens, source_start: int, source_end: int) -> bool:
    for token in tokens:
        if token["kind"] != "tag":
            continue
        if token["end"] <= source_start or token["start"] >= source_end:
            continue
        if token["tag_name"] in _UNSAFE_TAGS:
            return False
    return True


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
                if _OPEN_TAG_NAME_RE.match(stack[idx].lower()).group("name") == name:
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


def extract_exact_quote_fragment(source: str, selected_text: str):
    """Return source BBCode fragment for a plaintext selection when safe."""
    selected_text = normalize_selected_text(selected_text)
    if not source or not selected_text:
        return None

    found = _find_unique_text_range(source, selected_text)
    if not found:
        return None

    tokens, source_start, source_end = found
    if not _is_safe_selection(tokens, source_start, source_end):
        return None

    prefix_tags = "".join(_active_open_tags(tokens, source_start))
    fragment = source[source_start:source_end]
    suffix_tags = _close_tags_for_fragment(prefix_tags + fragment)
    return prefix_tags + fragment + suffix_tags
