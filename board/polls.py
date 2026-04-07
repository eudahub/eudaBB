import re
from datetime import datetime
from zoneinfo import ZoneInfo
from django.conf import settings


_POLL_RESULT_RE = re.compile(r"^(?P<option>.+?)\s+\|\s+(?P<pct>\d+)%\s+\|\s+\[\s*(?P<votes>\d+)\s*\]\s*$")
_TOTAL_VOTES_RE = re.compile(r"^Wszystkich Głosów\s*:\s*(?P<votes>\d+)\s*$", re.IGNORECASE)
_WARSAW = ZoneInfo("Europe/Warsaw")


def parse_poll_results_text(results_text: str):
    lines = [line.strip() for line in (results_text or "").splitlines() if line.strip()]
    if not lines:
        return {"question": "", "options": [], "total_votes": 0}

    question = lines[0]
    options = []
    total_votes = 0

    for line in lines[1:]:
        total_match = _TOTAL_VOTES_RE.match(line)
        if total_match:
            total_votes = int(total_match.group("votes"))
            continue
        match = _POLL_RESULT_RE.match(line)
        if not match:
            continue
        options.append({
            "option_text": match.group("option").strip(),
            "vote_count": int(match.group("votes")),
            "percent": int(match.group("pct")),
        })

    return {
        "question": question,
        "options": options,
        "total_votes": total_votes,
    }


def parse_archive_datetime(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=_WARSAW)
    except ValueError:
        return None


def get_poll_options_limit(original_count: int = 0) -> int:
    hard_limit = max(1, getattr(settings, "POLL_OPTIONS_HARD_MAX", 64))
    soft_limit = max(1, getattr(settings, "POLL_OPTIONS_SOFT_MAX", 32))
    return min(hard_limit, max(int(original_count or 0), soft_limit))


def validate_poll_option_count(option_count: int, original_count: int = 0) -> tuple[int, list[str]]:
    option_count = int(option_count or 0)
    original_count = int(original_count or 0)
    hard_limit = max(1, getattr(settings, "POLL_OPTIONS_HARD_MAX", 64))
    allowed_limit = get_poll_options_limit(original_count=original_count)

    if option_count > hard_limit:
        return allowed_limit, [
            f"Ankieta ma {option_count} opcji, ale twardy limit to {hard_limit}."
        ]

    if option_count > allowed_limit:
        if original_count > allowed_limit:
            return allowed_limit, [
                f"Ankieta miała {original_count} opcji. Możesz ją tylko zmniejszać; "
                f"obecny limit dla tej edycji to {allowed_limit}."
            ]
        return allowed_limit, [
            f"Ankieta ma {option_count} opcji, ale obecny limit to {allowed_limit}."
        ]

    return allowed_limit, []
