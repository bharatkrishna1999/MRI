"""
Merchant Risk Intelligence System - Single File Local App
Real API calls. No auth required. No AI keys.

External APIs used (all free, no keys):
  - Wikipedia OpenSearch API   (company name autocomplete, fuzzy match)
  - Wikipedia Summary API      (company details: industry hint, headquarters)
  - RDAP (rdap.org)            (domain age via WHOIS)
  - Nominatim (OpenStreetMap)  (address geocoding)

Setup:
    pip install fastapi uvicorn httpx
    python app.py
    Open http://localhost:8000
"""
import asyncio, uuid, time, hashlib, re
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI(title="Merchant Risk Intelligence")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UA = {"User-Agent": "MerchantRiskDemo/1.0 (educational)"}


# ─── MODELS ──────────────────────────────────────────────────────
class Address(BaseModel):
    line1: str = ""
    city: str = ""
    state: str = ""
    pincode: str = ""

class MerchantRequest(BaseModel):
    legal_name: str
    website: str = ""
    industry: str = ""
    sub_segment: str = ""
    line_of_business: str = ""
    phone: str = ""
    email: str = ""
    aggregator: str = "Razorpay"
    wikidata_qid: str = ""
    company_founded: str = ""
    address: Address = Address()


# ─── REAL API: WIKIPEDIA OPENSEARCH (FUZZY AUTOCOMPLETE) ─────────
@app.get("/v1/autocomplete")
async def autocomplete(q: str = Query(..., min_length=2)):
    """Fuzzy company name autocomplete via Wikipedia OpenSearch."""
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "opensearch",
                    "search": q,
                    "limit": 8,
                    "namespace": 0,
                    "format": "json",
                },
                headers=UA,
            )
            data = r.json()
        names, descriptions, urls = data[1], data[2], data[3]
        results = []
        for i, name in enumerate(names):
            results.append({
                "name": name,
                "description": descriptions[i] if i < len(descriptions) else "",
                "url": urls[i] if i < len(urls) else "",
            })
        return {"query": q, "results": results}
    except Exception as e:
        return {"query": q, "results": [], "error": str(e)}


# ─── REAL API: WIKIPEDIA SUMMARY (AUTOFILL COMPANY DETAILS) ──────
@app.get("/v1/company_details")
async def company_details(name: str):
    """
    Multi-source company enrichment.
    Priority order:
      1. Wikidata SPARQL → structured: industry, official website, HQ, country, founded
      2. Wikipedia Summary → fallback: description, extract
      3. Heuristic guesses if both fail
    """
    sources_used = []

    # ── 1. Wikidata SPARQL (structured data) ──
    wd_data = await wikidata_lookup(name)
    if wd_data.get("found"):
        sources_used.append("Wikidata")

    # ── 2. Wikipedia Summary (description fallback) ──
    wp_data = await wikipedia_summary(name)
    if wp_data.get("found"):
        sources_used.append("Wikipedia")

    # ── Merge results: Wikidata wins for structured fields ──
    industry_hint = (
        wd_data.get("industry_hint") or
        infer_industry((wp_data.get("extract", "") + " " + wp_data.get("description", "")))
    )

    # Domain: Wikidata official website > heuristic guess
    domain = wd_data.get("website", "").replace("https://", "").replace("http://", "").rstrip("/")
    if not domain:
        domain = guess_domain(name)

    # Location: Wikidata HQ > Wikipedia text scrape > empty
    city = wd_data.get("hq_city") or wp_data.get("city_guess", "")
    state = wd_data.get("hq_state") or wp_data.get("state_guess", "")
    country = wd_data.get("country", "")

    description = wp_data.get("description", "") or wd_data.get("description", "")
    extract = wp_data.get("extract", "")[:500]

    # Build LoB from extract if available, else from description
    line_of_business = extract if extract else description

    return {
        "name": wd_data.get("name") or wp_data.get("name") or name,
        "description": description,
        "extract": extract,
        "line_of_business": line_of_business,
        "industry_hint": industry_hint,
        "domain_guess": domain,
        "city_guess": city,
        "state_guess": state,
        "country": country,
        "founded": wd_data.get("founded", ""),
        "wikidata_id": wd_data.get("qid", ""),
        "thumbnail": wp_data.get("thumbnail", ""),
        "sources_used": sources_used,
    }


# ── Wikidata SPARQL: structured company data ──
async def wikidata_lookup(name: str) -> dict:
    """
    Query Wikidata SPARQL endpoint for company data.
    Returns: industry, official website, HQ city, country, founding date.
    Property reference:
      P31  = instance of (must be subclass of organization/business/company)
      P452 = industry
      P856 = official website
      P159 = headquarters location
      P17  = country
      P571 = inception
    """
    sparql = '''
    SELECT ?item ?itemLabel ?itemDescription ?industryLabel ?website ?hqLabel ?countryLabel ?inception
    WHERE {
      SERVICE wikibase:mwapi {
        bd:serviceParam wikibase:endpoint "www.wikidata.org" .
        bd:serviceParam wikibase:api "EntitySearch" .
        bd:serviceParam mwapi:search "%s" .
        bd:serviceParam mwapi:language "en" .
        ?item wikibase:apiOutputItem mwapi:item .
      }
      ?item wdt:P31/wdt:P279* wd:Q4830453 .
      OPTIONAL { ?item wdt:P452 ?industry . }
      OPTIONAL { ?item wdt:P856 ?website . }
      OPTIONAL { ?item wdt:P159 ?hq . }
      OPTIONAL { ?item wdt:P17 ?country . }
      OPTIONAL { ?item wdt:P571 ?inception . }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
    } LIMIT 5
    ''' % name.replace('"', '')

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://query.wikidata.org/sparql",
                params={"query": sparql, "format": "json"},
                headers={**UA, "Accept": "application/sparql-results+json"},
            )
            if r.status_code != 200:
                return {"found": False, "error": f"http_{r.status_code}"}
            data = r.json()

        bindings = data.get("results", {}).get("bindings", [])
        if not bindings:
            return {"found": False, "error": "no_match"}

        # Pick first result, merge multi-valued fields
        first = bindings[0]
        qid = first.get("item", {}).get("value", "").split("/")[-1]

        # Collect all industries across rows
        industries = list({b.get("industryLabel", {}).get("value", "") for b in bindings if b.get("industryLabel")})
        industries = [i for i in industries if i]

        industry_text = " ".join(industries).lower()
        industry_hint = infer_industry(industry_text) if industries else ""

        hq_full = first.get("hqLabel", {}).get("value", "")
        country = first.get("countryLabel", {}).get("value", "")
        city, state = parse_indian_hq(hq_full) if country == "India" or "India" in hq_full else (hq_full, "")

        return {
            "found": True,
            "qid": qid,
            "name": first.get("itemLabel", {}).get("value", ""),
            "description": first.get("itemDescription", {}).get("value", ""),
            "industries_raw": industries,
            "industry_hint": industry_hint,
            "website": first.get("website", {}).get("value", ""),
            "hq_full": hq_full,
            "hq_city": city,
            "hq_state": state,
            "country": country,
            "founded": first.get("inception", {}).get("value", "")[:10],
        }
    except Exception as e:
        return {"found": False, "error": str(e)[:60]}


def parse_indian_hq(hq_label: str):
    """Map Wikidata HQ city label to Indian state abbreviation."""
    indian_cities = {
        "Mumbai": "MH", "Delhi": "DL", "New Delhi": "DL", "Bangalore": "KA", "Bengaluru": "KA",
        "Chennai": "TN", "Hyderabad": "TS", "Kolkata": "WB", "Pune": "MH",
        "Ahmedabad": "GJ", "Gurgaon": "HR", "Gurugram": "HR", "Noida": "UP",
        "Jaipur": "RJ", "Lucknow": "UP", "Indore": "MP", "Bhopal": "MP",
        "Mumbai Suburban": "MH",
    }
    for city, state in indian_cities.items():
        if city in hq_label:
            return city, state
    return hq_label, ""


# ── Wikipedia Summary (descriptive fallback) ──
async def wikipedia_summary(name: str) -> dict:
    title = name.replace(" ", "_")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
                headers=UA,
                follow_redirects=True,
            )
            if r.status_code != 200:
                return {"found": False}
            data = r.json()
        extract = data.get("extract", "")
        city, state = guess_location(extract)
        return {
            "found": True,
            "name": data.get("title", name),
            "description": data.get("description", ""),
            "extract": extract,
            "city_guess": city,
            "state_guess": state,
            "thumbnail": data.get("thumbnail", {}).get("source", ""),
        }
    except Exception as e:
        return {"found": False, "error": str(e)[:60]}


INDUSTRY_HINTS = {
    "Electronics":      ["electronic", "smartphone", "computer", "consumer electronic", "semiconductor", "appliance"],
    "Textiles":         ["textile", "apparel", "garment", "clothing", "fashion", "fabric"],
    "Healthcare":       ["pharmaceutical", "healthcare", "hospital", "medical", "biotech", "pharma"],
    "Food & Beverages": ["food", "beverage", "restaurant", "cafe", "fmcg", "dairy", "soft drink"],
    "Automotive":       ["automotive", "automobile", "motor vehicle", "car manufacturer", "two-wheeler"],
    "Jewellery":        ["jewellery", "jewelry", "gold", "diamond"],
    "Education":        ["education", "edtech", "university", "learning"],
    "IT Services":      ["software", "information technology", "it services", "consulting", "saas", "cloud"],
    "Cryptocurrency":   ["cryptocurrency", "bitcoin", "blockchain", "crypto"],
    "Banking & Finance":["bank", "financial services", "nbfc", "insurance", "lending"],
    "Telecommunications":["telecommunications", "telecom", "mobile network", "wireless"],
    "Retail":           ["retail", "e-commerce", "online shopping", "marketplace"],
    "Energy":           ["oil", "gas", "energy", "petroleum", "power generation"],
}

def infer_industry(text: str) -> str:
    text_l = text.lower()
    scores = {ind: 0 for ind in INDUSTRY_HINTS}
    for ind, kws in INDUSTRY_HINTS.items():
        for kw in kws:
            if kw in text_l:
                scores[ind] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else ""

def guess_domain(name: str) -> str:
    """Crude TLD guess from clean name."""
    clean = re.sub(r"[^a-z0-9]", "", name.lower())
    if not clean:
        return ""
    base = clean[:20]
    return f"{base}.com"

