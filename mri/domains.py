"""
Domain normalisation. One input, many shapes.

`example.com`, `https://www.example.com/pricing?ref=x`, `EXAMPLE.COM.`,
`user@example.com` and `www.example.co.uk` all collapse to the registrable
domain via tldextract's bundled public suffix snapshot. Snapshot, not live
fetch: a form field must not depend on an outbound call to render an answer.
"""
from __future__ import annotations

import ipaddress
import re

import tldextract

# suffix_list_urls=() pins tldextract to the snapshot shipped inside the wheel.
# No network, no cache directory, no first-request latency spike.
_extract = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.I)


class InvalidDomain(ValueError):
    pass


def normalize_domain(raw: str) -> str:
    """
    Return the registrable domain for any reasonable user input.

    Raises InvalidDomain with a message fit to show in the UI.
    """
    if raw is None:
        raise InvalidDomain("Enter a domain.")

    value = raw.strip()
    if not value:
        raise InvalidDomain("Enter a domain.")

    # Someone pasted an email address rather than a site.
    if "@" in value and not _SCHEME_RE.match(value):
        value = value.rsplit("@", 1)[-1]

    if not _SCHEME_RE.match(value):
        value = "//" + value

    # Strip everything that is not host: scheme, credentials, path, query, port.
    host = value.split("//", 1)[1]
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("["):  # bracketed IPv6
        raise InvalidDomain("Enter a domain name, not an IP address.")
    host = host.split(":", 1)[0]
    host = host.strip().strip(".").lower()

    if not host:
        raise InvalidDomain("That does not contain a domain.")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise InvalidDomain("Enter a domain name, not an IP address.")

    parts = _extract(host)
    if not parts.domain or not parts.suffix:
        raise InvalidDomain(f"'{raw.strip()[:60]}' is not a resolvable domain name.")

    registrable = f"{parts.domain}.{parts.suffix}"
    for label in registrable.split("."):
        if not _LABEL_RE.match(label):
            raise InvalidDomain(f"'{registrable}' contains an invalid label.")

    return registrable


def domain_parts(domain: str):
    """(subdomain, domain, suffix) for an already-normalised domain."""
    return _extract(domain)


def tld_of(domain: str) -> str:
    """Effective top-level domain, e.g. 'com', 'co.uk', 'top'."""
    return _extract(domain).suffix


def cctld_country(domain: str) -> str | None:
    """
    ISO-3166 alpha-2 implied by a country-code TLD, if the domain uses one.

    Only the final label counts: 'co.uk' implies GB, 'com' implies nothing.
    """
    suffix = tld_of(domain)
    if not suffix:
        return None
    last = suffix.rsplit(".", 1)[-1]
    if len(last) != 2:
        return None
    return CCTLD_TO_ISO.get(last, last.upper())


# ccTLDs whose two letters are not the ISO country code, plus the handful of
# ccTLDs that are marketed and used globally and therefore imply nothing.
CCTLD_TO_ISO = {
    "uk": "GB",
    "ac": "SH",
    "eu": "EU",
    "su": "RU",
    "tp": "TL",
}

GENERIC_USE_CCTLDS = {
    "io", "ai", "co", "me", "tv", "cc", "ly", "sh", "gg", "to", "fm", "am",
    "is", "so", "st", "us", "ws", "nu", "dev",
}


def cctld_is_meaningful(domain: str) -> bool:
    """False for ccTLDs that are sold as generic namespaces (.io, .ai, .co)."""
    suffix = tld_of(domain)
    last = suffix.rsplit(".", 1)[-1] if suffix else ""
    return len(last) == 2 and last not in GENERIC_USE_CCTLDS


def root_urls(domain: str) -> list[str]:
    """Ordered candidates for the site root."""
    return [f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"]
