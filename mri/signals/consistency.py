"""
Consistency — 5 points.

Do the applicant's declared details agree with what the infrastructure and the
page itself say. Each of these needs a declared value to compare against; where
the applicant left the field blank, the signal returns unavailable rather than
inventing agreement.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..domains import cctld_country, cctld_is_meaningful
from .base import ok, unavailable

_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|bv|ab|oy|pte|pty|plc|"
    r"sarl|sas|srl|llp|lp|private|pvt|holdings?|group|technologies|technology|labs?|"
    r"software|solutions|studios?|ventures)\b\.?",
    re.I,
)


def _canonical(name: str) -> str:
    text = re.sub(r"[^a-z0-9\s]", " ", (name or "").lower())
    text = _LEGAL_SUFFIXES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def geo_consistency(declared_country: str | None, geo: dict, domain: str) -> "Signal":
    hosting = (geo or {}).get("country")
    cc = cctld_country(domain) if cctld_is_meaningful(domain) else None
    declared = (declared_country or "").strip().upper() or None

    known = [v for v in (declared, hosting, cc) if v]
    if len(known) < 2:
        return unavailable("geo_consistency",
                           "Fewer than two of declared country, hosting country and ccTLD were available to compare.")

    parts = []
    if declared:
        parts.append(f"declared {declared}")
    if hosting:
        parts.append(f"hosted in {hosting}")
    if cc:
        parts.append(f"{cc} ccTLD")
    observed = ", ".join(parts)

    # Hosting country is weak evidence on its own: a CDN puts everyone in the US
    # or Ireland. Only a three-way disagreement, or a declared-vs-ccTLD clash, is
    # treated as a real inconsistency.
    if declared and cc and declared != cc:
        return ok("geo_consistency", observed, 25,
                  f"The applicant declared {declared} but registered a {cc} country domain ({observed}), a disagreement the applicant controls both sides of.",
                  ["GEO_MISMATCH"], declared=declared, hosting=hosting, cctld=cc)

    if declared and hosting and declared != hosting and not cc:
        return ok("geo_consistency", observed, 65,
                  f"The applicant declared {declared} while the site is hosted in {hosting}; common with a CDN, so this is noted rather than penalised heavily.",
                  declared=declared, hosting=hosting, cctld=cc)

    if len(set(known)) == 1:
        return ok("geo_consistency", observed, 100,
                  f"Country evidence agrees across every available source ({observed}).",
                  declared=declared, hosting=hosting, cctld=cc)

    return ok("geo_consistency", observed, 80,
              f"Country evidence is broadly consistent ({observed}).",
              declared=declared, hosting=hosting, cctld=cc)


def legal_name_match(declared_name: str | None, bundle: dict) -> "Signal":
    if not (declared_name or "").strip():
        return unavailable("legal_name_match", "The applicant declared no legal name to check the site against.")
    if not bundle.get("root_ok"):
        return unavailable("legal_name_match", "No page content was retrieved, so the legal name could not be checked.")

    canonical = _canonical(declared_name)
    if not canonical:
        return unavailable("legal_name_match", "The declared legal name reduced to nothing comparable after normalisation.")

    text = bundle.get("combined_text", "")
    haystack = _canonical(text)

    if canonical in haystack:
        return ok("legal_name_match", "exact", 100,
                  f"'{declared_name}' appears verbatim in the site's own copy, so the operating entity matches the application.",
                  matched="exact")

    # Fall back to fuzzy matching against the strongest candidate window, since
    # footers abbreviate ("Acme, Inc." rendered as "Acme").
    tokens = canonical.split()
    best = 0.0
    if tokens:
        window = len(tokens)
        words = haystack.split()
        for i in range(0, max(1, len(words) - window + 1)):
            candidate = " ".join(words[i:i + window])
            ratio = SequenceMatcher(None, canonical, candidate).ratio()
            if ratio > best:
                best = ratio
                if best > 0.97:
                    break

    percent = int(best * 100)
    if best >= 0.85:
        return ok("legal_name_match", f"{percent}% similarity", 85,
                  f"A close variant of '{declared_name}' appears in the site copy ({percent}% similarity), consistent with an abbreviated trading name.",
                  similarity=percent)
    if best >= 0.6:
        return ok("legal_name_match", f"{percent}% similarity", 50,
                  f"Only a partial match for '{declared_name}' appears in the site copy ({percent}% similarity); the trading name and the legal entity may differ.",
                  similarity=percent)
    return ok("legal_name_match", f"{percent}% similarity", 15,
              f"'{declared_name}' does not appear anywhere in the site's copy or policy pages, so the operating entity is unconfirmed.",
              ["LEGAL_NAME_MISMATCH"], similarity=percent)


def email_domain_match(declared_email: str | None, domain: str, bundle: dict) -> "Signal":
    email = (declared_email or "").strip().lower()
    if not email or "@" not in email:
        # Fall back to an address published on the site itself.
        site_emails = bundle.get("emails") or []
        if not site_emails:
            return unavailable("email_domain_match",
                               "No contact email was declared and none was published on the site.")
        email = site_emails[0]
        source = "published on the site"
    else:
        source = "declared on the application"

    email_domain = email.rsplit("@", 1)[-1]
    from ..domains import domain_parts

    parts = domain_parts(email_domain)
    registrable = f"{parts.domain}.{parts.suffix}" if parts.suffix else email_domain

    if registrable == domain:
        return ok("email_domain_match", email, 100,
                  f"The contact address {email} ({source}) is on the merchant's own domain.",
                  email=email, email_domain=registrable)

    free_providers = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "protonmail.com",
        "proton.me", "icloud.com", "aol.com", "mail.com", "gmx.com", "yandex.com",
        "zoho.com", "live.com", "msn.com",
    }
    if registrable in free_providers:
        return ok("email_domain_match", email, 30,
                  f"The contact address {email} ({source}) is on a free consumer provider rather than the merchant's own domain.",
                  ["EMAIL_DOMAIN_MISMATCH"], email=email, email_domain=registrable)

    return ok("email_domain_match", email, 55,
              f"The contact address {email} ({source}) is on {registrable}, a different domain from the site being underwritten.",
              ["EMAIL_DOMAIN_MISMATCH"], email=email, email_domain=registrable)
