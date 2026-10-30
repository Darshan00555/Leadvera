"""stdio MCP server exposing the portal's research tools to Claude Code (`claude -p`).

Started by Claude Code itself from the --mcp-config the worker writes for each assignment:
    python -m app.research.mcp_server
Environment: DATABASE_URL, AF_RUN_ID, AF_STATE_FILE (where this batch's stats are written for the worker).
"""
import json
import os

from mcp.server.mcpserver import MCPServer

from .. import appsettings
from ..db import SessionLocal
from ..fields import RESEARCH_FIELDS
from ..ingest import EMPTY_STATS
from ..models import Run
from .tools import ToolBox

server = MCPServer(
    name="afleads",
    instructions="Lead database: check what is already known, look up Indian company "
                 "master data, fetch registry pages politely, and record every researched company.")

_db = SessionLocal()
_run = _db.get(Run, int(os.environ["AF_RUN_ID"])) if os.environ.get("AF_RUN_ID") else None
_settings = appsettings.get_all(_db, include_secrets=False)
_stats = dict(EMPTY_STATS)
_box = ToolBox(_db, _run, _stats, _settings)


def _checkpoint():
    """Persist this batch's progress after every call so a killed process loses nothing."""
    path = os.environ.get("AF_STATE_FILE")
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"stats": _stats, "blocked": sorted(_box.blocked), "sources": sorted(_box.sources),
                   "finished": _box.batch_finished, "verified": _box.batch_verified,
                   "candidates": _box.batch_candidates, "errors": _box.errors}, fh)
    os.replace(tmp, path)


def _call(name, args):
    res = _box.execute(name, args)
    _checkpoint()
    return json.dumps(res, default=str)


@server.tool(description="Check whether a company is already in the lead database (verified, queued or excluded) "
                         "before researching it. Give any of: name, CIN/LLPIN, website domain.")
def check_known(name: str | None = None, cin: str | None = None, domain: str | None = None) -> str:
    return _call("check_known", {"name": name, "cin": cin, "domain": domain})


@server.tool(description="Search the locally imported MCA / data.gov.in company master data by legal name or "
                         "CIN/LLPIN. Returns official registration details and the source URL to cite.")
def lookup_indian_company(name: str | None = None, cin: str | None = None) -> str:
    return _call("lookup_indian_company", {"name": name, "cin": cin})


@server.tool(description="Fetch a public web page from the portal server (e.g. a state registry search-results "
                         "or detail page whose URL you construct). Honours robots.txt and rate limits. If the "
                         "response is a CAPTCHA/login wall it says so - do not retry it.")
def fetch_url(url: str) -> str:
    return _call("fetch_url", {"url": url})


@server.tool(description="Record ONE researched company. Submit every company you examined, at any status. "
                         "`candidate` is an object with verification_status (VERIFIED | PARTIALLY VERIFIED | "
                         "UNVERIFIED | DISQUALIFIED) plus any of these fields you actually found (omit the rest, "
                         "never guess): " + ", ".join(f for f in RESEARCH_FIELDS if f != "verification_status")
                         + ". relationship_evidence_urls and source_urls are lists of URLs; dates are YYYY-MM-DD; "
                         "us_state is a 2-letter code. The reply says whether it became a verified lead, went to "
                         "the research queue (and what is missing), was a duplicate, or was disqualified.")
def submit_candidate(candidate: dict) -> str:
    return _call("submit_candidate", candidate)


@server.tool(description="Call once when this assignment is done.")
def finish_batch(summary: str, blocked_sources: list[str] | None = None, suggestions: list[str] | None = None) -> str:
    return _call("finish_batch", {"summary": summary, "blocked_sources": blocked_sources or [],
                                  "suggestions": suggestions or []})


if __name__ == "__main__":
    _checkpoint()
    server.run("stdio")
