"""
Client-side Argon2 pre-hashing.

The browser computes argon2id(password, salt) before sending — the server
never sees the plaintext password.  Management commands and the admin panel
use prehash_password() to simulate the same step in Python.

Parameters must match exactly between JS (hash-wasm) and Python (argon2-cffi).
"""

from argon2.low_level import hash_secret_raw, Type

# Must match the JS constants in login.html / register.html
# Benchmarked 2026-04-01: desktop WASM ~355ms, smartphone WASM ~1340ms
PREHASH_MEMORY    = 262144  # KB (256 MB)
PREHASH_TIME      = 2
PREHASH_PARALLEL  = 1
PREHASH_HASHLEN   = 32      # bytes → 64 hex chars

SITE_SALT_SUFFIX = ":sfiniabb"


def prehash_password(password: str, username: str) -> str:
    """Return hex-encoded argon2id(password, username+':sfiniabb').

    Deterministic — same password + username always gives the same hex string.
    Used by CLI commands and the auth backend (for admin/CLI logins without JS).
    """
    salt = (username.lower() + SITE_SALT_SUFFIX).encode()
    raw = hash_secret_raw(
        secret=password.encode(),
        salt=salt,
        time_cost=PREHASH_TIME,
        memory_cost=PREHASH_MEMORY,
        parallelism=PREHASH_PARALLEL,
        hash_len=PREHASH_HASHLEN,
        type=Type.ID,
    )
    return raw.hex()
