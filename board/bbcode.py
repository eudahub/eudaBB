"""
BBCode renderer — thin wrapper around the `bbcode` library.
Centralised here so swapping the parser later touches only this file.

Built-in tags (from library):
    [b], [i], [u], [s], [center], [hr], [sub], [sup]
    [color=X], [code], [quote], [quote=user], [list], [*], [url], [url=X]

Custom tags added here:
    [img]URL[/img]           — image
    [size=N]...[/size]       — font size in px (clamped 8–72)
    [font=N]...[/font]       — alias for [size]
    [spoiler]...[/spoiler]   — collapsible block (HTML5 <details>)
    [spoiler=Label]...       — collapsible block with custom label
    [youtube=URL]            — embedded YouTube video (auto-inserted by bbcode_lint)
                               [url=youtube-url] stays as a plain link (user's choice)

Backslash escapes (processed before BBCode parsing):
    \\  →  literal backslash
    backslash-[ →  literal [
    backslash-] →  literal ]
"""
import bbcode

_parser = bbcode.Parser(
    newline="<br>",
    install_defaults=True,
    escape_html=True,
)


# ---------------------------------------------------------------------------
# [img]
# ---------------------------------------------------------------------------

_RE_STRIP_TAGS = __import__("re").compile(r"<[^>]+>")

def _render_img(tag_name, value, options, parent, context):
    # strip any auto-linked HTML the parser may have injected into the URL
    url = _RE_STRIP_TAGS.sub("", value or "").strip()
    if not url:
        return ""
    return f'<img src="{url}" alt="" style="max-width:100%;">'

_parser.add_formatter("img", _render_img, strip=True,
                      render_embedded=False, swallow_trailing_newline=True)


# ---------------------------------------------------------------------------
# [size=N] and [font=N]  (N in px, clamped to 8–72)
# ---------------------------------------------------------------------------

def _render_size(tag_name, value, options, parent, context):
    raw = options.get(tag_name, "").strip()
    if not raw.isdigit():
        return value
    px = max(8, min(72, int(raw)))
    return f'<span style="font-size:{px}px">{value}</span>'

_parser.add_formatter("size", _render_size)
_parser.add_formatter("font", _render_size)


# ---------------------------------------------------------------------------
# [spoiler] / [spoiler=Label]
# ---------------------------------------------------------------------------

def _render_spoiler(tag_name, value, options, parent, context):
    label = options.get("spoiler", "").strip() or "Spoiler"
    return f"<details><summary>{label}</summary>{value}</details>"

_parser.add_formatter("spoiler", _render_spoiler, swallow_trailing_newline=True)


# ---------------------------------------------------------------------------
# [youtube=URL]  — embedded video
# [url=youtube-url] is intentionally NOT converted (user chose plain link)
# ---------------------------------------------------------------------------

import re as _re

_YT_ID_RE = _re.compile(
    r'(?:youtube\.com/(?:watch\?(?:[^&\s]+&)*v=|shorts/|embed/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})',
    _re.IGNORECASE,
)


def _extract_yt_id(url: str) -> str:
    """Return YouTube video ID or empty string."""
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else ""


def _render_youtube(tag_name, value, options, parent, context):
    url = (options.get("youtube") or value or "").strip()
    url = _RE_STRIP_TAGS.sub("", url)
    vid = _extract_yt_id(url)
    if not vid:
        return f'[youtube={url}]'   # fallback: show as-is
    return (
        f'<div style="position:relative;padding-bottom:56.25%;height:0;overflow:hidden;">'
        f'<iframe style="position:absolute;top:0;left:0;width:100%;height:100%;" '
        f'src="https://www.youtube-nocookie.com/embed/{vid}" '
        f'frameborder="0" allowfullscreen loading="lazy"></iframe></div>'
    )


_parser.add_formatter("youtube", _render_youtube,
                      render_embedded=False, swallow_trailing_newline=True)


# ---------------------------------------------------------------------------
# Backslash escape preprocessing
# ---------------------------------------------------------------------------

_ESC_BACKSLASH = "\U000F0001"
_ESC_LBRACKET  = "\U000F0002"
_ESC_RBRACKET  = "\U000F0003"


def _apply_escapes(text: str) -> str:
    result = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "\\":
                result.append(_ESC_BACKSLASH); i += 2; continue
            elif nxt == "[":
                result.append(_ESC_LBRACKET);  i += 2; continue
            elif nxt == "]":
                result.append(_ESC_RBRACKET);  i += 2; continue
        result.append(text[i])
        i += 1
    return "".join(result)


def _restore_escapes(html: str) -> str:
    return (
        html
        .replace(_ESC_BACKSLASH, "\\")
        .replace(_ESC_LBRACKET,  "&#91;")
        .replace(_ESC_RBRACKET,  "&#93;")
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render(text: str) -> str:
    """Render BBCode markup to safe HTML."""
    text = _apply_escapes(text or "")
    html = _parser.format(text)
    return _restore_escapes(html)
