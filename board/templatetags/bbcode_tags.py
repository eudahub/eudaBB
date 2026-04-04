from django import template
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
