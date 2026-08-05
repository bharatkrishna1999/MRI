"""
Hosting country from the bundled MaxMind GeoLite2 Country database.

The database file ships in the repository and is read locally, so there is no
rate limit, no API key and no third-party outage to absorb. The only network
step is resolving the domain's A record, which we already need.

GeoLite2 data is created by MaxMind, available from https://www.maxmind.com.
See NOTICE for attribution terms.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

DB_PATH = Path(
    os.environ.get(
        "GEOIP_DB_PATH",
        Path(__file__).resolve().parent.parent / "data" / "GeoLite2-Country.mmdb",
    )
)

_reader = None
_reader_lock = threading.Lock()
_load_error: str | None = None


def _get_reader():
    """maxminddb readers are safe for concurrent reads; open once, reuse."""
    global _reader, _load_error
    if _reader is not None or _load_error is not None:
        return _reader
    with _reader_lock:
        if _reader is None and _load_error is None:
            try:
                import geoip2.database

                _reader = geoip2.database.Reader(str(DB_PATH), mode=1)  # MODE_MMAP
            except Exception as exc:
                _load_error = f"{type(exc).__name__}: {exc}"
    return _reader


def database_info() -> dict:
    reader = _get_reader()
    if reader is None:
        return {"available": False, "error": _load_error, "path": str(DB_PATH)}
    meta = reader.metadata()
    import datetime

    built = datetime.datetime.fromtimestamp(meta.build_epoch, datetime.timezone.utc)
    return {
        "available": True,
        "path": str(DB_PATH),
        "database_type": meta.database_type,
        "built": built.date().isoformat(),
        "node_count": meta.node_count,
    }


def country_for_ip(ip: str) -> dict:
    """
    Resolve an IPv4/IPv6 address to an ISO country code with no outbound call.
    """
    if not ip:
        return {"ok": False, "error": "no ip supplied"}
    reader = _get_reader()
    if reader is None:
        return {"ok": False, "error": _load_error or "GeoLite2 database unavailable"}
    try:
        response = reader.country(ip)
    except Exception as exc:
        # AddressNotFoundError is normal for reserved and unallocated space.
        return {"ok": True, "ip": ip, "country": None, "country_name": None,
                "note": f"{type(exc).__name__}"}
    return {
        "ok": True,
        "ip": ip,
        "country": response.country.iso_code,
        "country_name": response.country.name,
        "continent": response.continent.code,
        "registered_country": response.registered_country.iso_code,
    }
