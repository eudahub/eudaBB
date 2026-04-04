from django import template
from django.utils.safestring import mark_safe
from board.bbcode import render

register = template.Library()


@register.filter
def pagination_range(page_obj):
    """Return list of page numbers to display, with None meaning ellipsis.

    Always shows first 3, last 3, and cur-1..cur+1.
    Merges adjacent windows, inserts None for gaps.
    """
    cur = page_obj.number
    total = page_obj.paginator.num_pages

    # Build the set of pages to show
    visible = set()
    visible.update(range(1, min(4, total + 1)))            # first 3
    visible.update(range(max(1, total - 2), total + 1))    # last 3
    visible.update(range(max(1, cur - 1), min(total, cur + 1) + 1))  # window

    pages = sorted(visible)

    # Insert None where there are gaps
    result = []
    prev = None
    for p in pages:
        if prev is not None and p > prev + 1:
            result.append(None)  # ellipsis
        result.append(p)
        prev = p
    return result

@register.filter
def bbcode(value):
    return mark_safe(render(value or ""))
