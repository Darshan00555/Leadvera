"""Claude subscription backend: real MCP server + real subprocess wiring, with a fake `claude` CLI."""
import json
import os
import stat
import sys
from datetime import date

import pytest

from app import appsettings
from app.models import Lead, Run
from app.research.agent import StopRun
from app.research.claude_code import ClaudeCodeError, ClaudeCodeResearcher, child_env

from .conftest import GOOD

FAKE = os.path.join(os.path.dirname(__file__), "fake_claude.py")


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    os.chmod(FAKE, os.stat(FAKE).st_mode | stat.S_IEXEC)
    log = tmp_path / "claude_calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CANDIDATE", json.dumps(GOOD))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-SHOULD-NOT-LEAK")
    return log


def researcher(db, **over):
    s = appsettings.get_all(db, include_secrets=True)
    s.update({"max_batches_per_run": 1, "revisit_queue_per_run": 0, "claude_code_oauth_token": "sk-ant-oat01-test",
              **over})
    run = Run(run_date=date.today(), trigger="manual", status="running")
    db.add(run); db.commit()
    return ClaudeCodeResearcher(db, run, settings=s, claude_bin=FAKE), run


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shebang script as the CLI")
def test_full_run_through_mcp(db, fake):
    r, run = researcher(db)
    stop = r.execute()
    assert stop == "all assignments processed"
    lead = db.query(Lead).one()
    assert lead.verification_status == "VERIFIED" and lead.first_run_id == run.id
    assert run.stats["new_verified"] == 1 and run.stats["candidates"] == 1
    assert run.queries == ["fake query"] and "icis.corp.delaware.gov - CAPTCHA" in run.blocked_sources
    assert run.recommendations == ["r1", "r2", "r3"]
    assert run.cost_usd == 0  # subscription: nothing billed to the API cost meter
    calls = [json.loads(l) for l in fake.read_text().splitlines()]
    research = calls[0]
    # API key must never reach Claude Code; the subscription token must
    assert research["env"]["ANTHROPIC_API_KEY"] is None
    assert research["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
    a = research["argv"]
    assert "-p" in a and "--strict-mcp-config" in a and "dontAsk" in a and "--bare" not in a
    assert "mcp__afleads" in a and "Bash" in a[a.index("--disallowedTools"):]
    assert a[a.index("--tools") + 1:a.index("--tools") + 3] == ["WebSearch", "WebFetch"]
    recs = calls[1]["argv"]
    assert recs[recs.index("--tools") + 1] == "" and "--mcp-config" not in recs


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shebang script as the CLI")
def test_usage_limit_stops_cleanly(db, fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    r, run = researcher(db, max_batches_per_run=3)
    stop = r.execute()
    assert "usage limit" in stop
    assert len(fake.read_text().splitlines()) == 2  # one research attempt + recommendations, no retries


@pytest.mark.parametrize("text", ["You've hit your session limit · resets 3:30pm (UTC)",
                                  "You've hit your weekly limit", "Claude AI usage limit reached|1793120000"])
def test_limit_messages_recognised(text):
    from app.research.claude_code import LIMIT_RE
    assert LIMIT_RE.search(text)


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shebang script as the CLI")
def test_not_signed_in_fails_with_clear_message(db, fake, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth")
    r, _ = researcher(db)
    with pytest.raises(ClaudeCodeError, match="setup-token"):
        r.execute()


def test_child_env_strips_api_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    env = child_env("tok")
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


def test_missing_cli_is_explained(db, monkeypatch):
    monkeypatch.setattr("app.research.claude_code.find_claude", lambda: None)
    run = Run(run_date=date.today(), trigger="manual", status="running")
    db.add(run); db.commit()
    with pytest.raises(ClaudeCodeError, match="not found"):
        ClaudeCodeResearcher(db, run)
