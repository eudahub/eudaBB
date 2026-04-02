"""
Email privacy utilities.

Email is stored as Argon2id hash with DETERMINISTIC salt — never in plaintext.
Deterministic salt is required for O(1) lookup: client computes hash once,
server does WHERE email_hash = ? with an index.

Contrast with password hashes (random salt, verified per-user after username lookup).

Salt scheme: b'sfiniabb:email' — constant for all emails.
Email itself is unique, so a rainbow table would need one entry per email anyway,
making the constant salt no weaker than per-email salt at Argon2id 256MB parameters.

Display mask: jan.iksinski@gmail.com → j***i@gmail.com
For unknown providers the domain host is also masked: sluzbowy@firma.pl → s**y@f***a.pl
"""

import re

from argon2.low_level import hash_secret_raw, Type


SHORT_LOCAL_THRESHOLD = 4  # local parts this short get a weak-mask warning

# Argon2 parameters — must match JS constants in find_account.html / register.html
EMAIL_HASH_MEMORY   = 262144   # KB (256 MB)
EMAIL_HASH_TIME     = 2
EMAIL_HASH_PARALLEL = 1
EMAIL_HASH_LEN      = 32       # bytes → 64 hex chars
EMAIL_HASH_SALT     = b"sfiniabb:email"


def hash_email(email: str) -> str:
    """Return hex-encoded argon2id hash of normalised email (deterministic salt).

    Same email always produces the same hash → supports O(1) DB lookup.
    """
    raw = hash_secret_raw(
        secret=email.strip().lower().encode(),
        salt=EMAIL_HASH_SALT,
        time_cost=EMAIL_HASH_TIME,
        memory_cost=EMAIL_HASH_MEMORY,
        parallelism=EMAIL_HASH_PARALLEL,
        hash_len=EMAIL_HASH_LEN,
        type=Type.ID,
    )
    return raw.hex()


def verify_email(email: str, stored_hash: str) -> bool:
    """Return True if email matches stored hash.

    Works with both old PHC-format hashes (random salt, from argon2-cffi PasswordHasher)
    and new hex-format hashes (deterministic salt).
    """
    normalised = email.strip().lower()
    # New format: 64-char hex string
    if len(stored_hash) == 64 and all(c in "0123456789abcdef" for c in stored_hash):
        return hash_email(normalised) == stored_hash
    # Old PHC format (argon2$argon2id$...) — kept for backward compatibility during migration
    from django.contrib.auth.hashers import check_password
    return check_password(normalised, stored_hash)


KNOWN_PROVIDERS = {
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "hotmail.pl", "live.com",
    "yahoo.com", "yahoo.pl",
    "wp.pl", "onet.pl", "onet.eu", "interia.pl", "o2.pl", "tlen.pl",
    "poczta.fm", "gazeta.pl", "op.pl",
    "protonmail.com", "proton.me", "icloud.com", "me.com",
}


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
