"""Verification gates, scoring and normalisation (Sections 4 and 6 of the master brief).

The research agent proposes a status; these rules decide the final one. A record can only be
downgraded here, never upgraded, so an over-eager agent cannot inflate the verified count.
"""
import re
from urllib.parse import urlparse

from . import jurisdictions as J

CIN_RE = re.compile(r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$")
LLPIN_RE = re.compile(r"^[A-Z]{3}-\d{4}$")
LLC_SUFFIX_RE = re.compile(r"\b(L\.?\s?L\.?\s?C\.?|LIMITED LIABILITY COMPANY)\s*$", re.I)
CORP_SUFFIX_RE = re.compile(r"\b(INC\.?|INCORPORATED|CORP\.?|CORPORATION)\s*$", re.I)

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}

# Hosts accepted as authoritative registration evidence.
OFFICIAL_IN_HOSTS = ("mca.gov.in", "data.gov.in")
OFFICIAL_US_SUFFIXES = (".gov", ".us")
OFFICIAL_US_HOSTS = ("sunbiz.org", "sosbiz.idaho.gov", "corp.delaware.gov")

LEGAL_NOISE = re.compile(
    r"\b(private|pvt|limited|ltd|llp|llc|l\.l\.c|inc|corp|corporation|company|co|opc|india|the)\b\.?", re.I)


