"""PM compression helpers.

BBCode is stored as zlib-compressed UTF-8 bytes (BinaryField / PostgreSQL bytea).
This makes the content opaque in direct DB access without being E2E encrypted.
Compression ratio for BBCode text is typically 3–8×.

Server always handles compression/decompression — client-side fflate is used only
to compress the form submission so the POST payload is also compressed.
If JS is unavailable, the server compresses plain text as fallback.
"""

import zlib
import base64


ZLIB_LEVEL = 6   # balance between speed and ratio


def compress(text: str) -> bytes:
    """Compress UTF-8 BBCode text to zlib bytes."""
    return zlib.compress(text.encode("utf-8"), level=ZLIB_LEVEL)


def decompress(data) -> str:
    """Decompress zlib bytes (from BinaryField) to UTF-8 string."""
    return zlib.decompress(bytes(data)).decode("utf-8")


def compress_from_b64(b64_str: str) -> bytes:
    """Accept base64-encoded zlib bytes from client (fflate output).

    fflate.zlibSync() → Uint8Array → base64 → this function → stored bytes.
    Validates that the bytes are valid zlib before storing.
    Raises ValueError if invalid.
    """
    try:
        raw = base64.b64decode(b64_str)
        # Verify it decompresses correctly
        zlib.decompress(raw)
        return raw
    except Exception as exc:
        raise ValueError(f"Invalid compressed PM content: {exc}") from exc