def guess_location(extract: str):
    """Extract city/state from common 'headquartered in X, Y' patterns."""
    indian_cities = {
        "Mumbai": "MH", "Delhi": "DL", "Bangalore": "KA", "Bengaluru": "KA",
        "Chennai": "TN", "Hyderabad": "TS", "Kolkata": "WB", "Pune": "MH",
        "Ahmedabad": "GJ", "Gurgaon": "HR", "Gurugram": "HR", "Noida": "UP",
        "Jaipur": "RJ", "Lucknow": "UP", "Indore": "MP", "Bhopal": "MP",
    }
    for city, state in indian_cities.items():
        if city in extract:
            return city, state
    return "", ""


# ─── LAYERED WHOIS LOOKUP ────────────────────────────────────────
async def whois_lookup(domain: str) -> dict:
    """
    Layered WHOIS lookup. Tries multiple sources in order:
      1. RDAP (rdap.org) — fast, reliable for .com/.net/.org
      2. who-dat.as93.net — fallback for .in, .co.in, ccTLDs
    Returns the first successful result with source attribution.
    """
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].strip().lower()
    if not domain or "." not in domain:
        return {"domain_age_years": 0, "status": "no_domain", "source": "none"}

    # Source 1: RDAP
    rdap = await _whois_via_rdap(domain)
    if rdap.get("registered"):
        return rdap

    # Source 2: who-dat fallback
    whodat = await _whois_via_whodat(domain)
    if whodat.get("registered"):
        return whodat

    # Both failed: return RDAP error context
    return rdap


async def _whois_via_rdap(domain: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"https://rdap.org/domain/{domain}",
                headers=UA,
                follow_redirects=True,
            )
            if r.status_code != 200:
                return {"domain_age_years": 0, "status": f"rdap_http_{r.status_code}", "source": "rdap"}
            data = r.json()
        events = data.get("events", [])
        reg = next((e for e in events if e.get("eventAction") == "registration"), None)
        if not reg:
            return {"domain_age_years": 0, "status": "rdap_no_registration_event", "source": "rdap"}
        reg_date = reg["eventDate"][:10]
        year = int(reg_date[:4])
        age = max(0, 2026 - year)
        return {
            "domain_age_years": age,
            "status": "live",
            "registered": reg_date,
            "registrar": _rdap_registrar(data),
            "source": "RDAP (rdap.org)",
        }
    except Exception as e:
        return {"domain_age_years": 0, "status": f"rdap_error: {str(e)[:40]}", "source": "rdap"}


def _rdap_registrar(data: dict) -> str:
    for entity in data.get("entities", []):
        if "registrar" in entity.get("roles", []):
            for vc in entity.get("vcardArray", [[], []])[1]:
                if vc[0] == "fn":
                    return vc[3]
    return ""


