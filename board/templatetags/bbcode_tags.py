from django import template
from django.utils.safestring import mark_safe
from board.bbcode import render

register = template.Library()

@register.filter
def bbcode(value):
    return mark_safe(render(value or ""))
