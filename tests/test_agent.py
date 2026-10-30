"""Drives the research loop with a scripted fake Claude client (no network, no API key)."""
from datetime import date
from types import SimpleNamespace as NS

from app import appsettings
from app.models import Coverage, Lead, Run
from app.research.agent import Researcher

from .conftest import GOOD


def usage(i=1000, o=500, searches=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=0, cache_creation_input_tokens=0,
              server_tool_use=NS(web_search_requests=searches))


def msg(stop, *blocks, searches=0):
    return NS(stop_reason=stop, content=list(blocks), usage=usage(searches=searches), stop_details=None)


def tool(id, name, inp):
    return NS(type="tool_use", id=id, name=name, input=inp)


class FakeStream:
    def __init__(self, m): self.m = m
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get_final_message(self): return self.m


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = NS(messages=NS(stream=self._stream))
        self.messages = NS(create=self._create)

    def _stream(self, **kw):
        # snapshot: the agent keeps appending to the same messages list after the call
        self.calls.append({**kw, "messages": [dict(m) for m in kw["messages"]]})
        return FakeStream(self.script.pop(0))

    def _create(self, **kw):
        return NS(content=[NS(type="text", text='{"recommendations": ["a", "b", "c"]}')], usage=usage())


def candidate(**over):
    from app.fields import RESEARCH_FIELDS
    c = {k: None for k in RESEARCH_FIELDS}
    c.update({k: v for k, v in GOOD.items() if k in c})
    c.update(over)
    return c


def test_agent_loop_end_to_end(db):
    s = appsettings.get_all(db, include_secrets=True)
    s.update(max_batches_per_run=1, revisit_queue_per_run=0, daily_target=200)
    script = [
        # server-side search loop paused -> must be resent, not treated as done
        msg("pause_turn", NS(type="server_tool_use", name="web_search", input={"query": "q1"}), searches=3),
        msg("tool_use", tool("t1", "check_known", {"name": "Testco", "cin": None, "domain": None})),
        msg("tool_use", tool("t2", "submit_candidate", candidate()),
            tool("t3", "submit_candidate", candidate(us_llc_legal_name="Other Corp", cin_llpin="U72900KA2019PTC000001",
                                                     us_entity_number="9", indian_website="https://x.example",
                                                     indian_legal_name="Other Pvt Ltd"))),
        msg("tool_use", tool("t4", "finish_batch", {"summary": "done", "blocked_sources": ["icis.corp.delaware.gov - CAPTCHA"],
                                                    "suggestions": []})),
    ]
    client = FakeClient(script)
    run = Run(run_date=date.today(), trigger="manual", status="running")
    db.add(run); db.commit()
    stop = Researcher(db, run, client=client, settings=s).execute()

    assert stop == "all assignments processed"
    assert db.query(Lead).filter_by(verification_status="VERIFIED").count() == 1
    assert run.stats["new_verified"] == 1 and run.stats["disqualified"] == 1
    assert run.web_searches == 3 and run.cost_usd > 0
    assert run.queries == ["q1"] and run.recommendations == ["a", "b", "c"]
    assert "icis.corp.delaware.gov - CAPTCHA" in run.blocked_sources
    assert db.query(Coverage).one().verified_yielded == 1
    # pause_turn: the paused assistant turn is appended and the next request is made without a new user turn
    second = client.calls[1]["messages"]
    assert second[-1]["role"] == "assistant"
    # parallel tool results go back in ONE user message
    third = client.calls[3]["messages"]
    assert third[-1]["role"] == "user" and len(third[-1]["content"]) == 2
    # refusal fallback + caching + effort are sent
    assert client.calls[0]["fallbacks"] == "default" and client.calls[0]["cache_control"] == {"type": "ephemeral"}
    assert client.calls[0]["output_config"]["effort"] == s["effort"]


def test_cost_cap_stops_run(db):
    s = appsettings.get_all(db, include_secrets=True)
    s.update(max_batches_per_run=3, revisit_queue_per_run=0, max_cost_usd_per_run=0.0001)
    client = FakeClient([msg("end_turn", NS(type="text", text="nothing"))] * 3)
    run = Run(run_date=date.today(), trigger="manual", status="running")
    db.add(run); db.commit()
    assert Researcher(db, run, client=client, settings=s).execute() == "cost cap reached"
    assert len(client.calls) == 1


def test_cancel_stops_run(db):
    s = appsettings.get_all(db, include_secrets=True)
    run = Run(run_date=date.today(), trigger="manual", status="running", cancel_requested=True)
    db.add(run); db.commit()
    client = FakeClient([])
    assert Researcher(db, run, client=client, settings=s).execute() == "cancelled by user"
    assert client.calls == []


def test_truncated_tool_call_is_not_executed(db):
    s = appsettings.get_all(db, include_secrets=True)
    s.update(max_batches_per_run=1, revisit_queue_per_run=0)
    script = [msg("max_tokens", tool("t1", "submit_candidate", {"indian_legal_name": "Half"})),
              msg("end_turn", NS(type="text", text="done"))]
    client = FakeClient(script)
    run = Run(run_date=date.today(), trigger="manual", status="running")
    db.add(run); db.commit()
    Researcher(db, run, client=client, settings=s).execute()
    assert db.query(Lead).count() == 0
    follow_up = client.calls[1]["messages"][-1]
    assert follow_up["role"] == "user" and follow_up["content"][0]["tool_use_id"] == "t1"
    assert follow_up["content"][0]["is_error"]


def test_validate_input():
    from app.research.tools import validate_input
    assert validate_input("fetch_url", {}) .startswith("INVALID_JSON")
    assert validate_input("submit_candidate", {"verification_status": "VERIFIED"})
    assert validate_input("submit_candidate", {"verification_status": "VERIFIED", "indian_legal_name": "X"}) is None
    assert validate_input("nope", {}) == "unknown tool nope"
