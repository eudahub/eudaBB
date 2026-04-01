"""
Email privacy utilities.

Email is stored as Argon2 hash (with salt) — never in plaintext.
Verification works identically to password: hash(input, stored_salt) == stored_hash.

Display mask: jan.iksinski@gmail.com → j***i@gmail.com
For unknown providers the domain host is also masked: sluzbowy@firma.pl → s**y@f***a.pl
"""

import re

from django.contrib.auth.hashers import make_password, check_password


SHORT_LOCAL_THRESHOLD = 4  # local parts this short get a weak-mask warning


KNOWN_PROVIDERS = {
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "hotmail.pl", "live.com",
    "yahoo.com", "yahoo.pl",
    "wp.pl", "onet.pl", "onet.eu", "interia.pl", "o2.pl", "tlen.pl",
    "poczta.fm", "gazeta.pl", "op.pl",
    "protonmail.com", "proton.me", "icloud.com", "me.com",
}


def hash_email(email: str) -> str:
    """Return Argon2 hash of normalised email (salt embedded in hash string)."""
    return make_password(email.strip().lower(), hasher="argon2")


def verify_email(email: str, stored_hash: str) -> bool:
    """Return True if email matches the stored Argon2 hash."""
    return check_password(email.strip().lower(), stored_hash)


def _mask_part(s: str) -> str:
    """Keep first and last char, replace middle with up to 3 stars."""
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

    Returns an empty list for long local parts — no choice needed.
    The list is ordered from least to most private:
      j*n@wp.pl, j*@wp.pl, *n@wp.pl, *@wp.pl
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
