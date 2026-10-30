"""Runs one research run: plans assignments, drives the Claude agent loop per assignment, logs everything."""
import json
import logging
import time
from datetime import date, datetime

import anthropic

from .. import appsettings
from ..config import Config
from ..ingest import EMPTY_STATS
from ..models import Coverage, Lead, utcnow
from . import planner, prompts
from .planner import describe_targets
from .tools import CLIENT_TOOLS, ToolBox, result_block, server_tools

log = logging.getLogger("research")

# USD per million tokens (input, output, cache read). Web search is billed per 1,000 searches.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}
WEB_SEARCH_PER_1K = 10.00
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class StopRun(Exception):
    pass


def run_cap(run):
    """Per-run lead cap chosen on the launch page (0 = none)."""
    return int(((run.targets if run else None) or {}).get("max_leads") or 0)


def make_client(db):
    key = Config.ANTHROPIC_API_KEY or appsettings.get(db, "anthropic_api_key")
    if not key:
        raise RuntimeError("No Anthropic API key configured (Settings -> Research, or ANTHROPIC_API_KEY)")
    return anthropic.Anthropic(api_key=key, max_retries=4, timeout=900)


class Researcher:
    def __init__(self, db, run, client=None, settings=None):
        self.db, self.run = db, run
        self.settings = settings or appsettings.get_all(db, include_secrets=True)
        self.client = client or make_client(db)
        self.stats = dict(EMPTY_STATS, **(run.stats or {}))
        self.started = time.monotonic()
        self.tools = ToolBox(db, run, self.stats, self.settings, on_event=self._event)
        self.model = self.settings["model"]

    # ---- bookkeeping -------------------------------------------------------------------------
    def _event(self, kind, payload):
        if kind == "candidate":
            self.note(f"candidate -> {payload.get('outcome')} {payload.get('lead_id', '')}".strip())
        elif kind == "tool_error":
            self.run.errors = (self.run.errors or []) + [f"{payload['tool']}: {payload['error']}"]

    def note(self, msg):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        log.info("run %s: %s", self.run.id, msg)
        self.run.log = (self.run.log or [])[-1500:] + [line]

    def save(self):
        self.run.stats = dict(self.stats)
        self.run.heartbeat_at = utcnow()
        self.db.commit()

    def _account(self, usage):
        if usage is None:
            return
        inp = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        out = usage.output_tokens or 0
        cache = getattr(usage, "cache_read_input_tokens", 0) or 0
        stu = getattr(usage, "server_tool_use", None)
        searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        p_in, p_out, p_cache = PRICES.get(self.model, PRICES["claude-opus-5-5"])
        cost = (inp * p_in + out * p_out + cache * p_cache) / 1e6 + searches * WEB_SEARCH_PER_1K / 1000
        self.run.input_tokens = (self.run.input_tokens or 0) + inp
        self.run.output_tokens = (self.run.output_tokens or 0) + out
        self.run.cache_read_tokens = (self.run.cache_read_tokens or 0) + cache
        self.run.web_searches = (self.run.web_searches or 0) + searches
        self.run.cost_usd = round((self.run.cost_usd or 0) + cost, 4)

    def check_limits(self):
        self.db.refresh(self.run, ["cancel_requested"])
        if self.run.cancel_requested:
            raise StopRun("cancelled by user")
        if (time.monotonic() - self.started) / 60 > float(self.settings["max_run_minutes"]):
            raise StopRun("time limit reached")
        if (self.run.cost_usd or 0) >= float(self.settings["max_cost_usd_per_run"]):
            raise StopRun("cost cap reached")
        if self.leads_left() <= 0:
            cap = run_cap(self.run)
            daily = f"daily limit {self.settings['daily_target']} verified + web-verified"
            raise StopRun(f"lead limit reached (this run: {cap} leads; {daily})" if cap else f"{daily} reached")

    def leads_left(self):
        """Leads still allowed: the daily limit across all of today's runs (new verified plus newly
        web-verified), and - when the run was launched with one - this run's own lead cap."""
        from zoneinfo import ZoneInfo
        from sqlalchemy import or_
        tz = ZoneInfo(self.settings.get("timezone") or "Asia/Kolkata")
        start = (datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
                 .astimezone(ZoneInfo("UTC")).replace(tzinfo=None))
        today = self.db.query(Lead).filter(or_(Lead.verified_at >= start, Lead.web_verified_at >= start)).count()
        left = max(0, int(self.settings["daily_target"]) - today)
        cap = run_cap(getattr(self, "run", None))
        if cap:
            mine = self.stats.get("new_verified", 0) + self.stats.get("web_verified", 0)
            left = min(left, max(0, cap - mine))
        return left

    # ---- model calls --------------------------------------------------------------------------
    def _call(self, messages):
        kwargs = dict(
            model=self.model, max_tokens=16000, system=prompts.SYSTEM,
            tools=server_tools(self.settings) + CLIENT_TOOLS, messages=messages,
            output_config={"effort": self.settings["effort"]},
            cache_control={"type": "ephemeral"},
        )
        if self.model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"):
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        with self.client.beta.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def run_batch(self, group):
        today = date.today().isoformat()
        messages = [{"role": "user", "content": prompts.batch_prompt(group, today, self.leads_left())}]
        self.tools.batch_finished = None
        self.tools.batch_candidates = self.tools.batch_verified = 0
        queries = []
        for turn in range(int(self.settings["max_turns_per_batch"])):
            self.check_limits()
            try:
                resp = self._call(messages)
            except anthropic.RateLimitError:
                self.note("rate limited; sleeping 60s")
                time.sleep(60)
                continue
            except (anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                self.run.errors = (self.run.errors or []) + [f"API transient error: {type(e).__name__}"]
                time.sleep(15)
                continue
            self._account(resp.usage)
            for b in resp.content:
                if b.type == "server_tool_use" and b.name == "web_search":
                    queries.append((b.input or {}).get("query", ""))
                elif b.type == "server_tool_use" and b.name == "web_fetch":
                    self.tools.sources.add(_host((b.input or {}).get("url", "")))
            self.save()

            if resp.stop_reason == "refusal":
                cat = getattr(getattr(resp, "stop_details", None), "category", None)
                self.note(f"model declined this assignment (category={cat}); skipping")
                break
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue  # server-side tool loop paused; resend so it resumes
            calls = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason == "max_tokens":
                # A tool call cut off mid-input must not run; every tool_use still needs a tool_result.
                results = [result_block(c.id, {"error": "INVALID_JSON: output limit hit while writing this call - "
                                                        "resend it, shorter"}) for c in calls]
                messages.append({"role": "user", "content": results or
                                 "You hit the output limit. Continue concisely."})
                continue
            if not calls:
                break
            results = [result_block(c.id, self.tools.execute(c.name, c.input)) for c in calls]
            messages.append({"role": "user", "content": results})
            self.save()
            if self.tools.batch_finished is not None:
                break
        return queries

    def recommendations(self, coverage_rows):
        data = json.dumps({"stats": self.stats, "blocked_sources": sorted(self.tools.blocked),
                           "assignments": coverage_rows}, default=str)[:30000]
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=4000, output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": {
                        "type": "object", "additionalProperties": False, "required": ["recommendations"],
                        "properties": {"recommendations": {"type": "array", "items": {"type": "string"}}}}}},
                messages=[{"role": "user", "content": prompts.RECOMMEND_PROMPT.format(data=data)}])
            self._account(resp.usage)
            text = next(b.text for b in resp.content if b.type == "text")
            return json.loads(text)["recommendations"][:3]
        except Exception as e:  # recommendations are nice-to-have; never fail the run over them
            self.run.errors = (self.run.errors or []) + [f"recommendations: {type(e).__name__}"]
            return []

    # ---- orchestration ------------------------------------------------------------------------
    def execute(self):
        s = self.settings
        t = self.run.targets or {}  # set when the run was launched with a custom focus
        budget = int(t.get("assignments") or s["max_batches_per_run"])
        revisits = planner.revisit_groups(self.db, int(s["revisit_queue_per_run"])) if t.get("revisit", True) else []
        groups = (revisits + planner.plan(self.db, s, budget, t))[:budget]
        self.note(f"planned {len(groups)} assignments; target {s['daily_target']}"
                  + (f"; focus: {describe_targets(t)}" if t else ""))
        coverage_rows, stop_reason = [], "all assignments processed"
        try:
            for i, g in enumerate(groups, 1):
                self.check_limits()
                self.note(f"assignment {i}/{len(groups)}: {g['id']}")
                before_c, t0 = self.stats["candidates"], time.monotonic()
                queries = self.run_batch(g)
                self.run.queries = (self.run.queries or []) + queries
                self.run.query_groups = (self.run.query_groups or []) + [g["id"]]
                row = dict(query_group=g["id"], industry=g.get("industry"), us_state=g.get("us_state"),
                           market=g.get("market"),
                           indian_state=g.get("indian_state"), pattern=g.get("pattern"),
                           queries_run=len(queries), candidates_yielded=self.stats["candidates"] - before_c,
                           verified_yielded=self.tools.batch_verified, sources=", ".join(sorted(self.tools.sources))[:2000])
                self.db.add(Coverage(run_id=self.run.id, date=date.today(), **row))
                coverage_rows.append({**row, "minutes": round((time.monotonic() - t0) / 60, 1)})
                if self.tools.batch_finished and self.tools.batch_finished.get("suggestions"):
                    self.note("suggestions: " + " | ".join(self.tools.batch_finished["suggestions"])[:500])
                self.save()
        except StopRun as e:
            stop_reason = str(e)
        self.note(f"stopping: {stop_reason}")
        self.run.blocked_sources = sorted(self.tools.blocked)
        self.run.sources = sorted(x for x in self.tools.sources if x)
        self.run.recommendations = self.recommendations(coverage_rows)
        self.stats["stop_reason"] = stop_reason
        self.stats["total_verified"] = self.db.query(Lead).filter_by(verification_status="VERIFIED").count()
        self.stats["queue_size"] = self.db.query(Lead).filter(
            Lead.verification_status.in_(("PARTIALLY VERIFIED", "UNVERIFIED"))).count()
        self.save()
        return stop_reason


def _host(url):
    from ..rules import domain_of
    return domain_of(url)