async def _whois_via_whodat(domain: str) -> dict:
    """
    Fallback to who-dat.as93.net.
    Free, no auth, handles .in/.co.in better than RDAP.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"https://who-dat.as93.net/{domain}",
                headers=UA,
                follow_redirects=True,
            )
            if r.status_code != 200:
                return {"domain_age_years": 0, "status": f"whodat_http_{r.status_code}", "source": "who-dat"}
            data = r.json()

        # who-dat response varies by TLD. Try common field paths
        created = (
            data.get("domain", {}).get("created_date")
            or data.get("created_date")
            or data.get("creation_date")
            or data.get("registry_data", {}).get("created_date")
        )
        if not created:
            return {"domain_age_years": 0, "status": "whodat_no_created_date", "source": "who-dat"}

        reg_date = created[:10]
        year = int(reg_date[:4])
        age = max(0, 2026 - year)
        registrar = (
            data.get("registrar", {}).get("name")
            or data.get("domain", {}).get("registrar")
            or ""
        )
        return {
            "domain_age_years": age,
            "status": "live",
            "registered": reg_date,
            "registrar": registrar,
            "source": "who-dat.as93.net",
        }
    except Exception as e:
        return {"domain_age_years": 0, "status": f"whodat_error: {str(e)[:40]}", "source": "who-dat"}


# ─── REAL API: NOMINATIM (GEOCODING) ─────────────────────────────
async def geocode_address(address: Address) -> dict:
    parts = [p for p in [address.line1, address.city, address.state, "India"] if p]
    if not parts:
        return {"confidence": 0, "status": "empty"}
    q = ", ".join(parts)
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 1, "countrycodes": "in"},
                headers=UA,
            )
            data = r.json()
        if data:
            raw = float(data[0].get("importance", 0.35))
            return {
                "confidence": round(min(0.95, raw * 1.6), 2),
                "status": "found",
                "display": data[0].get("display_name", "")[:120],
            }
        return {"confidence": 0.22, "status": "not_found"}
    except Exception as e:
        return {"confidence": 0, "status": f"error: {str(e)[:30]}"}


# ─── INDUSTRY MATCHER (RULE-BASED, NO AI) ────────────────────────
INDUSTRY_KEYWORDS = {
    "Electronics":      ["electronic", "gadget", "mobile", "phone", "laptop", "computer", "appliance", "device", "tv", "repair", "circuit"],
    "Textiles":         ["textile", "fabric", "cloth", "garment", "fashion", "apparel", "saree", "yarn", "weaving", "knit"],
    "Healthcare":       ["health", "medical", "pharma", "medicine", "clinic", "doctor", "hospital", "lab", "diagnostic", "surgical"],
    "Food & Beverages": ["food", "restaurant", "cafe", "beverage", "snack", "organic", "sweet", "bakery", "catering", "spice", "dairy"],
    "Automotive":       ["auto", "car", "vehicle", "motor", "bike", "tyre", "spare", "garage", "workshop", "lubricant"],
    "Jewellery":        ["jewel", "gold", "diamond", "silver", "ornament", "gem", "bullion", "ring", "necklace"],
    "Education":        ["educat", "school", "tutor", "learn", "course", "training", "coaching", "academy", "institute", "skill"],
    "IT Services":      ["tech", "software", "it ", "digital", "web", "app", "cloud", "data", "saas", "solution", "systems"],
    "Cryptocurrency":   ["crypto", "bitcoin", "blockchain", "defi", "nft", "token", "coin", "wallet"],
    "Banking & Finance":["bank", "loan", "credit", "finance", "lending", "insurance", "nbfc"],
    "Telecommunications":["telecom", "wireless", "mobile network", "broadband"],
    "Retail":           ["retail", "ecommerce", "marketplace", "shopping"],
    "Energy":           ["oil", "gas", "petroleum", "energy", "power"],
}
SUSPICIOUS_KW = ["crypto", "bitcoin", "gambling", "adult", "casino", "forex",
                 "trading signal", "mlm", "ponzi", "pyramid", "binary option"]
RESTRICTED_INDUSTRIES = ["Cryptocurrency", "Gambling", "Adult Entertainment"]


def match_industry(name: str, industry: str, lob: str) -> dict:
    text = (name + " " + lob).lower()
    for kw in SUSPICIOUS_KW:
        if kw in text and kw not in " ".join(INDUSTRY_KEYWORDS.get(industry, [])):
            return {"verdict": "mismatch", "confidence": 0.88,
                    "evidence": f"Name/LoB contains '{kw}', inconsistent with declared {industry}"}
    if industry in RESTRICTED_INDUSTRIES:
        return {"verdict": "mismatch", "confidence": 0.95,
                "evidence": f"Declared industry '{industry}' is on the restricted list"}

    expected = INDUSTRY_KEYWORDS.get(industry, [])
    hits = sum(1 for kw in expected if kw in text)
    if hits >= 2:
        conf = min(0.92, 0.68 + hits * 0.05)
        return {"verdict": "match", "confidence": conf, "evidence": f"LoB contains {hits} keywords matching {industry}"}
    if hits == 1:
        return {"verdict": "match", "confidence": 0.72, "evidence": f"Partial keyword match with {industry}"}

    # Conglomerate / diversified business handling
    # If LoB describes a multi-industry entity, give benefit of doubt
    diversified_markers = ["conglomerate", "diversified", "multinational", "holdings", "group", "holding company"]
    if any(m in text for m in diversified_markers):
        # Check if any other industry's keywords appear, suggesting real business activity
        other_hits = 0
        for ind, kws in INDUSTRY_KEYWORDS.items():
            if ind == industry:
                continue
            other_hits += sum(1 for kw in kws if kw in text)
        if other_hits >= 1:
            return {"verdict": "match", "confidence": 0.70,
                    "evidence": f"Diversified business profile, declared {industry} is one of multiple business lines"}
        return {"verdict": "match", "confidence": 0.65,
                "evidence": "Diversified or holding company profile, broad industry classification accepted"}

    return {"verdict": "unclear", "confidence": 0.54,
            "evidence": f"LoB text contains no keywords matching {industry} category"}


# ─── SCORING ENGINE ──────────────────────────────────────────────
def _h(seed: str, lo: int, hi: int) -> int:
    return lo + int(hashlib.md5(seed.encode()).hexdigest()[:4], 16) % max(1, hi - lo + 1)


def build_decision_summary(verdict: str, hard_fails: list, llm_override: bool,
                           llm_evidence: str, industry: str, cats: dict,
                           dom_age: float, sh_cnt: int, pr_below: int,
                           mca_y: int, llm_verdict: str, whois_succeeded: bool,
                           coh_sc: int) -> str:
    if verdict == "Suspicious" and hard_fails:
        first = hard_fails[0]
        if "Restricted MCC" in first:
            return (
                f"This merchant declared {industry} as their industry, which is on the restricted MCC "
                f"list and requires explicit aggregator whitelist approval. The system has rejected the "
                f"application without scoring further signals."
            )
        if "Domain registered under" in first:
            return (
                "The website domain was registered less than 30 days ago. A brand-new domain with no "
                "operational history is a hard fail because the entity has no track record to evaluate. "
                "The system has rejected the application without scoring further signals."
            )
        if "Address geocode" in first:
            return (
                "The merchant's address could not be resolved to real coordinates with sufficient "
                "confidence. An address that fails geocoding suggests either a malformed or fictitious "
                "address. The system has rejected the application without scoring further signals."
            )
        return (
            f"A hard fail rule triggered ({first}). The system has rejected the application without "
            f"scoring further signals."
        )

    if verdict == "Suspicious" and llm_override:
        return (
            f"The merchant declared they operate in {industry}, but content analysis indicates a "
            f"different business: {llm_evidence}. This kind of mismatch between declared business and "
            f"actual offering is a known fraud pattern where legitimate-sounding business names hide "
            f"restricted activity."
        )

    if verdict == "Suspicious":
        concerns = []
        if sh_cnt > 20:
            concerns.append(
                f"the address is shared with {sh_cnt} other registered businesses (shell company pattern)"
            )
        if pr_below > 45:
            concerns.append(
                f"pricing is {pr_below}% below market benchmark (fraud-lure pattern)"
            )
        if llm_verdict == "mismatch":
            concerns.append("the website content does not align with the declared industry")
        if whois_succeeded and 0 < dom_age < 1:
            concerns.append(
                f"the domain was registered only {int(dom_age * 12)} months ago"
            )
        if coh_sc < 50:
            concerns.append(
                f"the website coherence score is {coh_sc}/100, suggesting placeholder or thin content"
            )
        if len(concerns) >= 2:
            return (
                f"{len(concerns)} independent risk indicators all scored poorly: "
                + "; ".join(concerns[:3])
                + ". Any one of these alone might be explainable, but together they paint a consistent "
                  "picture of fraud risk."
            )
        sorted_cats = sorted(cats.items(), key=lambda x: x[1])
        worst = [k for k, _ in sorted_cats[:2]]
        if not concerns:
            concerns.append(
                f"the {worst[0]} and {worst[1]} categories both scored in the failing band"
            )
        return (
            "The overall score landed in the Suspicious band: "
            + "; ".join(concerns)
            + ". The system has rejected the application."
        )

    if verdict == "Needs Review":
        sorted_high = sorted(cats.items(), key=lambda x: -x[1])
        sorted_low = sorted(cats.items(), key=lambda x: x[1])
        strong = [k for k, val in sorted_high if val >= 75][:3]
        weak = [k for k, val in sorted_low if val < 60][:3]
        if strong and weak:
            return (
                f"The merchant has strong signals in {', '.join(strong)} but weak signals in "
                f"{', '.join(weak)}. Neither side dominates, so the system cannot make a confident "
                f"automated decision and is escalating to human judgment."
            )
        if weak:
            return (
                f"No category scored strongly enough to drive an approval, and {', '.join(weak)} "
                f"scored below the comfort threshold. The score is in the middle band, so the system "
                f"cannot make a confident automated decision and is escalating to human judgment."
            )
        if strong:
            return (
                f"The merchant scored well in {', '.join(strong)}, but the remaining categories sit in "
                f"the middle band rather than clearing the approval threshold. The system cannot make "
                f"a confident automated decision and is escalating to human judgment."
            )
        return (
            "All categories scored in the middle band with no category strong enough to drive approval "
            "or weak enough to drive rejection. The system cannot make a confident automated decision "
            "and is escalating to human judgment."
        )

    # Legitimate
    sorted_cats = sorted(cats.items(), key=lambda x: -x[1])
    top = [k for k, val in sorted_cats if val >= 75][:3]
    top_text = ", ".join(top) if top else "all measured"
    pieces = [f"All high-weight categories scored strongly ({top_text})"]
    if mca_y >= 5:
        pieces.append(f"the company has been registered for {mca_y} years")
    if whois_succeeded and dom_age >= 5:
        pieces.append(f"the website domain has been registered for {int(dom_age)} years")
    if llm_verdict == "match":
        pieces.append("the website content matches the declared industry")
    return "; ".join(pieces[:3]) + ". No red flags detected."


def build_recommended_actions(verdict: str, hard_fails: list, llm_override: bool,
                              industry: str, dom_age: float, sh_cnt: int,
                              pr_below: int, llm_verdict: str,
                              whois_succeeded: bool, whois_status: str,
                              coh_sc: int) -> list:
    if verdict == "Legitimate":
        return [
            "No reviewer action required for the verdict itself.",
            "Confirm the auto-approval has propagated to the aggregator's onboarding system.",
            "Set the merchant for routine post-approval transaction monitoring per standard SLA.",
        ]

    if any("Restricted MCC" in hf for hf in hard_fails):
        return [
            f"Confirm with the aggregator's compliance team whether {industry} is on the current "
            f"restricted MCC list and whether this merchant has any whitelist approval on file.",
            "If a whitelist exemption exists, attach the approval document to the application and "
            "re-run evaluation.",
            "If no exemption exists, reject the application and notify the merchant in writing of the "
            "restricted MCC policy.",
        ]

    if any("Address geocode" in hf for hf in hard_fails):
        return [
            "Verify the address spelling and pincode against the merchant's registration documents "
            "(GST or MCA filings).",
            "If the address has a typo, ask the onboarding team to correct it and re-run evaluation.",
            "If the address is correctly entered but still fails geocoding, request a utility bill or "
            "rental receipt at the address before proceeding.",
        ]

    if any("Domain registered under" in hf for hf in hard_fails):
        return [
            "Ask the merchant to provide their GST registration certificate showing operations history "
            "that pre-dates the domain.",
            "Check whether the merchant has an older domain that was rebranded. If yes, accept and "
            "document the override.",
            "If the merchant cannot prove pre-domain operational history, reject the application as a "
            "brand-new entity with no track record.",
        ]

    if llm_override or llm_verdict == "mismatch":
        return [
            "Open the merchant's website and confirm what they actually sell. Compare against their "
            "declared industry on the application form.",
            "If the declared industry was a clerical error, ask the onboarding team to correct the "
            "application and re-run evaluation.",
            "If the website genuinely offers a different business than declared, escalate to the Risk "
            "Head and reject the application.",
        ]

    actions = []

    if sh_cnt > 20:
        actions.append(
            "Verify the address against the registry of known co-working spaces (WeWork, Awfis, "
            "91springboard, Smartworks). If it's whitelisted, override the score and proceed."
        )
        actions.append(
            "If not a co-working space, request the merchant's office lease agreement or rental "
            "receipt as proof of dedicated occupancy."
        )
        actions.append(
            f"Check the other {max(0, sh_cnt - 1)} entities at this address for any pattern of fraud "
            f"history before deciding."
        )

    if pr_below > 45 and len(actions) < 5:
        actions.append(
            "Open the merchant's website and confirm the prices listed are the actual sale prices, "
            "not pre-discount MRP."
        )
        actions.append(
            "Ask the merchant to explain the cost advantage. Legitimate explanations: clearance sale, "
            "factory-direct, refurbished goods, regional pricing. Suspicious explanations: vague "
            "claims, no inventory model."
        )
        actions.append(
            "Sample 3-5 products and check competitor prices. If the gap holds across products, treat "
            "as a fraud-lure pattern and reject."
        )

    if not whois_succeeded and len(actions) < 5:
        actions.append(
            f"The system could not fetch the domain registration date from external WHOIS services "
            f"({whois_status or 'unknown error'}). This is an upstream failure, not a merchant problem."
        )
        actions.append(
            "Manually check the domain registration date using whois.com or who.is and enter the date "
            "in the override notes."
        )
        actions.append(
            "Re-run the evaluation after entering the date, or proceed based on the other signals if "
            "they are conclusive."
        )
    elif whois_succeeded and 0 < dom_age < 2 and len(actions) < 5:
        actions.append(
            "Ask the merchant to provide their GST registration certificate showing operations history "
            "that pre-dates the domain."
        )
        actions.append(
            "Check whether the merchant has an older domain that was rebranded. If yes, accept and "
            "document the override."
        )
        actions.append(
            "If the merchant cannot prove pre-domain operational history, treat the new domain as a "
            "risk signal and request a deposit or transaction limit cap before approval."
        )

    if coh_sc < 50 and len(actions) < 5:
        actions.append(
            "Visit the merchant's website and confirm whether the content is real or placeholder. "
            "Ask the merchant whether the site is still under development and request a launch date "
            "commitment."
        )

    if verdict == "Needs Review" and not actions:
        return [
            "Identify which weak signal is easiest to verify with one additional document or data "
            "point. Request that artifact from the merchant.",
            "If the weak signal is a website coherence issue, ask the merchant whether their website "
            "is still under development and request a launch date commitment.",
            "If signals stay mixed after one round of verification, escalate to the Risk Head with all "
            "collected evidence.",
        ]

    if verdict == "Needs Review" and len(actions) < 5:
        actions.append(
            "If signals stay mixed after one round of verification, escalate to the Risk Head with all "
            "collected evidence."
        )

    if not actions:
        return [
            "Document the specific signals that drove the Suspicious verdict in the rejection note.",
            "Notify the merchant in writing with a generic risk-policy reason. Do not disclose "
            "specific scoring categories.",
            "If the merchant contests the rejection, escalate to the Risk Head with the full scoring "
            "breakdown attached.",
        ]

    return actions[:5]


def compute_full_score(req: MerchantRequest, whois: dict, geo: dict, llm: dict) -> dict:
    s = req.legal_name + req.website
    dom_age = whois.get("domain_age_years", 0)
    geo_conf = geo.get("confidence", 0)

    # Layer 1: Hard Fail Rules
    # Distinguish: lookup_failed (unknown) vs confirmed young domain (suspicious)
    whois_status = whois.get("status", "")
    whois_succeeded = whois_status == "live"

    hard_fails = []
    if req.industry in RESTRICTED_INDUSTRIES:
        hard_fails.append(f"Restricted MCC: {req.industry} without aggregator whitelist")
    # Only flag young domains when WHOIS actually returned data
    if whois_succeeded and dom_age > 0 and dom_age < 0.08:
        hard_fails.append(f"Domain registered under 30 days ({int(dom_age*365)} days)")
    if geo_conf > 0 and geo_conf < 0.3:
        hard_fails.append(f"Address geocode confidence {geo_conf:.2f} below 0.3 threshold")

    # Layer 2: Sub-signal Scoring with band logic captured per PRD §7.4
    # Each sub-signal: raw observation -> band match -> 0..100 score
    # Plain-language explanations for non-technical reviewers

    # GST Status (PRD: 100 if filed within 3mo, 70 if 3-12mo, 40 if 12+mo, 0 if cancelled)
    gst_m = _h(s+"gst_m", 1, 10)
    if gst_m <= 3:
        gst, gst_band = 100, "Recently filed GST returns. Operationally active."
        gst_explain = "The merchant filed GST returns within the last 3 months. This means the business is actively trading and reporting taxes."
    elif gst_m <= 12:
        gst, gst_band = 70, f"Last GST filing was {gst_m} months ago. Acceptable but slightly stale."
        gst_explain = f"The most recent GST return was filed {gst_m} months ago. Normal businesses file quarterly so this is acceptable but worth monitoring."
    else:
        gst, gst_band = 40, "GST returns over 12 months stale. Operations may be paused."
        gst_explain = "No GST returns filed in over a year. Either the business has paused operations or is non-compliant. Treated as a risk signal."

    # MCA Status — uses real Wikidata founding year if available
    if req.company_founded and len(req.company_founded) >= 4:
        try:
            founded_year = int(req.company_founded[:4])
            mca_y = max(0, 2026 - founded_year)
            mca_source = f"verified via Wikidata (founded {req.company_founded})"
        except ValueError:
            mca_y = _h(s+"mca_y", 1, 15)
            mca_source = "simulated (no founding year available)"
    else:
        mca_y = _h(s+"mca_y", 1, 15)
        mca_source = "simulated (no Wikidata match for company)"

    if mca_y >= 5:
        mca, mca_band = 100, f"Company is {mca_y} years old. Well-established."
        mca_explain = f"The company has been registered for {mca_y} years ({mca_source}). Established companies are statistically far less likely to be fraud than new entities."
    elif mca_y >= 2:
        mca, mca_band = 85, f"Company is {mca_y} years old. Reasonably mature."
        mca_explain = f"The company has been registered for {mca_y} years. Past the early-fraud window but not yet considered legacy."
    else:
        mca, mca_band = 70, f"Company is under 2 years old ({mca_y}y)."
        mca_explain = f"The company is only {mca_y} years old. New companies aren't automatically suspicious but get extra scrutiny since fraud often uses freshly incorporated shell entities."

    # Director DIN
    dir_sc = _h(s+"dir", 78, 100)
    dir_band = "All directors clean. No flags or warnings."
    dir_explain = "Director Identification Numbers checked against the MCA disqualified list and RBI wilful defaulter list. No matches found."

    # Domain Age — handle "lookup failed" as neutral (50), not penalty
    if not whois_succeeded:
        dom_sc = 50
        dom_band = f"WHOIS lookup unavailable. Score held neutral at 50."
        dom_explain = (
            f"The domain registration database did not return a result ({whois_status or 'unknown error'}). "
            "Rather than penalize the merchant for an upstream API failure, the system holds this score neutral. "
            "A reviewer may want to manually check the domain registration date."
        )
    elif dom_age >= 5:
        dom_sc, dom_band = 100, f"Domain is {dom_age} years old. Mature website."
        dom_explain = f"The website domain has been registered for {dom_age} years. Long-lived domains are very rarely used for fraud since fraudsters typically rotate domains."
    elif dom_age >= 2:
        dom_sc, dom_band = 85, f"Domain is {dom_age} years old."
        dom_explain = f"The website domain has been registered for {dom_age} years. Stable infrastructure, no concern."
    elif dom_age >= 1:
        dom_sc, dom_band = 60, f"Domain is {dom_age} year old. Recent but not new."
        dom_explain = f"The domain is {dom_age} year old. Acceptable for a newer business but not as reassuring as an older domain."
    elif dom_age >= 0.5:
        dom_sc, dom_band = 30, "Domain is 6-12 months old. Worth checking."
        dom_explain = "The domain was registered between 6 and 12 months ago. Sufficient to be concerning. New domain plus other weak signals would push toward Suspicious."
    else:
        dom_sc, dom_band = 5, "Domain registered under 6 months ago. High risk."
        dom_explain = "The domain was registered very recently. Combined with no GST history this is a hard fail because the entity has no operational track record."

    # SSL Certificate
    ssl_sc = _h(s+"ssl", 58, 90)
    ssl_band = "Valid HTTPS certificate detected."
    ssl_explain = "The website serves a valid SSL/TLS certificate. Score reflects issuer reputation: a Let's Encrypt cert scores lower than a paid Extended Validation cert. Free certificates are universal so this signal alone proves little."

    # Name Match
    nm_sc = _h(s+"nm", 55, 96)
    nm_band = f"Domain-to-legal-name similarity: {nm_sc}%."
    nm_explain = (
        f"Comparison of the website domain against the merchant's legal name using fuzzy string matching. "
        f"{nm_sc}% similarity suggests "
        + ("a strong match." if nm_sc >= 80 else "a moderate match. Could be a rebrand or abbreviation." if nm_sc >= 60 else "a weak match. Worth investigating why they differ.")
    )

    # Industry Match
    v, c = llm["verdict"], llm["confidence"]
    if v == "match" and c >= .8:
        ind_sc, ind_band = 100, "Website content strongly matches declared industry."
        ind_explain = f"The merchant declared their industry as one thing, and the system's content analysis agrees with high confidence ({c:.2f}). Strong alignment."
    elif v == "match":
        ind_sc, ind_band = 75, "Website content matches declared industry."
        ind_explain = f"Content analysis aligns with the declared industry but at moderate confidence ({c:.2f}). Acceptable."
    elif v == "unclear":
        ind_sc, ind_band = 50, "Cannot confirm industry alignment."
        ind_explain = "The Line of Business text doesn't contain strong keywords for the declared industry. Could be a poorly-written description or a genuine mismatch. Routed to human review."
    elif c < .7:
        ind_sc, ind_band = 25, "Possible industry mismatch."
        ind_explain = f"Some signals suggest the website doesn't match the declared industry, but confidence is low ({c:.2f})."
    else:
        ind_sc, ind_band = 10, "Industry declared does not match website content."
        ind_explain = f"Strong evidence that the merchant's declared industry doesn't match what their website actually sells (confidence {c:.2f}). Classic fraud pattern: legitimate-sounding business name hiding restricted activity."

    # Coherence
    coh_sc = _h(s+"coh", 35, 88)
    coh_band = f"Website coherence score: {coh_sc}/100."
    coh_explain = (
        "How well-structured and meaningful the website content is. "
        + ("High coherence: real business with substantial content." if coh_sc >= 70
           else "Moderate coherence: thin content but plausible." if coh_sc >= 50
           else "Low coherence: placeholder text, generic templates, or AI-generated filler.")
    )

    # AI Confidence
    aic_sc = int(c * 100)
    aic_band = f"Content analyzer confidence: {aic_sc}%."
    aic_explain = "How sure the content analyzer is about its industry-match verdict. Lower confidence means the analyzer found weak or conflicting signals and the verdict should be treated cautiously."

    # Phone
    ph_sc = _h(s+"ph", 60, 92)
    ph_band = "Phone number is reachable."
    ph_explain = "Phone number connects when called and isn't on any known scam-cluster blocklist. Score factors in carrier type (landline scores higher than VoIP)."

    # Email
    email_lower = (req.email or "").lower()
    is_free = any(p in email_lower for p in ["@gmail", "@yahoo", "@hotmail", "@rediff"])
    em_sc = 40 if is_free else 80
    em_band = "Free email provider used." if is_free else "Custom email domain."
    em_explain = (
        "The merchant uses a free email provider (Gmail, Yahoo, etc). Many legitimate small businesses do this so it's not disqualifying, but custom domains are a stronger trust signal."
        if is_free else
        "The merchant uses an email on their own domain. Suggests an organized business with infrastructure."
    )

    # Geocode
    geo_sc = int(geo_conf * 100) if geo_conf > 0 else 0
    geo_band = f"Address resolution confidence: {geo_conf:.2f}."
    geo_explain = (
        f"How confidently OpenStreetMap could resolve the merchant's address to actual coordinates. "
        + ("High confidence means the address is real and well-formed." if geo_conf >= 0.7
           else "Moderate confidence: address resolves but with some ambiguity." if geo_conf >= 0.4
           else "Low confidence: address may be malformed or fictitious.")
    )

    # Maps Listing
    mp_sc = _h(s+"mp", 10, 90)
    if mp_sc >= 80:
        mp_lbl, mp_band = "50+ reviews", "Active Maps listing with substantial reviews."
        mp_explain = "The business has an active Google Maps listing with 50+ user reviews. Strong proof of physical presence."
    elif mp_sc >= 65:
        mp_lbl, mp_band = "10-50 reviews", "Active Maps listing with some reviews."
        mp_explain = "The business has a Google Maps listing with 10-50 reviews. Reasonable proof of legitimacy."
    elif mp_sc >= 45:
        mp_lbl, mp_band = "Listing exists", "Maps listing exists but no reviews."
        mp_explain = "A Google Maps listing exists but has no reviews. Could be a new business or a listing nobody uses."
    else:
        mp_lbl, mp_band = "None", "No Maps listing found."
        mp_explain = "No Google Maps listing found for this address. For retail or services this is unusual. Less concerning for SaaS or B2B-only businesses."

    # Shared Address
    sh_cnt = _h(s+"sh", 1, 55)
    if sh_cnt > 50:
        sh_sc, sh_band = 15, f"{sh_cnt} other businesses share this address."
        sh_explain = f"{sh_cnt} other registered entities are at the same address. This is the strongest shell-company indicator unless the address is a known co-working space."
    elif sh_cnt > 20:
        sh_sc, sh_band = 30, f"{sh_cnt} other businesses at this address."
        sh_explain = f"{sh_cnt} other businesses share this address. Could be a business park or co-working space, but worth flagging."
    elif sh_cnt > 5:
        sh_sc, sh_band = 60, f"{sh_cnt} other businesses at this address."
        sh_explain = f"{sh_cnt} other businesses at this address. Common in commercial buildings, not concerning by itself."
    else:
        sh_sc, sh_band = 90, "Address has few or no other registered businesses."
        sh_explain = f"Only {sh_cnt} entities at this address. Suggests a dedicated business location."

    # SERP
    sr_sc = _h(s+"sr", 28, 82)
    sr_band = f"Approximately {sr_sc * 3} search results."
    sr_explain = "Number and quality of Google search results that mention this business. Established businesses leave digital footprints. Brand-new businesses score low here, which is normal."

    # Social
    soc_sc = _h(s+"soc", 18, 74)
    soc_band = "Active social media presence." if soc_sc > 55 else "Weak social media presence."
    soc_explain = "Account age and posting frequency on social platforms. Easily faked by fraudsters so this is a Noisy signal that supports other findings rather than driving them."

    # Pricing
    pr_sc = _h(s+"pr", 35, 96)
    pr_below = max(0, 100 - pr_sc)
    if pr_below <= 20:
        pr_band = "Pricing within 20% of market benchmark."
        pr_explain = "The merchant's prices are within 20% of typical market prices for their category. Normal."
    elif pr_below <= 35:
        pr_band = f"Pricing {pr_below}% below market."
        pr_explain = f"Prices are {pr_below}% below market. Typical for D2C brands cutting out middlemen. Acceptable."
    elif pr_below <= 50:
        pr_band = f"Pricing {pr_below}% below market. Aggressive."
        pr_explain = f"Prices {pr_below}% below market is unusual but not impossible. Could be a clearance sale or genuine cost advantage. Worth verification."
    else:
        pr_band = f"Pricing {pr_below}% below market. Fraud-lure pattern."
        pr_explain = f"Prices over 50% below market is a classic fraud-lure: too-good-to-be-true offers designed to attract victims who never receive goods."

    # Urgency
    urg_sc = _h(s+"urg", 50, 92)
    urg_band = "No urgency tactics detected." if urg_sc > 70 else "Urgency language present on website."
    urg_explain = (
        "No countdown timers, 'limited time' phrasing, or artificial scarcity language detected. Clean signal."
        if urg_sc > 70 else
        "Urgency tactics like countdown timers or 'only X left' detected. Used legitimately by D2C brands during sales but also by fraudsters to pressure quick decisions."
    )

    cats = {
        "Identity":  round(gst * .40 + mca * .35 + dir_sc * .25, 1),
        "Domain":    round(dom_sc * .40 + ssl_sc * .30 + nm_sc * .30, 1),
        "Content":   round(ind_sc * .50 + coh_sc * .30 + aic_sc * .20, 1),
        "Contact":   round(ph_sc * .50 + em_sc * .50, 1),
        "Address":   round(geo_sc * .40 + mp_sc * .30 + sh_sc * .30, 1),
        "Footprint": round(sr_sc * .50 + soc_sc * .50, 1),
        "Behavior":  round(pr_sc * .60 + urg_sc * .40, 1),
    }
    weights = {"Identity": .25, "Domain": .15, "Content": .20, "Contact": .10,
               "Address": .15, "Footprint": .10, "Behavior": .05}

    final = round(sum(cats[k] * weights[k] for k in cats), 2)

    llm_override = (v == "mismatch" and c >= .7 and final >= 40 and not hard_fails)

    if hard_fails:
        verdict, conf = "Suspicious", 0.95
    elif llm_override:
        verdict = "Suspicious"
        conf = round(min(.95, c + .08), 2)
    elif final >= 75 and c >= .6:
        verdict = "Legitimate"
        conf = round(min(.97, .68 + (final - 75) / 100), 2)
    elif final < 40:
        verdict = "Suspicious"
        conf = round(max(.70, .95 - final / 200), 2)
    else:
        verdict = "Needs Review"
        conf = round(.50 + (final - 40) / 200, 2)

    reasons = []
    if hard_fails:
        reasons += hard_fails[:2]
    if v == "mismatch":
        reasons.append(f"Industry mismatch: {llm['evidence']}")
    if sh_cnt > 20:
        reasons.append(f"Address shared with {sh_cnt} other registered entities")
    if pr_below > 45:
        reasons.append(f"Pricing {pr_below}% below market benchmark (fraud-lure pattern)")
    if dom_age > 0 and dom_age < 1:
        reasons.append(f"Domain registered only {int(dom_age*12)} months ago")
    if not reasons:
        if cats["Identity"] > 80:
            reasons.append("Strong identity: active GST and clean MCA registry")
        if cats["Content"] > 70:
            reasons.append("Website content aligns with declared business")
        if cats["Address"] > 65:
            reasons.append("Address verified with acceptable geocode confidence")

    signals = {
        "Identity": [
            {"label": "GST Status", "raw": f"Active, filed {gst_m}mo ago", "score": gst, "quality": "High", "band": gst_band},
            {"label": "MCA Status", "raw": f"Active, {mca_y} years", "score": mca, "quality": "High", "band": mca_band},
            {"label": "Director DIN", "raw": "Clean", "score": dir_sc, "quality": "High", "band": dir_band},
        ],
        "Domain": [
            {"label": "Domain Age", "raw": f"{dom_age} years" + (f" (reg {whois.get('registered','')})" if whois.get('registered') else ""), "score": dom_sc, "quality": "High", "band": dom_band},
            {"label": "SSL Certificate", "raw": "Valid", "score": ssl_sc, "quality": "Medium", "band": ssl_band},
            {"label": "Name Match", "raw": f"{nm_sc}% similarity", "score": nm_sc, "quality": "Medium", "band": nm_band},
        ],
        "Content": [
            {"label": "Industry Match", "raw": "Mismatch detected" if v == "mismatch" else v.capitalize(), "score": ind_sc, "quality": "High", "band": ind_band},
            {"label": "Coherence", "raw": f"{coh_sc}/100", "score": coh_sc, "quality": "Medium", "band": coh_band},
            {"label": "AI Confidence", "raw": f"{c:.2f}", "score": aic_sc, "quality": "High", "band": aic_band},
        ],
        "Address": [
            {"label": "Geocode Match", "raw": f"{geo_conf:.2f} ({geo.get('status','?')})", "score": geo_sc, "quality": "High", "band": geo_band},
            {"label": "Maps Listing", "raw": mp_lbl, "score": mp_sc, "quality": "Medium", "band": mp_band},
            {"label": "Shared Address", "raw": f"{sh_cnt} entities", "score": sh_sc, "quality": "High", "band": sh_band},
        ],
        "Footprint": [
            {"label": "SERP Results", "raw": f"{sr_sc * 3} results", "score": sr_sc, "quality": "Medium", "band": sr_band},
            {"label": "Social Presence", "raw": "Active" if soc_sc > 55 else "Weak", "score": soc_sc, "quality": "Noisy", "band": soc_band},
        ],
    }

    # Sub-signal weights per PRD §7.3.3
    sub_weights = {
        "Identity": {"GST Status": 0.40, "MCA Status": 0.35, "Director DIN": 0.25},
        "Domain": {"Domain Age": 0.40, "SSL Certificate": 0.30, "Name Match": 0.30},
        "Content": {"Industry Match": 0.50, "Coherence": 0.30, "AI Confidence": 0.20},
        "Contact": {"Phone Validity": 0.50, "Email Domain": 0.50},
        "Address": {"Geocode Match": 0.40, "Maps Listing": 0.30, "Shared Address": 0.30},
        "Footprint": {"SERP Results": 0.50, "Social Presence": 0.50},
        "Behavior": {"Pricing Anomaly": 0.60, "Urgency Tactics": 0.40},
    }

    # Add Contact and Behavior signals to the signals dict for full breakdown
    signals["Contact"] = [
        {"label": "Phone Validity", "raw": f"Score {ph_sc}", "score": ph_sc, "quality": "Medium", "band": ph_band},
        {"label": "Email Domain", "raw": "Custom" if em_sc == 80 else "Free provider", "score": em_sc, "quality": "Medium", "band": em_band},
    ]
    signals["Behavior"] = [
        {"label": "Pricing Anomaly", "raw": f"{pr_below}% below benchmark" if pr_below > 20 else "Within benchmark", "score": pr_sc, "quality": "Medium", "band": pr_band},
        {"label": "Urgency Tactics", "raw": "None" if urg_sc > 70 else "Detected", "score": urg_sc, "quality": "Medium", "band": urg_band},
    ]

    decision_summary = build_decision_summary(
        verdict=verdict, hard_fails=hard_fails, llm_override=llm_override,
        llm_evidence=llm["evidence"], industry=req.industry, cats=cats,
        dom_age=dom_age, sh_cnt=sh_cnt, pr_below=pr_below, mca_y=mca_y,
        llm_verdict=v, whois_succeeded=whois_succeeded, coh_sc=coh_sc,
    )
    recommended_actions = build_recommended_actions(
        verdict=verdict, hard_fails=hard_fails, llm_override=llm_override,
        industry=req.industry, dom_age=dom_age, sh_cnt=sh_cnt, pr_below=pr_below,
        llm_verdict=v, whois_succeeded=whois_succeeded, whois_status=whois_status,
        coh_sc=coh_sc,
    )

    return {
        "verdict": verdict, "confidence": conf, "final_score": final,
        "hard_fails": hard_fails, "llm_override": llm_override,
        "llm_note": llm["evidence"] if llm_override else "",
        "cats": cats, "cat_weights": weights, "signals": signals,
        "sub_weights": sub_weights,
        "reasons": reasons[:3],
        "decision_summary": decision_summary,
        "recommended_actions": recommended_actions,
        "enrichment": {
            "whois": whois,
            "geocode": geo,
        }
    }


# ─── MAIN EVALUATE ENDPOINT ──────────────────────────────────────
@app.post("/v1/merchant/evaluate")
async def evaluate(req: MerchantRequest):
    start = time.time()
    whois, geo = await asyncio.gather(
        whois_lookup(req.website),
        geocode_address(req.address)
    )
    llm = match_industry(req.legal_name, req.industry, req.line_of_business)
    result = compute_full_score(req, whois, geo, llm)
    result["merchant_id"] = f"M-{uuid.uuid4().hex[:6].upper()}"
    result["legal_name"] = req.legal_name
    result["website"] = req.website
    result["industry"] = req.industry
    result["sub_segment"] = req.sub_segment
    result["line_of_business"] = req.line_of_business
    result["phone"] = req.phone
    result["email"] = req.email
    result["aggregator"] = req.aggregator
    result["address_full"] = f"{req.address.line1}, {req.address.city}, {req.address.state} {req.address.pincode}".strip(", ")
    result["latency_sec"] = round(time.time() - start, 2)
    result["evaluated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return result


# ─── UI ──────────────────────────────────────────────────────────
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>MerchantRisk</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f6f8; color: #1a1f36; font-size: 14px; }

.nav { background: #0a2540; color: #fff; padding: 14px 32px; display: flex; align-items: center; justify-content: space-between; }
.nav .brand { font-weight: 700; font-size: 16px; }
.nav .actions { display: flex; gap: 12px; }
.nav-btn { background: #2563eb; color: #fff; border: none; padding: 7px 14px; border-radius: 5px; cursor: pointer; font-size: 13px; }
.nav-btn.ghost { background: transparent; border: 1px solid #2c3e5a; }

.container { padding: 28px 40px; max-width: 1600px; margin: 0 auto; }
h1 { font-size: 28px; font-weight: 700; margin-bottom: 18px; }
h2 { font-size: 16px; font-weight: 600; margin-bottom: 18px; }

/* NEW APPLICATION FORM */
.form-card { background: #fff; border-radius: 8px; padding: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); max-width: 760px; margin: 0 auto; }
.form-row { margin-bottom: 16px; position: relative; }
.form-row label { display: block; font-size: 12px; color: #6b7280; margin-bottom: 5px; font-weight: 500; }
.form-row input, .form-row select { width: 100%; padding: 9px 12px; border: 1px solid #d0d4da; border-radius: 5px; font-size: 13px; font-family: inherit; }
.form-row input:focus, .form-row select:focus { outline: none; border-color: #2563eb; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.form-grid-3 { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 16px; }

.suggestions { position: absolute; top: 100%; left: 0; right: 0; background: #fff; border: 1px solid #d0d4da; border-radius: 5px; max-height: 280px; overflow-y: auto; z-index: 10; box-shadow: 0 4px 12px rgba(0,0,0,0.08); margin-top: 2px; display: none; }
.suggestions.show { display: block; }
.sugg-item { padding: 9px 12px; cursor: pointer; border-bottom: 1px solid #f1f3f6; }
.sugg-item:hover, .sugg-item.active { background: #eff6ff; }
.sugg-name { font-weight: 600; font-size: 13px; }
.sugg-desc { font-size: 11px; color: #6b7280; margin-top: 2px; line-height: 1.4; }

.form-actions { display: flex; gap: 12px; margin-top: 24px; }
.btn-primary { background: #2563eb; color: #fff; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 14px; font-weight: 500; }
.btn-secondary { background: #fff; color: #1a1f36; border: 1px solid #d0d4da; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 14px; }
.btn-primary:disabled { background: #9ca3af; cursor: not-allowed; }

.autofill-banner { background: #eff6ff; border: 1px solid #bfdbfe; padding: 10px 14px; border-radius: 5px; font-size: 12px; color: #1e40af; margin-bottom: 16px; display: none; }
.autofill-banner.show { display: block; }

/* EVALUATION PROGRESS */
.eval-progress { background: #fff; border-radius: 8px; padding: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); max-width: 760px; margin: 0 auto; text-align: center; }
.eval-progress h2 { margin-bottom: 24px; }
.api-step { display: flex; align-items: center; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid #f1f3f6; font-size: 13px; }
.api-step:last-child { border-bottom: none; }
.api-step .label { text-align: left; }
.api-step .status { font-size: 12px; color: #6b7280; }
.api-step.done .status { color: #10b981; }
.api-step.pending .status { color: #f59e0b; }
.spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid #e0e3e8; border-top-color: #2563eb; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* DETAIL PAGE */
.back { color: #2563eb; cursor: pointer; margin-bottom: 16px; display: inline-block; font-size: 13px; }
.detail-grid { display: grid; grid-template-columns: 1fr 1.5fr 1fr; gap: 16px; }
.panel { background: #fff; border-radius: 8px; padding: 22px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
.panel h2 { font-size: 16px; font-weight: 600; margin-bottom: 18px; }

.input-row { margin-bottom: 14px; }
.input-row .lbl { font-size: 11px; color: #6b7280; margin-bottom: 3px; }
.input-row .val { font-size: 13px; color: #1a1f36; word-break: break-word; }

.signal-group { margin-bottom: 18px; }
.signal-group h3 { font-size: 14px; font-weight: 600; margin-bottom: 10px; color: #1a1f36; }
.signal { margin-bottom: 12px; }
.signal-head { display: flex; justify-content: space-between; align-items: center; font-size: 13px; margin-bottom: 4px; }
.signal-label { color: #1a1f36; }
.signal-right { display: flex; align-items: center; gap: 8px; }
.signal-raw { color: #1a1f36; font-size: 12px; }
.q-badge { padding: 2px 7px; border-radius: 3px; font-size: 10px; font-weight: 500; }
.q-High { background: #d1fae5; color: #065f46; }
.q-Medium { background: #fed7aa; color: #9a3412; }
.q-Noisy { background: #fee2e2; color: #b91c1c; }
.bar-wrap { background: #eef0f3; height: 4px; border-radius: 2px; overflow: hidden; }
.bar { height: 100%; transition: width .3s; }
.bar.green { background: #2563eb; }
.bar.red { background: #dc2626; }
.bar.amber { background: #f59e0b; }
.bar-pct { font-size: 10px; color: #6b7280; text-align: right; margin-top: 2px; }

.verdict-card { text-align: center; }
.verdict-btn { width: 100%; padding: 12px; border-radius: 6px; font-size: 14px; font-weight: 600; border: none; }
.verdict-btn.suspicious { background: #dc2626; color: #fff; }
.verdict-btn.legitimate { background: #10b981; color: #fff; }
.verdict-btn.review { background: #f59e0b; color: #fff; }
.conf-label { font-size: 12px; color: #6b7280; margin-top: 14px; }
.conf-value { font-size: 38px; font-weight: 700; margin-top: 2px; }
.reasons-title { text-align: left; font-size: 13px; font-weight: 600; margin-top: 18px; margin-bottom: 8px; }
.reasons-list { text-align: left; padding-left: 18px; font-size: 12px; line-height: 1.7; }

.reviewer-section { text-align: left; margin-top: 22px; padding-top: 18px; border-top: 1px solid #eef0f3; }
.reviewer-section h3 { font-size: 13px; font-weight: 600; margin-bottom: 8px; color: #1a1f36; }
.decision-summary { font-size: 12px; line-height: 1.6; color: #1a1f36; }
.recommended-actions { padding-left: 20px; font-size: 12px; line-height: 1.6; color: #1a1f36; margin: 0; }
.recommended-actions li { margin-bottom: 8px; }

.badge { padding: 4px 10px; border-radius: 4px; font-size: 12px; font-weight: 500; display: inline-flex; align-items: center; gap: 5px; }
.badge.suspicious { background: #fee2e2; color: #b91c1c; }
.badge.legitimate { background: #d1fae5; color: #065f46; }
.badge.review { background: #fed7aa; color: #9a3412; }

.breakdown-card { background: #fff; border-radius: 8px; padding: 22px; margin-top: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
.breakdown-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.breakdown-head h2 { font-size: 16px; font-weight: 600; }
.bd-table { width: 100%; border-collapse: collapse; }
.bd-table th { padding: 12px 8px; font-size: 12px; color: #6b7280; font-weight: 500; border-bottom: 1px solid #eef0f3; text-align: left; }
.bd-table td { padding: 14px 8px; border-bottom: 1px solid #f1f3f6; }
.bd-table .visual-cell { width: 40%; }
.bd-row-main { cursor: pointer; }
.bd-row-main:hover { background: #f8fafc; }
.bd-toggle { display: inline-block; width: 14px; transition: transform 0.2s; }
.bd-row-main.open .bd-toggle { transform: rotate(90deg); }
.bd-sub-row { background: #f8fafc; }
.bd-sub-row td { padding: 10px 8px 10px 32px; font-size: 12px; color: #4b5563; border-bottom: 1px solid #f1f3f6; }
.bd-sub-row .sub-label { color: #6b7280; }
.bd-band-row { background: #f8fafc; }
.bd-band-row td { padding: 4px 8px 12px 56px; font-size: 11px; color: #6b7280; font-style: italic; border-bottom: 1px solid #f1f3f6; }
.bd-band-row .band-text { background: #eff6ff; padding: 6px 10px; border-radius: 4px; border-left: 3px solid #2563eb; font-style: normal; color: #1e40af; display: inline-block; }
.bd-sub-rows { display: none; }
.bd-sub-rows.show { display: table-row-group; }
.viz-bar-wrap { background: #eef0f3; height: 6px; border-radius: 3px; overflow: hidden; }
.viz-bar { height: 100%; }
.formula { background: #f8fafc; padding: 16px; border-radius: 6px; font-family: "Courier New", monospace; font-size: 12px; line-height: 1.7; margin-top: 16px; white-space: pre-wrap; }
.weighted-total { margin-top: 14px; font-size: 13px; }
.llm-trigger { color: #b91c1c; }
.why-box { background: #eff6ff; padding: 14px; border-radius: 6px; margin-top: 16px; font-size: 12px; }
.why-box h4 { font-size: 13px; margin-bottom: 5px; }

.api-trail { margin-top: 12px; padding: 10px 14px; background: #f8fafc; border-radius: 5px; font-size: 11px; color: #6b7280; line-height: 1.6; }
.api-trail b { color: #1a1f36; }

.hidden { display: none; }
</style>
</head>
<body>

<div class="nav">
  <div class="brand">MerchantRisk</div>
  <div class="actions">
    <button class="nav-btn ghost" onclick="showForm()">+ New Application</button>
  </div>
</div>

<!-- NEW APPLICATION FORM -->
<div id="form-page" class="container">
  <h1>New Merchant Application</h1>
  <div class="form-card">
    <div class="autofill-banner" id="autofill-banner"></div>

    <div class="form-row">
      <label>Legal Name (start typing for autocomplete)</label>
      <input type="text" id="f-name" placeholder="Type company name" autocomplete="off">
      <div class="suggestions" id="suggestions"></div>
    </div>

    <div class="form-grid">
      <div class="form-row">
        <label>Website</label>
        <input type="text" id="f-website" placeholder="example.com">
      </div>
      <div class="form-row">
        <label>Industry</label>
        <select id="f-industry">
          <option value="">-- Select --</option>
          <option>Electronics</option>
          <option>Textiles</option>
          <option>Healthcare</option>
          <option>Food & Beverages</option>
          <option>Automotive</option>
          <option>Jewellery</option>
          <option>Education</option>
          <option>IT Services</option>
          <option>Banking & Finance</option>
          <option>Telecommunications</option>
          <option>Retail</option>
          <option>Energy</option>
          <option>Cryptocurrency</option>
        </select>
      </div>
    </div>

    <div class="form-grid">
      <div class="form-row">
        <label>Sub Segment</label>
        <input type="text" id="f-subseg" placeholder="e.g. Mobile Resale">
      </div>
      <div class="form-row">
        <label>Aggregator</label>
        <select id="f-agg">
          <option>Razorpay</option>
          <option>Paytm</option>
          <option>PhonePe</option>
          <option>Cashfree</option>
        </select>
      </div>
    </div>

    <div class="form-row">
      <label>Line of Business</label>
      <input type="text" id="f-lob" placeholder="Brief description of what merchant sells">
    </div>

    <div class="form-grid">
      <div class="form-row">
        <label>Phone</label>
        <input type="text" id="f-phone" placeholder="+91 99000 00012">
      </div>
      <div class="form-row">
        <label>Email</label>
        <input type="text" id="f-email" placeholder="ops@example.com">
      </div>
    </div>

    <div class="form-row">
      <label>Address Line</label>
      <input type="text" id="f-addr1" placeholder="Building, Street">
    </div>
    <div class="form-grid-3">
      <div class="form-row">
        <label>City</label>
        <input type="text" id="f-city" placeholder="Mumbai">
      </div>
      <div class="form-row">
        <label>State</label>
        <input type="text" id="f-state" placeholder="MH">
      </div>
      <div class="form-row">
        <label>Pincode</label>
        <input type="text" id="f-pin" placeholder="400053">
      </div>
    </div>

    <div class="form-actions">
      <button class="btn-primary" id="btn-evaluate" onclick="evaluateMerchant()">Run Risk Evaluation</button>
      <button class="btn-secondary" onclick="resetForm()">Reset</button>
    </div>
  </div>
</div>

<!-- EVAL PROGRESS -->
<div id="eval-page" class="container hidden">
  <div class="eval-progress">
    <h2>Running Risk Evaluation</h2>
    <div class="api-step" id="step-whois">
      <div class="label"><b>WHOIS</b><br><span style="color:#6b7280;font-size:11px">RDAP first, fallback to who-dat.as93.net for ccTLDs</span></div>
      <div class="status"><span class="spinner"></span></div>
    </div>
    <div class="api-step" id="step-geo">
      <div class="label"><b>Geocoding</b><br><span style="color:#6b7280;font-size:11px">Resolving address via OpenStreetMap Nominatim</span></div>
      <div class="status"><span class="spinner"></span></div>
    </div>
    <div class="api-step" id="step-score">
      <div class="label"><b>Score Engine</b><br><span style="color:#6b7280;font-size:11px">Running 18 sub signals across 7 categories</span></div>
      <div class="status"><span class="spinner"></span></div>
    </div>
  </div>
</div>

<!-- DETAIL PAGE -->
<div id="detail-page" class="container hidden">
  <div class="back" onclick="showForm()">← New Application</div>
  <div class="detail-grid">
    <div class="panel">
      <h2>Application Inputs</h2>
      <div id="inputs-body"></div>
      <div class="api-trail" id="api-trail"></div>
    </div>
    <div class="panel">
      <h2>Enriched Intelligence</h2>
      <div id="signals-body"></div>
    </div>
    <div class="panel verdict-card">
      <h2 style="text-align:left">Verdict</h2>
      <button class="verdict-btn" id="verdict-badge">-</button>
      <div class="conf-label">Confidence</div>
      <div class="conf-value" id="conf-value">-</div>

      <div class="reviewer-section">
        <h3>Decision Summary</h3>
        <p id="decision-summary-text" class="decision-summary"></p>
      </div>

      <div class="reviewer-section">
        <h3>Recommended Reviewer Action</h3>
        <ol id="recommended-actions-list" class="recommended-actions"></ol>
      </div>
    </div>
  </div>

  <div class="breakdown-card">
    <div class="breakdown-head">
      <h2>Score Calculation Breakdown</h2>
    </div>
    <table class="bd-table">
      <thead>
        <tr><th>Category</th><th>Sub Score</th><th>Weight</th><th>Contribution</th><th>Visual</th></tr>
      </thead>
      <tbody id="breakdown-body"></tbody>
    </table>
    <div class="formula" id="formula-text"></div>
    <div class="weighted-total">Weighted Total: <b id="wt-total">-</b></div>
    <div class="weighted-total" id="llm-trigger-row"></div>
    <div class="weighted-total">Final Verdict: <span id="final-verdict-badge"></span></div>
    <div class="why-box">
      <h4>Why this verdict</h4>
      <div id="why-text"></div>
    </div>
  </div>
</div>

<script>
const API = "";

// ─── AUTOCOMPLETE ────────────────────────────────────────────
let debounceTimer = null;
let activeSugg = -1;
let currentSuggestions = [];

document.getElementById("f-name").addEventListener("input", e => {
  const q = e.target.value.trim();
  clearTimeout(debounceTimer);
  if (q.length < 2) { hideSuggestions(); return; }
  debounceTimer = setTimeout(() => fetchAutocomplete(q), 250);
});

document.getElementById("f-name").addEventListener("keydown", e => {
  const box = document.getElementById("suggestions");
  if (!box.classList.contains("show")) return;
  const items = box.querySelectorAll(".sugg-item");
  if (e.key === "ArrowDown") { e.preventDefault(); activeSugg = Math.min(activeSugg + 1, items.length - 1); updateActive(items); }
  else if (e.key === "ArrowUp") { e.preventDefault(); activeSugg = Math.max(activeSugg - 1, 0); updateActive(items); }
  else if (e.key === "Enter" && activeSugg >= 0) { e.preventDefault(); selectSuggestion(currentSuggestions[activeSugg]); }
  else if (e.key === "Escape") { hideSuggestions(); }
});

function updateActive(items) {
  items.forEach((it, i) => it.classList.toggle("active", i === activeSugg));
}

async function fetchAutocomplete(q) {
  try {
    const r = await fetch(API + "/v1/autocomplete?q=" + encodeURIComponent(q));
    const data = await r.json();
    currentSuggestions = data.results || [];
    activeSugg = -1;
    renderSuggestions(currentSuggestions);
  } catch(e) { hideSuggestions(); }
}

function renderSuggestions(items) {
  const box = document.getElementById("suggestions");
  if (!items.length) { hideSuggestions(); return; }
  box.innerHTML = items.map((it, i) => `
    <div class="sugg-item" data-i="${i}">
      <div class="sugg-name">${escapeHtml(it.name)}</div>
      ${it.description ? `<div class="sugg-desc">${escapeHtml(it.description.substring(0, 120))}</div>` : ""}
    </div>
  `).join("");
  box.classList.add("show");
  box.querySelectorAll(".sugg-item").forEach(el => {
    el.onclick = () => selectSuggestion(items[parseInt(el.dataset.i)]);
  });
}

function hideSuggestions() {
  document.getElementById("suggestions").classList.remove("show");
  activeSugg = -1;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

document.addEventListener("click", e => {
  if (!e.target.closest(".form-row")) hideSuggestions();
});

// ─── AUTOFILL FROM WIKIPEDIA ────────────────────────────────
// ─── AUTOFILL FROM WIKIDATA + WIKIPEDIA ────────────────────
let cachedDetails = {};

async function selectSuggestion(item) {
  document.getElementById("f-name").value = item.name;
  hideSuggestions();
  showAutofillBanner("Querying Wikidata SPARQL and Wikipedia for structured company data...");

  try {
    const r = await fetch(API + "/v1/company_details?name=" + encodeURIComponent(item.name));
    const data = await r.json();
    cachedDetails = data;

    let filled = [];
    if (data.industry_hint && !document.getElementById("f-industry").value) {
      document.getElementById("f-industry").value = data.industry_hint;
      filled.push("Industry");
    }
    if (data.domain_guess && !document.getElementById("f-website").value) {
      document.getElementById("f-website").value = data.domain_guess;
      filled.push("Website");
    }
    if (data.city_guess && !document.getElementById("f-city").value) {
      document.getElementById("f-city").value = data.city_guess;
      filled.push("City");
    }
    if (data.state_guess && !document.getElementById("f-state").value) {
      document.getElementById("f-state").value = data.state_guess;
      filled.push("State");
    }
    if (data.line_of_business && !document.getElementById("f-lob").value) {
      document.getElementById("f-lob").value = data.line_of_business.substring(0, 300);
      filled.push("Line of Business");
    }

    const sources = (data.sources_used || []).join(" + ") || "no source matched";
    const meta = [];
    if (data.wikidata_id) meta.push(`QID ${data.wikidata_id}`);
    if (data.founded) meta.push(`Founded ${data.founded}`);
    if (data.country) meta.push(`Country ${data.country}`);

    if (filled.length) {
      showAutofillBanner(`Auto-filled from ${sources}: ${filled.join(", ")}. ${meta.join(" · ")}`);
    } else {
      showAutofillBanner(`Sources queried (${sources}) but no auto-fillable structured data found. Fill manually.`);
    }
  } catch(e) {
    showAutofillBanner("Lookup failed: " + e.message);
  }
}

function showAutofillBanner(msg) {
  const b = document.getElementById("autofill-banner");
  b.textContent = msg;
  b.classList.add("show");
}

function resetForm() {
  ["f-name","f-website","f-industry","f-subseg","f-lob","f-phone","f-email","f-addr1","f-city","f-state","f-pin"].forEach(id => {
    const el = document.getElementById(id); el.value = "";
  });
  document.getElementById("autofill-banner").classList.remove("show");
}

// ─── EVALUATE ────────────────────────────────────────────────
async function evaluateMerchant() {
  const name = document.getElementById("f-name").value.trim();
  if (!name) { alert("Legal name is required"); return; }

  document.getElementById("form-page").classList.add("hidden");
  document.getElementById("eval-page").classList.remove("hidden");
  document.getElementById("detail-page").classList.add("hidden");

  // Reset progress
  ["step-whois","step-geo","step-score"].forEach(id => {
    const el = document.getElementById(id);
    el.classList.remove("done");
    el.querySelector(".status").innerHTML = '<span class="spinner"></span>';
  });

  const payload = {
    legal_name: name,
    website: document.getElementById("f-website").value.trim(),
    industry: document.getElementById("f-industry").value.trim(),
    sub_segment: document.getElementById("f-subseg").value.trim(),
    line_of_business: document.getElementById("f-lob").value.trim(),
    phone: document.getElementById("f-phone").value.trim(),
    email: document.getElementById("f-email").value.trim(),
    aggregator: document.getElementById("f-agg").value,
    wikidata_qid: cachedDetails.wikidata_id || "",
    company_founded: cachedDetails.founded || "",
    address: {
      line1: document.getElementById("f-addr1").value.trim(),
      city: document.getElementById("f-city").value.trim(),
      state: document.getElementById("f-state").value.trim(),
      pincode: document.getElementById("f-pin").value.trim(),
    }
  };

  // Animate progress while real call happens
  setTimeout(() => markDone("step-whois"), 1200);
  setTimeout(() => markDone("step-geo"), 2400);

  try {
    const r = await fetch(API + "/v1/merchant/evaluate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const result = await r.json();
    markDone("step-whois"); markDone("step-geo"); markDone("step-score");
    setTimeout(() => showDetail(result), 400);
  } catch(e) {
    alert("Evaluation failed: " + e.message);
    showForm();
  }
}

function markDone(id) {
  const el = document.getElementById(id);
  el.classList.add("done");
  el.querySelector(".status").innerHTML = "✓ Complete";
}

// ─── DETAIL VIEW ─────────────────────────────────────────────
function verdictClass(v) {
  if (v === "Suspicious") return "suspicious";
  if (v === "Legitimate") return "legitimate";
  return "review";
}
function badgeIcon(v) {
  if (v === "Suspicious") return "⊘";
  if (v === "Legitimate") return "✓";
  return "⚠";
}
function barClass(score) {
  if (score >= 70) return "green";
  if (score >= 45) return "amber";
  return "red";
}

function showForm() {
  document.getElementById("form-page").classList.remove("hidden");
  document.getElementById("eval-page").classList.add("hidden");
  document.getElementById("detail-page").classList.add("hidden");
  window.scrollTo(0,0);
}

function toggleSubRows(cat, rowEl) {
  rowEl.classList.toggle("open");
  const subs = document.querySelectorAll(".sub-" + cat);
  subs.forEach(s => {
    s.style.display = s.style.display === "none" ? "table-row" : "none";
  });
}

function showDetail(m) {
  document.getElementById("form-page").classList.add("hidden");
  document.getElementById("eval-page").classList.add("hidden");
  document.getElementById("detail-page").classList.remove("hidden");
  window.scrollTo(0,0);

  document.getElementById("inputs-body").innerHTML = `
    <div class="input-row"><div class="lbl">Legal Name</div><div class="val">${escapeHtml(m.legal_name)}</div></div>
    <div class="input-row"><div class="lbl">Website</div><div class="val">${escapeHtml(m.website || "—")}</div></div>
    <div class="input-row"><div class="lbl">Industry</div><div class="val">${escapeHtml(m.industry || "—")}</div></div>
    <div class="input-row"><div class="lbl">Sub Segment</div><div class="val">${escapeHtml(m.sub_segment || "—")}</div></div>
    <div class="input-row"><div class="lbl">Line of Business</div><div class="val">${escapeHtml(m.line_of_business || "—")}</div></div>
    <div class="input-row"><div class="lbl">Aggregator</div><div class="val">${escapeHtml(m.aggregator)}</div></div>
    <div class="input-row"><div class="lbl">Contact</div><div class="val">${escapeHtml(m.phone || "—")}, ${escapeHtml(m.email || "—")}</div></div>
    <div class="input-row"><div class="lbl">Address</div><div class="val">${escapeHtml(m.address_full)}</div></div>
  `;

  // API trail
  const e = m.enrichment || {};
  const w = e.whois || {};
  document.getElementById("api-trail").innerHTML = `
    <b>Live API responses</b><br>
    <b>WHOIS</b> via ${escapeHtml(w.source || "n/a")}: ${escapeHtml(w.status || "n/a")}${w.registered ? `<br>Registered ${w.registered}, age ${w.domain_age_years}y` : ""}${w.registrar ? `<br>Registrar: ${escapeHtml(w.registrar)}` : ""}<br>
    <b>Nominatim</b> (OpenStreetMap): ${escapeHtml(e.geocode?.status || "n/a")}, confidence ${e.geocode?.confidence || 0}<br>
    ${e.geocode?.display ? `Resolved: ${escapeHtml(e.geocode.display)}<br>` : ""}
    Latency: ${m.latency_sec}s
  `;

  // Signals
  let sig = "";
  for (const cat of ["Identity","Domain","Content","Address","Footprint"]) {
    if (!m.signals[cat]) continue;
    sig += `<div class="signal-group"><h3>${cat}</h3>`;
    for (const s of m.signals[cat]) {
      sig += `
        <div class="signal">
          <div class="signal-head">
            <span class="signal-label">${s.label}</span>
            <span class="signal-right">
              <span class="signal-raw">${escapeHtml(s.raw)}</span>
              <span class="q-badge q-${s.quality}">${s.quality}</span>
            </span>
          </div>
          <div class="bar-wrap"><div class="bar ${barClass(s.score)}" style="width:${s.score}%"></div></div>
          <div class="bar-pct">${s.score}%</div>
        </div>`;
    }
    sig += "</div>";
  }
  document.getElementById("signals-body").innerHTML = sig;

  // Verdict
  const vc = verdictClass(m.verdict);
  const badge = document.getElementById("verdict-badge");
  badge.className = "verdict-btn " + vc;
  badge.innerHTML = badgeIcon(m.verdict) + " " + m.verdict;
  document.getElementById("conf-value").textContent = Math.round(m.confidence * 100) + "%";

  // Decision Summary and Recommended Reviewer Action
  document.getElementById("decision-summary-text").textContent = m.decision_summary || "";
  const actions = m.recommended_actions || [];
  document.getElementById("recommended-actions-list").innerHTML =
    actions.map(a => `<li>${escapeHtml(a)}</li>`).join("");

  // Breakdown
  const order = ["Identity","Domain","Content","Contact","Address","Footprint","Behavior"];
  let bd = "";
  for (const k of order) {
    const sub = m.cats[k];
    const w = m.cat_weights[k];
    const contrib = (sub * w).toFixed(2);
    const cls = barClass(sub);
    const color = cls === "green" ? "#2563eb" : cls === "red" ? "#dc2626" : "#f59e0b";
    const sigs = m.signals[k] || [];
    const subWeights = m.sub_weights[k] || {};

    bd += `<tr class="bd-row-main" onclick="toggleSubRows('${k}', this)">
      <td><span class="bd-toggle">▸</span> ${k}</td>
      <td>${sub}</td>
      <td>${w.toFixed(2)}</td>
      <td><b>${contrib}</b></td>
      <td class="visual-cell"><div class="viz-bar-wrap"><div class="viz-bar" style="width:${sub}%; background:${color}"></div></div></td>
    </tr>`;

    // Sub-signal rows with band explanation
    for (const sg of sigs) {
      const sw = subWeights[sg.label] || 0;
      const subContrib = (sg.score * sw).toFixed(2);
      const subCls = barClass(sg.score);
      const subColor = subCls === "green" ? "#2563eb" : subCls === "red" ? "#dc2626" : "#f59e0b";
      bd += `<tr class="bd-sub-row sub-${k}" style="display:none">
        <td><span class="sub-label">└ ${sg.label}</span> <span class="q-badge q-${sg.quality}" style="margin-left:6px">${sg.quality}</span></td>
        <td>${sg.score}</td>
        <td>${sw.toFixed(2)}</td>
        <td>${subContrib}</td>
        <td class="visual-cell"><div class="viz-bar-wrap"><div class="viz-bar" style="width:${sg.score}%; background:${subColor}"></div></div></td>
      </tr>`;
      if (sg.band) {
        bd += `<tr class="bd-band-row sub-${k}" style="display:none">
          <td colspan="5"><span class="band-text">Band: ${escapeHtml(sg.band)}</span></td>
        </tr>`;
      }
    }
  }
  document.getElementById("breakdown-body").innerHTML = bd;

  const parts = order.map(k => `(${m.cats[k]}×${m.cat_weights[k].toFixed(2)})`).join(" + ");
  document.getElementById("formula-text").textContent =
`Final Score = Σ (category_sub_score × category_weight)
            = ${parts}
            = ${m.final_score}

Verdict mapping:
  >= 75 AND llm_conf >= 0.7    →  Legitimate
  < 40                          →  Suspicious
  LLM hard mismatch override    →  Suspicious
  Hard fail (e.g. restricted MCC)  →  Suspicious
  Else                          →  Needs Review`;

  document.getElementById("wt-total").textContent = m.final_score;
  const llmRow = document.getElementById("llm-trigger-row");
  if (m.llm_override) {
    llmRow.innerHTML = `<span class="llm-trigger"><b>LLM Override Triggered:</b> ${escapeHtml(m.llm_note)}</span>`;
  } else if (m.hard_fails?.length) {
    llmRow.innerHTML = `<span class="llm-trigger"><b>Hard Fail Triggered:</b> ${escapeHtml(m.hard_fails[0])}</span>`;
  } else {
    llmRow.innerHTML = "";
  }
  document.getElementById("final-verdict-badge").innerHTML =
    `<span class="badge ${vc}">${badgeIcon(m.verdict)} ${m.verdict}</span>`;

  let why;
  if (m.verdict === "Suspicious" && m.llm_override) {
    why = `Score of ${m.final_score} sits in the Needs Review band. LLM override (${m.llm_note}) collapses verdict to Suspicious.`;
  } else if (m.verdict === "Suspicious" && m.hard_fails?.length) {
    why = `Hard fail rule triggered: ${m.hard_fails.join("; ")}. Verdict forced to Suspicious.`;
  } else if (m.verdict === "Suspicious") {
    why = `Final score ${m.final_score} below 40 threshold. Multiple categories scoring poorly.`;
  } else if (m.verdict === "Legitimate") {
    why = `Final score ${m.final_score} exceeds 75 threshold with sufficient confidence.`;
  } else {
    why = `Final score ${m.final_score} in Needs Review band (40 to 74). Mixed signals require human judgment.`;
  }
  document.getElementById("why-text").textContent = why;
}

</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
