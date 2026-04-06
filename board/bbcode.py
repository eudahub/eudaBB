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
    max_tag_depth=31,
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
# [quote=Author post_id=N time=T]  — enriched quote with optional link
#
# Overrides the library default to support the enriched format produced by
# enrich_quotes: [quote=Username post_id=12345 time=1133305680]
# The option string is "Username post_id=12345 time=1133305680" — parsed here.
# Also handles the plain sfinia format [quote="Username"] (no post_id).
# ---------------------------------------------------------------------------

import re as _re2

_QUOTE_OPT_RE = _re2.compile(
    r'^(?P<author>[^ ]+)?'
    r'(?:\s+post_id=(?P<post_id>\d+|not_found))?'
    r'(?:\s+time=(?P<time>\d+))?',
    _re2.IGNORECASE,
)


def _render_quote(tag_name, value, options, parent, context):
    raw_opt = (options.get("quote") or "").strip()
    author = ""
    post_id = ""
    ts = ""

    if raw_opt:
        m = _QUOTE_OPT_RE.match(raw_opt)
        if m:
            author  = m.group("author")  or ""
            post_id = m.group("post_id") or ""
            ts      = m.group("time")    or ""

    # bbcode library splits [quote="Author" post_id=N time=T] into separate options
    if not post_id:
        post_id = str(options.get("post_id", "") or "").strip()
    if not ts:
        ts = str(options.get("time", "") or "").strip()

    not_found = (post_id.lower() == "not_found") if post_id else False

    if author and post_id and not not_found and ts:
        # Format timestamp as "YYYY-MM-DD HH:MM"
        try:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_str = ts
        cite = (
            f'<a href="/post/{post_id}/" class="quote-link">'
            f'{author} pisze: ↑{date_str}</a>'
        )
    elif author and post_id and not not_found:
        cite = (
            f'<a href="/post/{post_id}/" class="quote-link">'
            f'{author} pisze:</a>'
        )
    elif author and not_found:
        cite = f'<span class="quote-not-found">{author} pisze:</span>'
    elif author:
        cite = f'{author} pisze:'
    else:
        cite = "Cytat:"

    return (
        f'<cite class="bbquote-cite">{cite}</cite>'
        f'<blockquote class="bbquote">'
        f'{value}'
        f'</blockquote>'
    )


_parser.add_formatter("quote", _render_quote, render_embedded=True,
                      swallow_trailing_newline=True)


# ---------------------------------------------------------------------------
# [fquote]  — foreign quote (from outside the forum, e.g. internet)
# ---------------------------------------------------------------------------

def _render_fquote(tag_name, value, options, parent, context):
    label = (options.get("fquote") or "").strip() or "Źródło zewnętrzne"
    return (
        f'<blockquote class="bbquote bbquote--foreign">'
        f'<cite>{label}</cite>'
        f'{value}'
        f'</blockquote>'
    )


_parser.add_formatter("fquote", _render_fquote, render_embedded=True,
                      swallow_trailing_newline=True)


# ---------------------------------------------------------------------------
# [bible=Ref]  — scripture quote with reference
# ---------------------------------------------------------------------------

def _render_bible(tag_name, value, options, parent, context):
    ref = (
        options.get("bible")
        or options.get("Bible")
        or options.get(tag_name)
        or ""
    ).strip()
    if len(ref) >= 2 and ref[0] == '"' and ref[-1] == '"':
        ref = ref[1:-1].strip()
    label = f'<cite class="bbquote-cite bbquote-cite--bible">{ref}</cite>' if ref else ""
    return (
        f'{label}'
        f'<div class="bbquote--bible">{value}</div>'
    )


_parser.add_formatter("bible", _render_bible, render_embedded=True,
                      swallow_trailing_newline=True)
_parser.add_formatter("Bible", _render_bible, render_embedded=True,
                      swallow_trailing_newline=True)


# ---------------------------------------------------------------------------
# [ai=Label]  — AI quote/note
# ---------------------------------------------------------------------------

def _render_ai(tag_name, value, options, parent, context):
    ref = (
        options.get("ai")
        or options.get("AI")
        or options.get(tag_name)
        or ""
    ).strip()
    if len(ref) >= 2 and ref[0] == '"' and ref[-1] == '"':
        ref = ref[1:-1].strip()
    label = f'<cite class="bbquote-cite bbquote-cite--ai">{ref}</cite>' if ref else ""
    return (
        f'{label}'
        f'<div class="bbquote--ai">{value}</div>'
    )


_parser.add_formatter("ai", _render_ai, render_embedded=True,
                      swallow_trailing_newline=True)
_parser.add_formatter("AI", _render_ai, render_embedded=True,
                      swallow_trailing_newline=True)


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
