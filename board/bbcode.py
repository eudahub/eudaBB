"""
BBCode renderer — thin wrapper around the `bbcode` library.
Centralised here so swapping the parser later touches only this file.
"""
import bbcode

_parser = bbcode.Parser(
    newline="<br>",
    install_defaults=True,
    escape_html=True,
)


def render(text: str) -> str:
    """Render BBCode markup to safe HTML."""
    return _parser.format(text or "")
