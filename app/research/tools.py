"""Client-side tools the research agent can call. Server tools (web_search / web_fetch) run at Anthropic."""
import json
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .. import rules as R
from ..config import Config
from ..fields import RESEARCH_FIELDS, STATUSES
from ..ingest import ingest_candidate, known_lookup
from ..models import IndianCompany

WEB_SEARCH_TYPE = "web_search_20260209"
WEB_FETCH_TYPE = "web_fetch_20260209"

_nullable = {"type": ["string", "null"]}
# submit_candidate is deliberately non-strict: ~45 optional fields, and the engine (ingest.clean + rules.gate)
# validates and normalises every submission in code anyway.
_candidate_props = {k: {"type": "string"} for k in RESEARCH_FIELDS}
_candidate_props["verification_status"] = {"type": "string", "enum": list(STATUSES)}
_candidate_props["relationship_evidence_urls"] = {"type": "array", "items": {"type": "string"}}
_candidate_props["source_urls"] = {"type": "array", "items": {"type": "string"}}
_candidate_props["us_state"] = {"type": "string", "description": "two-letter US state code (U.S. entities only)"}
_candidate_props["foreign_country"] = {"type": "string", "description": "country of the overseas entity: US, GB, AU, "
                                                                        "NZ or an EU/EEA two-letter code"}
_candidate_props["cin_llpin"] = {"type": "string", "description": "21-character CIN or AAA-1234 LLPIN"}

CLIENT_TOOLS = [
    {
        "name": "check_known",
        "description": "Check whether a company is already in the lead database (verified, queued or excluded) "
                       "before spending research on it. Give any of: name, CIN/LLPIN, website domain.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["name", "cin", "domain"],
                         "properties": {"name": _nullable, "cin": _nullable, "domain": _nullable}},
    },
    {
        "name": "lookup_indian_company",
        "description": "Search the locally imported MCA / data.gov.in company master data by exact or partial "
                       "legal name, or by CIN/LLPIN. Returns official registration details and the source URL.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["name", "cin"],
                         "properties": {"name": _nullable, "cin": _nullable}},
    },
    {
        "name": "fetch_url",
        "description": "Fetch a public web page from this server (e.g. a state registry search-results or detail "
                       "page whose URL you construct). Honours robots.txt and rate limits. Returns page text. "
                       "If the response is a CAPTCHA/login wall it says so - do not retry it.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["url"],
                         "properties": {"url": {"type": "string"}}},
    },
    {
        "name": "submit_candidate",
        "description": "Record one researched company in the database. Submit every company you examined, at any "
                       "status. Omit any field you could not find - never guess. The reply says whether it became a "
                       "verified lead, went to the research queue (and what is missing), was a duplicate, or was "
                       "disqualified.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["verification_status"], "properties": _candidate_props},
    },
    {
        "name": "finish_batch",
        "description": "Call once when this assignment is done. Summarise it.",
        "strict": True,
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "required": ["summary", "blocked_sources", "suggestions"],
            "properties": {
                "summary": {"type": "string"},
                "blocked_sources": {"type": "array", "items": {"type": "string"},
                                    "description": "host - reason, for every source that was inaccessible, "
                                                   "CAPTCHA-gated, login-gated or rate-limited"},
                "suggestions": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
]


WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": "Search the web for company websites, terms of service, registry records, and founder profiles.",
    "strict": True,
    "input_schema": {
        "type": "object", "additionalProperties": False, "required": ["query"],
        "properties": {"query": {"type": "string", "description": "Search query string"}},
    },
}

for _t in CLIENT_TOOLS:
    _t["eager_input_streaming"] = True  # stream large tool inputs; validate_input() guards truncation

REQUIRED = {t["name"]: t["input_schema"]["required"] for t in CLIENT_TOOLS}
REQUIRED["web_search"] = ["query"]


def validate_input(name, inp):
    """With eager input streaming the API no longer validates tool input, so the client must."""
    if name not in REQUIRED:
        return f"unknown tool {name}"
    if not isinstance(inp, dict):
        return "INVALID_JSON: tool input was not a JSON object - resend the call"
    missing = [k for k in REQUIRED[name] if k not in inp]
    if missing:
        return f"INVALID_JSON: missing required field(s) {missing} - resend the call"
    if name == "submit_candidate" and not (inp.get("indian_legal_name") or inp.get("indian_brand_name")
                                           or inp.get("us_llc_legal_name")):
        return "submit_candidate needs at least a company or LLC name"
    return None


def server_tools(settings):
    search = {"type": WEB_SEARCH_TYPE, "name": "web_search",
              "max_uses": int(settings["web_search_max_uses_per_batch"])}
    fetch = {"type": WEB_FETCH_TYPE, "name": "web_fetch",
             "max_uses": int(settings["web_fetch_max_uses_per_batch"]), "max_content_tokens": 8000}
    if settings.get("blocked_domains"):
        search["blocked_domains"] = list(settings["blocked_domains"])
        fetch["blocked_domains"] = list(settings["blocked_domains"])
    return [search, fetch]


_CAPTCHA_MARKERS = ("captcha", "recaptcha", "hcaptcha", "are you a robot", "verify you are human",
                    "cf-challenge", "access denied")


