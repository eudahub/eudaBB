"""
BBCode auto-repair and validation.

Two-pass pipeline:
  1. repair(text)    — fixes what can be fixed automatically, returns (text, changes)
  2. validate(text)  — checks what remains, returns list of human-readable errors

Repair rules:
  - Bare YouTube URL (not inside any tag) → [youtube=URL]
  - Bare http(s) URL (not inside any tag) → [url=URL]URL[/url]
  - Bare www.* URL → same
  - Unclosed simple inline tags (b i u s sub sup) → closed in reverse at end of text

Validation errors (unfixable, must be corrected by user):
  - Unknown tag name
  - Unclosed block tag (code quote spoiler list)
  - Mismatched nesting  e.g. [b][i]...[/b][/i]
  - [size=X] where X is not a number
  - [color=] empty value
  - [url=] empty value
"""

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Tag classification
# ---------------------------------------------------------------------------

# Tags that must be balanced (have a [/tag] counterpart)
PAIRED_TAGS = {
    'b', 'i', 'u', 's', 'sub', 'sup', 'center',
    'color', 'size', 'font',
    'code', 'quote', 'fquote', 'spoiler',
    'url', 'img', 'youtube',
    'list',
}

# Standalone tags (no closing tag)
STANDALONE_TAGS = {'hr', '*'}

ALL_KNOWN_TAGS = PAIRED_TAGS | STANDALONE_TAGS

# Tags that auto-repair closes silently (inline, low-stakes)
AUTO_CLOSE_TAGS = {'b', 'i', 'u', 's', 'sub', 'sup', 'center'}

# Tags where inner content is literal (don't process URLs inside)
LITERAL_TAGS = {'code'}

# Tags where we don't wrap URLs (already semantic)
NO_URL_WRAP_TAGS = {'url', 'img', 'youtube', 'code', 'fquote'}


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r'\[(/?)(\w+)(?:=([^\]\n]*))?\]', re.IGNORECASE)

@dataclass
class Token:
    type: str        # 'text' | 'tag'
    text: str        # original source text of this token
    closing: bool = False
    name: str = ''
    option: str = ''  # value after = in [tag=value]

    def __str__(self):
        if self.type == 'text':
            return self.text
        slash = '/' if self.closing else ''
        opt = f'={self.option}' if self.option else ''
        return f'[{slash}{self.name}{opt}]'


def _tokenize(text: str) -> list[Token]:
    tokens = []
    pos = 0
    for m in _TAG_RE.finditer(text):
        if m.start() > pos:
            tokens.append(Token('text', text[pos:m.start()]))
        tokens.append(Token(
            type='tag',
            text=m.group(0),
            closing=bool(m.group(1)),
            name=m.group(2).lower(),
            option=m.group(3) or '',
        ))
        pos = m.end()
    if pos < len(text):
        tokens.append(Token('text', text[pos:]))
    return tokens


# ---------------------------------------------------------------------------
# URL patterns
# ---------------------------------------------------------------------------

_YT_ID_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:[^&\s\[\]]+&)*v=|shorts/|embed/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})',
    re.IGNORECASE,
)

_URL_RE = re.compile(
    r'(?<![="\'])'                  # not already in attribute
    r'((?:https?://|www\.)'         # http(s):// or www.
    r'[^\s\[\]<>"\']{3,})',         # rest of URL
    re.IGNORECASE,
)


def _is_youtube(url: str) -> bool:
    return bool(_YT_ID_RE.search(url))