def norm_name(name):
    if not name:
        return ""
    s = LEGAL_NOISE.sub(" ", str(name).lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def domain_of(url):
    if not url:
        return ""
    url = str(url).strip()
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def split_urls(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = re.split(r"[\s,;|]+", str(value))
    return [str(u).strip() for u in items if str(u).strip().startswith(("http://", "https://"))]


def is_official_in(url):
    d = domain_of(url)
    return any(d == h or d.endswith("." + h) for h in OFFICIAL_IN_HOSTS)


def is_official_us(url):
    d = domain_of(url)
    return bool(d) and (d.endswith(OFFICIAL_US_SUFFIXES) or any(d == h or d.endswith("." + h)
                                                                for h in OFFICIAL_US_HOSTS))


def country_of(rec):
    """Jurisdiction of the overseas entity: blank means the original U.S. LLC case; unknown input stays as given."""
    raw = (rec.get("foreign_country") or "").strip()
    if not raw:
        return "US"
    return J.norm_country(raw) or raw.upper()


def is_official_foreign(url, country):
    return is_official_us(url) if country == "US" else J.is_official(country, domain_of(url))


def valid_indian_id(value):
    v = (value or "").strip().upper()
    return bool(CIN_RE.match(v) or LLPIN_RE.match(v))


def entity_pair_key(rec):
    ind = (rec.get("cin_llpin") or "").strip().upper() or "IN:" + norm_name(rec.get("indian_legal_name"))
    country = country_of(rec)
    st = (rec.get("us_state") or "").strip().upper() if country == "US" else "@" + country
    num = re.sub(r"\s+", "", (rec.get("us_entity_number") or "").strip().upper())
    us = f"{st}:{num}" if num else f"{st}:" + norm_name(rec.get("us_llc_legal_name"))
    return f"{ind}|{us}"


def gate(rec):
    """Return (final_status, problems, flags)."""
    proposed = (rec.get("verification_status") or "UNVERIFIED").strip().upper()
    if proposed == "DISQUALIFIED":
        return "DISQUALIFIED", [rec.get("research_limitations") or "Marked disqualified by researcher"], {}

    problems, disq = [], []
    us_name = (rec.get("us_llc_legal_name") or "").strip()
    country = country_of(rec)
    if country == "US" and us_name and CORP_SUFFIX_RE.search(us_name) and not LLC_SUFFIX_RE.search(us_name):
        disq.append("US entity is a corporation, not an LLC")

    indian_ok = valid_indian_id(rec.get("cin_llpin")) and bool(rec.get("indian_legal_name"))
    if not valid_indian_id(rec.get("cin_llpin")):
        problems.append("Missing or malformed CIN/LLPIN")
    if not rec.get("indian_registry_url"):
        problems.append("No Indian registry record URL")
        indian_ok = False
    elif not is_official_in(rec["indian_registry_url"]):
        problems.append("Indian registration not evidenced by an MCA / data.gov.in record")
        indian_ok = False

    lim = rec.get("us_verification_limitation")
    if country == "US":
        state = (rec.get("us_state") or "").upper()
        us_ok = bool(us_name) and bool(LLC_SUFFIX_RE.search(us_name)) and state in US_STATES
        if not us_name:
            problems.append("No US LLC name")
        elif not LLC_SUFFIX_RE.search(us_name):
            problems.append("US entity name has no LLC designator")
        if state not in US_STATES:
            problems.append("Missing/invalid US state")
        if not rec.get("us_registry_url") or not is_official_us(rec.get("us_registry_url")):
            problems.append("US LLC not confirmed on an official state registry" + (f" ({lim})" if lim else ""))
            us_ok = False
        other = "the US LLC"
    elif J.name(country) == country:
        us_ok, other = False, f"the {country} entity"
        problems.append(f"Unsupported foreign jurisdiction: {country}")
    else:
        cname = J.name(country)
        us_ok, other = bool(us_name) and J.has_form(country, us_name), f"the {cname} entity"
        if not us_name:
            problems.append(f"No {cname} entity name")
        elif not us_ok:
            problems.append(f"{cname} entity name has no company-form designator ({', '.join(J.forms(country)[:4])})")
        if not rec.get("us_registry_url") or not is_official_foreign(rec.get("us_registry_url"), country):
            problems.append(f"{cname} entity not confirmed on the official company registry ({J.registry(country)})"
                            + (f" ({lim})" if lim else ""))
            us_ok = False

    rel_ok = bool(split_urls(rec.get("relationship_evidence_urls"))) and bool(rec.get("relationship_evidence"))
    if not rel_ok:
        problems.append(f"No documented evidence linking the Indian entity and {other}")

    flags = {"indian": indian_ok, "us": us_ok, "relationship": rel_ok}
    if disq:
        return "DISQUALIFIED", disq + problems, flags
    if indian_ok and us_ok and rel_ok and proposed == "VERIFIED":
        return "VERIFIED", problems, flags
    if indian_ok or us_ok:
        return "PARTIALLY VERIFIED", problems, flags
    return "UNVERIFIED", problems, flags


def web_evidence(rec):
    """Second, lower tier: every fact is cited from public web pages, but not yet confirmed on official
    registries. Never changes verification_status - gate() alone decides VERIFIED. Returns (ok, missing)."""
    missing = []
    if not (valid_indian_id(rec.get("cin_llpin")) and rec.get("indian_legal_name")):
        missing.append("Indian legal name with a valid CIN/LLPIN")
    if not (rec.get("indian_registry_url") or split_urls(rec.get("source_urls"))):
        missing.append("a cited page for the Indian company")
    name, country = (rec.get("us_llc_legal_name") or "").strip(), country_of(rec)
    if country == "US":
        named = bool(LLC_SUFFIX_RE.search(name)) and (rec.get("us_state") or "").upper() in US_STATES
    else:
        named = J.has_form(country, name)
    if not named:
        missing.append("overseas entity's exact legal name" + (" and US state" if country == "US" else ""))
    if not (split_urls(rec.get("relationship_evidence_urls")) and rec.get("relationship_evidence")):
        missing.append("a cited page linking the two entities")
    return not missing, missing


BOOKKEEPING_SIGNAL_WORDS = ("subscription", "saas", "recurring", "multiple entities", "us customer",
                            "us operations", "us clients", "hiring", "headcount", "expansion", "funding",
                            "cross-border", "stripe", "us sales", "marketplace")


def score(rec, status):
    """Section 6 scoring. Points come only from fields that carry evidence."""
    pts, parts = 0, []
    if status == "VERIFIED":
        pts += 30; parts.append("both entities verified +30")
    if rec.get("relationship_evidence") and split_urls(rec.get("relationship_evidence_urls")):
        pts += 20; parts.append("relationship documented +20")
    if rec.get("indian_website") and rec.get("recent_activity_evidence"):
        pts += 15; parts.append("active operations +15")
    if rec.get("decision_maker_name") or rec.get("founder_ceo_name"):
        pts += 15; parts.append("decision-maker identified +15")
    if (rec.get("business_email") and rec.get("email_source_url")) or rec.get("business_phone") \
            or rec.get("linkedin_decision_maker_url"):
        pts += 10; parts.append("business contact channel +10")
    sig = (rec.get("bookkeeping_need_signals") or "").lower()
    if sig and any(w in sig for w in BOOKKEEPING_SIGNAL_WORDS):
        pts += 10; parts.append("recurring bookkeeping signal +10")
    return pts, parts


def priority(points):
    return "A" if points >= 80 else "B" if points >= 60 else "C"


def confidence(rec, status):
    """0-100: strength of the evidence trail (separate from the commercial lead score)."""
    c = 30 if is_official_in(rec.get("indian_registry_url") or "") else 0
    c += 30 if is_official_foreign(rec.get("us_registry_url") or "", country_of(rec)) else 0
    c += min(25, 10 * len(split_urls(rec.get("relationship_evidence_urls"))))
    c += min(15, 5 * len(split_urls(rec.get("source_urls"))))
    return c if status == "VERIFIED" else min(c, 69)


def next_research_step(problems):
    p = " ".join(problems).lower()
    if "cin" in p or "indian registr" in p:
        return "Confirm exact legal name and CIN/LLPIN on MCA master data / data.gov.in"
    if "official state registry" in p:
        return "Find the LLC's detail page on the state Secretary of State registry"
    if "official company registry" in p:
        return "Find the entity's record on its country's official company registry"
    if "linking" in p:
        return "Find a page (Terms, Privacy, filing, press release) naming both entities"
    return "Manual review"
