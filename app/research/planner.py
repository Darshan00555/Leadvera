"""Builds each run's research assignments, rotating through least-recently-covered combinations."""
import itertools
import zlib
from datetime import date, timedelta

from sqlalchemy import func

from .. import jurisdictions as J
from ..fields import QUEUE_STATUSES
from ..models import Coverage, Lead

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Query patterns from Section 3A of the brief, parameterised.
PATTERNS = {
    "legal-pages": ['"{industry}" India "Private Limited" "{state} LLC" "Terms of Service"',
                    '"Pvt. Ltd." "{state} limited liability company" {industry} "Privacy Policy"'],
    "founder": ['"Indian founder" {industry} "{state} LLC" "Pvt Ltd"',
                '{industry} founder India "Private Limited" "LLC" "{state}" interview'],
    "subsidiary": ['"India Pvt Ltd" "United States subsidiary" LLC {industry}',
                   '"Private Limited" "{state} LLC" subsidiary {industry} {indian_state}'],
    "linkedin": ['site:linkedin.com/company {industry} "Pvt Ltd" "LLC" "{state}"',
                 'site:in.linkedin.com/company "{industry}" "LLC" "United States"'],
    "contact": ['"{industry}" "{indian_state}" "{state}" "LLC" "Contact Us" "Private Limited"',
                '"Indian company" "{industry}" "LLC" "United States address" "{state}"'],
    "expansion": ['"{industry}" Indian startup "US entity" "{state} LLC"',
                  '"Indian {industry} company" "US LLC" expansion'],
}

# The same patterns for a company registered outside the U.S. ({country}, {form} = a local company form).
INTL_PATTERNS = {
    "legal-pages": ['"{industry}" India "Private Limited" "{form}" "{country}" "Terms"',
                    '"Pvt Ltd" "{country}" {industry} "Privacy Policy" "{indian_state}"'],
    "founder": ['{industry} founder "{indian_state}" "{country}" office "Pvt Ltd"',
                '"Indian founder" {industry} "{country}" "{form}" "Private Limited"'],
    "subsidiary": ['"{indian_state}" "Private Limited" "{country}" subsidiary {industry}',
                   '"wholly owned subsidiary" "Pvt Ltd" "{form}" {industry} {country}'],
    "linkedin": ['site:linkedin.com/company {industry} "Pvt Ltd" "{country}" "{indian_state}"',
                 'site:in.linkedin.com/company "{industry}" "{form}" "{country}"'],
    "contact": ['"{industry}" "{indian_state}" "{country}" "Contact Us" "Private Limited"',
                '"Indian company" "{industry}" "{country} office" "{form}"'],
    "expansion": ['"Indian {industry} company" expands to "{country}" "{indian_state}"',
                  '"{indian_state}" startup "{country} entity" {industry}'],
}


def _cycle_key(industry, us_state, pattern):
    return f"{industry}|{us_state}|{pattern}"


def clean_targets(markets=(), us_states=(), locations=(), industries=(), assignments=None, revisit=True,
                  max_leads=None):
    """Validate a run's custom focus. Only what was actually chosen is kept; {} means 'use saved defaults'."""
    from ..rules import US_STATES
    t = {}
    mk = []
    for v in markets or ():
        v = str(v).strip().upper()
        code = v if v in J.GROUPS else J.norm_country(v)
        if code and code not in mk:
            mk.append(code)
    if mk:
        t["markets"] = mk
    sts = [s for s in dict.fromkeys(str(x).strip().upper() for x in us_states or ()) if s in US_STATES]
    if sts:
        t["us_states"] = sts
    locs = [x for x in dict.fromkeys(" ".join(str(v).split())[:60] for v in locations or ()) if x][:20]
    if locs:
        t["indian_locations"] = locs
    inds = [x for x in dict.fromkeys(" ".join(str(v).split())[:60] for v in industries or ()) if x][:30]
    if inds:
        t["industries"] = inds
    if assignments:
        t["assignments"] = max(1, min(40, int(assignments)))
    if not revisit:
        t["revisit"] = False
    if max_leads:
        t["max_leads"] = max(1, min(500, int(max_leads)))
    return t