def _wrap_urls_in_text(text: str) -> tuple[str, list[str]]:
    """Replace bare URLs in a plain-text segment with BBCode tags."""
    changes = []
    result = []
    last = 0
    for m in _URL_RE.finditer(text):
        url = m.group(1)
        # Normalise: add https:// to www. links
        full_url = url if url.lower().startswith('http') else f'https://{url}'
        result.append(text[last:m.start()])
        if _is_youtube(full_url):
            result.append(f'[youtube={full_url}][/youtube]')
            changes.append(f'Bare link YouTube → [youtube=...][/youtube]')
        else:
            result.append(f'[url={full_url}]{url}[/url]')
            changes.append(f'Bare URL → [url=...]: {url[:60]}')
        last = m.end()
    result.append(text[last:])
    return ''.join(result), changes


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def repair(text: str) -> tuple[str, list[str]]:
    """Auto-repair BBCode. Returns (repaired_text, list_of_human_readable_changes)."""
    tokens = _tokenize(text)
    all_changes: list[str] = []

    # Pass 1: wrap bare URLs (skip content inside literal/url/img/youtube tags)
    context: list[str] = []  # stack of open tag names
    new_tokens: list[Token] = []

    for tok in tokens:
        if tok.type == 'tag':
            if not tok.closing:
                context.append(tok.name)
            elif context and context[-1] == tok.name:
                context.pop()
            new_tokens.append(tok)
        else:
            # text token
            in_no_wrap = any(t in NO_URL_WRAP_TAGS for t in context)
            if in_no_wrap:
                new_tokens.append(tok)
            else:
                wrapped, changes = _wrap_urls_in_text(tok.text)
                all_changes.extend(changes)
                if wrapped != tok.text:
                    # Re-tokenize the wrapped result (may now contain tags)
                    for sub in _tokenize(wrapped):
                        new_tokens.append(sub)
                else:
                    new_tokens.append(tok)

    # Pass 2: close unclosed auto-close inline tags
    open_stack: list[Token] = []
    for tok in new_tokens:
        if tok.type == 'tag' and not tok.closing and tok.name in AUTO_CLOSE_TAGS:
            open_stack.append(tok)
        elif tok.type == 'tag' and tok.closing and tok.name in AUTO_CLOSE_TAGS:
            if open_stack and open_stack[-1].name == tok.name:
                open_stack.pop()

    for unclosed in reversed(open_stack):
        closer = Token('tag', f'[/{unclosed.name}]', closing=True, name=unclosed.name)
        new_tokens.append(closer)
        all_changes.append(f'Zamknięto niedomknięty znacznik [{unclosed.name}]')

    repaired = ''.join(str(t) for t in new_tokens)
    return repaired, all_changes


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class LintError:
    message: str
    context: str = ''    # surrounding text for context

    def __str__(self):
        if self.context:
            return f'{self.message} (przy: …{self.context}…)'
        return self.message


def validate(text: str) -> list[LintError]:
    """Check BBCode for errors that cannot be auto-repaired. Returns list of LintErrors."""
    errors: list[LintError] = []
    tokens = _tokenize(text)
    stack: list[Token] = []   # open paired tags

    # Track whether we're inside [code] (inner tags are literal, skip validation)
    in_literal = False

    for tok in tokens:
        if tok.type != 'tag':
            continue

        name = tok.name

        if in_literal:
            # Only look for [/code] to exit literal mode
            if tok.closing and name == 'code':
                in_literal = False
                if stack and stack[-1].name == 'code':
                    stack.pop()
            continue

        if name == 'code' and not tok.closing:
            in_literal = True

        # Unknown tag
        if name not in ALL_KNOWN_TAGS:
            ctx = tok.text
            errors.append(LintError(f'Nieznany znacznik: {tok.text}', ctx))
            continue

        if tok.name in STANDALONE_TAGS:
            continue

        if not tok.closing:
            # Validate options for specific tags
            if name == 'size' or name == 'font':
                if not tok.option.strip().isdigit():
                    errors.append(LintError(
                        f'[{name}=] wymaga liczby, np. [{name}=14], otrzymano: [{name}={tok.option}]'
                    ))
            elif name == 'color' and not tok.option.strip():
                errors.append(LintError(f'[color=] wymaga wartości koloru, np. [color=red]'))
            elif name == 'url' and tok.option and not _looks_like_url(tok.option.strip()):
                errors.append(LintError(
                    f'[url=] zawiera nieprawidłowy adres: {tok.option[:80]}'
                ))

            stack.append(tok)
        else:
            # Closing tag
            if not stack:
                errors.append(LintError(
                    f'Zamknięcie {tok.text} bez otwierającego znacznika'
                ))
            elif stack[-1].name != name:
                # Mismatched — find what's open
                open_names = [t.name for t in reversed(stack)]
                errors.append(LintError(
                    f'Nieprawidłowe zagnieżdżenie: zamykasz [{name}], '
                    f'ale otwarty jest [{stack[-1].name}]'
                ))
                # Try to recover: pop until we find the matching tag
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i].name == name:
                        stack = stack[:i]
                        break
            else:
                stack.pop()

    # Unclosed block tags remaining on stack
    for tok in reversed(stack):
        if tok.name in AUTO_CLOSE_TAGS:
            continue  # already handled by repair()
        errors.append(LintError(
            f'Niedomknięty znacznik: [{tok.name}]'
            + (f' (z opcją ={tok.option})' if tok.option else '')
        ))

    return errors


def _looks_like_url(s: str) -> bool:
    return bool(re.match(r'https?://', s, re.IGNORECASE)) or \
           bool(re.match(r'www\.', s, re.IGNORECASE)) or \
           bool(re.match(r'/', s))   # relative URL


# ---------------------------------------------------------------------------
# Combined: repair then validate
# ---------------------------------------------------------------------------

def repair_and_validate(text: str) -> tuple[str, list[str], list[LintError]]:
    """
    Run repair then validate.
    Returns (repaired_text, changes, errors).
    If errors is empty, repaired_text is ready to save.
    """
    repaired, changes = repair(text)
    errors = validate(repaired)
    return repaired, changes, errors
