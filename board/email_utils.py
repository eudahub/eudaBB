"""
Email display utilities.

Email is stored in plaintext in User.email (blank=True for ghost accounts
that haven't provided one yet).

Display mask: jan.iksinski@gmail.com → j***i@gmail.com
For unknown providers the domain host is also masked: sluzbowy@firma.pl → s**y@f***a.pl
"""

import re

SHORT_LOCAL_THRESHOLD = 4  # local parts this short offer mask variant choices

KNOWN_PROVIDERS = {
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "hotmail.pl", "live.com",
    "yahoo.com", "yahoo.pl",
    "wp.pl", "onet.pl", "onet.eu", "interia.pl", "o2.pl", "tlen.pl",
    "poczta.fm", "gazeta.pl", "op.pl",
    "protonmail.com", "proton.me", "icloud.com", "me.com",
}


def _mask_part(s: str) -> str:
    if len(s) <= 1:
        return "*"
    if len(s) == 2:
        return s[0] + "*"
    stars = min(len(s) - 2, 3)
    return s[0] + "*" * stars + s[-1]


def mask_email(email: str) -> str:
    """Return display mask for email address."""
    email = email.strip()
    if "@" not in email:
        return _mask_part(email)

    local, domain = email.rsplit("@", 1)
    domain_lower = domain.lower()

    local_clean = re.sub(r"[^a-zA-Z0-9]", "", local) or local
    masked_local = _mask_part(local_clean)

    if domain_lower in KNOWN_PROVIDERS:
        masked_domain = domain_lower
    else:
        parts = domain_lower.split(".")
        tld = ".".join(parts[-1:]) if len(parts) == 2 else ".".join(parts[-2:])
        host = parts[0]
        masked_domain = _mask_part(host) + "." + tld

    return f"{masked_local}@{masked_domain}"


def mask_email_variants(email: str) -> list:
    """Return alternative mask options when local part is short (≤ SHORT_LOCAL_THRESHOLD).

    Returns empty list for long local parts — no choice needed.
    """
    email = email.strip()
    if "@" not in email:
        return []

    local, domain = email.rsplit("@", 1)
    domain_lower = domain.lower()
    local_clean = re.sub(r"[^a-zA-Z0-9]", "", local) or local
    n = len(local_clean)

    if n > SHORT_LOCAL_THRESHOLD:
        return []

    if domain_lower in KNOWN_PROVIDERS:
        masked_domain = domain_lower
    else:
        parts = domain_lower.split(".")
        tld = ".".join(parts[-1:]) if len(parts) == 2 else ".".join(parts[-2:])
        host = parts[0]
        masked_domain = _mask_part(host) + "." + tld

    variants = []
    if n >= 3:
        variants.append(f"{local_clean[0]}*{local_clean[-1]}@{masked_domain}")
    if n >= 2:
        variants.append(f"{local_clean[0]}*@{masked_domain}")
        variants.append(f"*{local_clean[-1]}@{masked_domain}")
    variants.append(f"*@{masked_domain}")
    return variants
