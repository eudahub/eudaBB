from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe
from board.bbcode import render

register = template.Library()


@register.filter
def pagination_range(page_obj):
    """Return list for pagination display.

    Each item is either an int (page number) or the string '…' (ellipsis).
    Commas are appended to int items as needed (stored as negative ints won't
    work — instead we return dicts with 'n' and 'comma' keys so the template
    can render cleanly without look-ahead).

    Format: list of dicts:
      {'n': int, 'comma': bool}   — page number, comma after?
      {'ellipsis': True}          — gap marker
    """
    cur = page_obj.number
    total = page_obj.paginator.num_pages

    visible = set()
    visible.update(range(1, min(4, total + 1)))
    visible.update(range(max(1, total - 2), total + 1))
    visible.update(range(max(1, cur - 1), min(total, cur + 1) + 1))

    pages = sorted(visible)

    # Build raw list with None gaps
    raw = []
    prev = None
    for p in pages:
        if prev is not None and p > prev + 1:
            raw.append(None)
        raw.append(p)
        prev = p

    # Convert to dicts; comma after a number only if next item is also a number
    result = []
    for i, item in enumerate(raw):
        if item is None:
            result.append({'ellipsis': True})
        else:
            next_item = raw[i + 1] if i + 1 < len(raw) else None
            result.append({'n': item, 'comma': isinstance(next_item, int)})
    return result

@register.filter
def bbcode(value):
    return mark_safe(render(value or ""))


def _render_bbcode_or_raw(text, broken_tags):
    if broken_tags:
        return '<pre class="broken-bbcode">' + escape(text) + '</pre>'
    return render(text or "")


@register.filter
def post_content(post):
    """Render post content, inserting merge markers between appended parts."""
    merge_log = post.merge_log or []
    if not merge_log:
        return mark_safe(_render_bbcode_or_raw(post.content_bbcode, post.broken_tags))

    starts = [0] + [e["offset"] for e in merge_log]
    ends   = [e["offset"] - 2 for e in merge_log] + [len(post.content_bbcode)]
    parts  = [post.content_bbcode[s:e] for s, e in zip(starts, ends)]

    html_chunks = []
    for i, part_text in enumerate(parts):
        if i > 0:
            entry = merge_log[i - 1]
            minutes = entry.get("minutes_after", "?")
            uname   = escape(entry.get("username", "?"))
            html_chunks.append(
                f'<p class="merge-marker" style="font-size:11px;color:#888;'
                f'font-style:italic;margin:.6rem 0 .3rem 0;">'
                f'— po {minutes} min. {uname} dopisał:</p>'
            )
        html_chunks.append(_render_bbcode_or_raw(part_text, post.broken_tags))

    return mark_safe("".join(html_chunks))


@register.filter
def pm_bbcode(pm):
    """Decompress PM content and render BBCode."""
    from board.pm_utils import decompress
    try:
        text = decompress(pm.content_compressed)
    except Exception:
        return mark_safe("<em>[błąd odczytu wiadomości]</em>")
    return mark_safe(render(text))
