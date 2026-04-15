"""GeoIP2 helpers — optional feature.

Requires:
  - pip install geoip2
  - GEOIP_DB_PATH in settings pointing to GeoLite2-Country.mmdb

If geoip2 is not installed or the .mmdb file is missing, all functions
return None/False gracefully without errors.
"""

import os

_reader = None
_reader_tried = False


def _get_reader():
    global _reader, _reader_tried
    if _reader_tried:
        return _reader
    _reader_tried = True
    try:
        import geoip2.database
        from django.conf import settings
        path = getattr(settings, "GEOIP_DB_PATH", "/opt/geoip/GeoLite2-Country.mmdb")
        if os.path.exists(path):
            _reader = geoip2.database.Reader(path)
    except Exception:
        pass
    return _reader


def get_country_info(ip: str) -> tuple:
    """Return (iso_code, english_name) or (None, None)."""
    if not ip:
        return None, None
    reader = _get_reader()
    if reader is None:
        return None, None
    try:
        c = reader.country(ip).country
        return c.iso_code, c.name
    except Exception:
        return None, None


def get_country_code(ip: str) -> str | None:
    return get_country_info(ip)[0]


def is_country_blocked(ip: str) -> bool:
    code = get_country_code(ip)
    if not code:
        return False
    from board.models import BlockedCountry
    return BlockedCountry.objects.filter(country_code=code).exists()