def describe_targets(t):
    parts = [", ".join(J.name(m) if m not in J.GROUPS else m for m in t.get("markets", []))]
    if t.get("us_states"):
        parts.append("US states " + ", ".join(t["us_states"]))
    parts += [", ".join(t.get("indian_locations", [])), ", ".join(t.get("industries", []))]
    if t.get("assignments"):
        parts.append(f"{t['assignments']} assignments")
    if t.get("max_leads"):
        parts.append(f"stop at {t['max_leads']} leads")
    if t.get("revisit") is False:
        parts.append("no queue revisits")
    return " · ".join(p for p in parts if p)


def places(settings, targets=None):
    """(country, US state) pairs to research: each chosen U.S. state, plus one entry per other country."""
    t = targets or {}
    markets = J.expand_markets(t.get("markets") or settings.get("target_markets") or ["US"]) or ["US"]
    us_states = t.get("us_states") or settings["us_states"] or ["WY", "DE", "FL"]
    out = []
    for m in markets:
        out += [("US", st) for st in us_states] if m == "US" else [(m, "")]
    return out


def plan(db, settings, max_groups, targets=None):
    """targets (optional, from the run launcher) narrows the run: markets, us_states, indian_locations, industries."""
    t = targets or {}
    industries = t.get("industries") or settings["industries"] or ["software"]
    indian_states = t.get("indian_locations") or settings["indian_states"] or ["Karnataka"]
    strict_location = bool(t.get("indian_locations"))

    last = dict(db.query(Coverage.query_group, func.max(Coverage.date)).group_by(Coverage.query_group).all())
    yields = dict(db.query(Coverage.query_group, func.sum(Coverage.verified_yielded))
                  .group_by(Coverage.query_group).all())

    # Non-U.S. places are keyed "@GB" etc. so Germany ("DE") never collides with Delaware.
    combos = [(ind, st if m == "US" else "@" + m, pat)
              for ind, (m, st), pat in itertools.product(industries, places(settings, t), PATTERNS)]
    never = date(1970, 1, 1)
    # Least recently covered first; among equals, prefer combos that have yielded verified leads before.
    combos.sort(key=lambda c: (last.get(_cycle_key(*c), never), -(yields.get(_cycle_key(*c)) or 0)))

    groups = []
    for i, (ind, st, pat) in enumerate(combos[:max_groups]):
        ist = indian_states[(zlib.crc32(ind.encode()) + i) % len(indian_states)]
        if st.startswith("@"):
            m = st[1:]
            fmt = dict(industry=ind, country=J.name(m), form=J.forms(m)[0], indian_state=ist)
            queries = [q.format(**fmt) for q in INTL_PATTERNS[pat]]
            group = {"market": m, "market_name": J.name(m), "forms": J.forms(m), "registry": J.registry(m),
                     "us_state": "", "us_state_name": ""}
        else:
            fmt = dict(industry=ind, state=STATE_NAMES.get(st, st), indian_state=ist)
            queries = [q.format(**fmt) for q in PATTERNS[pat]]
            group = {"market": "US", "market_name": J.name("US"), "us_state": st, "us_state_name": STATE_NAMES.get(st, st)}
        if strict_location:
            queries = [q if ist in q else f'{q} "{ist}"' for q in queries]
        groups.append({"kind": "discover", "id": _cycle_key(ind, st, pat), "industry": ind, "indian_state": ist,
                       "strict_location": strict_location, "pattern": pat, "queries": queries, **group})
    return groups


def revisit_groups(db, limit, per_group=10):
    """Queue items not re-checked in the last 3 days, oldest first."""
    cutoff = date.today() - timedelta(days=3)
    rows = (db.query(Lead).filter(Lead.verification_status.in_(QUEUE_STATUSES))
            .filter((Lead.date_last_checked == None) | (Lead.date_last_checked <= cutoff))  # noqa: E711
            .order_by(Lead.date_last_checked.asc()).limit(limit).all())
    items = [{"lead_id": r.lead_id, "indian_legal_name": r.indian_legal_name, "indian_brand_name": r.indian_brand_name,
              "us_llc_legal_name": r.us_llc_legal_name, "us_state": r.us_state, "foreign_country": r.foreign_country,
              "missing_evidence": r.missing_evidence, "indian_website": r.indian_website} for r in rows]
    return [{"kind": "revisit", "id": f"revisit|{i}", "industry": "", "us_state": "", "indian_state": "",
             "pattern": "revisit", "items": items[i:i + per_group]} for i in range(0, len(items), per_group)]