class Fetcher:
    """Polite HTTP fetcher: robots.txt, per-host spacing, size cap, CAPTCHA detection."""

    def __init__(self, blocked_domains=(), min_interval=2.0, timeout=25, max_bytes=3_000_000):
        self.blocked = {d.lower() for d in blocked_domains}
        self.min_interval, self.timeout, self.max_bytes = min_interval, timeout, max_bytes
        self._last, self._robots, self._lock = {}, {}, threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": Config.USER_AGENT, "Accept-Language": "en"})

    def _allowed(self, url):
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[base] = rp
        return self._robots[base].can_fetch(Config.USER_AGENT, url)

    def get(self, url):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return {"ok": False, "error": "only absolute http(s) URLs are allowed"}
        host = p.hostname.lower()
        if any(host == d or host.endswith("." + d) for d in self.blocked):
            return {"ok": False, "error": "domain is on the admin block list"}
        if not self._allowed(url):
            return {"ok": False, "blocked": True, "error": "disallowed by robots.txt"}
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
        try:
            r = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True)
            body = r.raw.read(self.max_bytes, decode_content=True)
        except requests.RequestException as e:
            return {"ok": False, "error": f"request failed: {type(e).__name__}"}
        text = body.decode(r.encoding or "utf-8", errors="replace")
        if "html" in r.headers.get("content-type", "html"):
            soup = BeautifulSoup(text, "html.parser")
            for t in soup(["script", "style", "noscript", "svg"]):
                t.decompose()
            text = " ".join(soup.get_text(" ").split())
        low = text[:5000].lower()
        if r.status_code in (401, 403, 429) or any(m in low for m in _CAPTCHA_MARKERS):
            return {"ok": False, "blocked": True, "status": r.status_code,
                    "error": "access restricted (CAPTCHA, login or rate limit) - record as a limitation"}
        return {"ok": r.status_code < 400, "status": r.status_code, "final_url": r.url, "text": text[:6000],
                "truncated": len(text) > 6000}


class ToolBox:
    """Executes client tool calls for one run."""

    def __init__(self, db, run, stats, settings, fetcher=None, on_event=None):
        self.db, self.run, self.stats, self.settings = db, run, stats, settings
        self.fetcher = fetcher or Fetcher(settings.get("blocked_domains") or [])
        self.on_event = on_event or (lambda *a, **k: None)
        self.batch_finished = None
        self.blocked, self.sources, self.errors = set(), set(), []
        self.batch_candidates = self.batch_verified = 0

    def execute(self, name, args):
        problem = validate_input(name, args)
        if problem:
            return {"error": problem}
        try:
            if name == "check_known":
                hits = known_lookup(self.db, args.get("name"), args.get("cin"), args.get("domain"))
                return {"known": bool(hits), "matches": hits}
            if name == "lookup_indian_company":
                return self._lookup_indian(args.get("name"), args.get("cin"))
            if name == "fetch_url":
                res = self.fetcher.get(args["url"])
                host = R.domain_of(args["url"])
                self.sources.add(host)
                if res.get("blocked"):
                    self.blocked.add(f"{host} - {res['error']}")
                return res
            if name == "submit_candidate":
                before = self.stats["new_verified"]
                res = ingest_candidate(self.db, args, self.run.id if self.run else None, self.stats,
                                       default_owner=self.settings.get("default_owner") or "")
                self.batch_candidates += 1
                self.batch_verified += self.stats["new_verified"] - before
                self.on_event("candidate", res)
                return res
            if name == "web_search":
                return self.search_web(args.get("query", ""))
            if name == "finish_batch":
                self.batch_finished = args
                self.blocked.update(args.get("blocked_sources") or [])
                return {"ok": True}
            return {"error": f"unknown tool {name}"}
        except Exception as e:  # a tool failure is reported to the model, not raised
            self.db.rollback()
            self.errors.append(f"{name}: {type(e).__name__}: {e}"[:300])
            self.on_event("tool_error", {"tool": name, "error": repr(e)})
            return {"error": f"{type(e).__name__}: {e}"}

    def search_web(self, query):
        if not query:
            return {"results": [], "error": "search query is required"}
        try:
            r = requests.post("https://html.duckduckgo.com/html/",
                              data={"q": query},
                              headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                              timeout=12)
            if r.status_code >= 400:
                return {"results": [], "error": f"Search returned HTTP {r.status_code}"}
            soup = BeautifulSoup(r.text, "html.parser")
            items = []
            for res in soup.select(".result")[:8]:
                t = res.select_one(".result__title a")
                s = res.select_one(".result__snippet")
                if t:
                    url = t.get("href", "")
                    title = t.get_text(strip=True)
                    snippet = s.get_text(strip=True) if s else ""
                    items.append({"title": title, "url": url, "snippet": snippet})
                    self.sources.add(R.domain_of(url))
            return {"results": items}
        except Exception as e:
            return {"results": [], "error": f"Search failed: {e}"}

    def _lookup_indian(self, name, cin):
        q = self.db.query(IndianCompany)
        if cin:
            rows = q.filter(IndianCompany.cin == cin.strip().upper()).limit(5).all()
        elif name:
            nn = R.norm_name(name)
            rows = q.filter(IndianCompany.norm_name == nn).limit(5).all() or \
                q.filter(IndianCompany.norm_name.like(f"%{nn}%")).limit(10).all()
        else:
            rows = []
        if not rows and q.count() == 0:
            return {"results": [], "note": "No master data imported yet - find the MCA or data.gov.in record online."}
        return {"results": [{"cin": r.cin, "name": r.name, "status": r.status, "state": r.state,
                             "incorporation_date": r.incorporation_date, "class": r.company_class,
                             "source_url": r.source_url} for r in rows]}


def result_block(tool_use_id, payload):
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(payload, default=str),
            "is_error": bool(isinstance(payload, dict) and payload.get("error") and len(payload) == 1)}
