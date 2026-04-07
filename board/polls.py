import re
from datetime import datetime
from zoneinfo import ZoneInfo


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
