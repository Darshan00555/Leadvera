"""Research backend that runs Anthropic's Claude Code CLI (`claude -p`) signed in with a Claude subscription
(Pro / Max), so research draws from the plan's usage limits instead of separately billed API credits.

Each assignment is one headless Claude Code session with:
  - built-in WebSearch / WebFetch,
  - the portal's tools via the `afleads` MCP server (app/research/mcp_server.py),
  - everything else (shell, file edits) removed or denied.
Authentication comes from CLAUDE_CODE_OAUTH_TOKEN (created with `claude setup-token`); any API key in the
environment is removed from the child process so it can never silently switch to API billing.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .. import appsettings, db as dbmod
from ..config import Config
from . import prompts
from .agent import Researcher, StopRun

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
DENIED_TOOLS = ["Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "Task", "Agent"]
LIMIT_RE = re.compile(r"(usage|session|weekly) limit|limit reached|hit your (\w+ )?limit|out of (extra )?usage|"
                      r"limit will reset|resets? (at|in|\d)", re.I)
AUTH_RE = re.compile(r"invalid api key|please run /login|not logged in|authentication|unauthori[sz]ed|oauth token "
                     r"(has )?expired|invalid bearer|401", re.I)

TOOL_NOTE = """

Tool names in this session: web search = WebSearch, web fetch = WebFetch (if it is not available, open \
pages with the afleads fetch_url tool instead), and the portal tools are the `afleads` MCP tools \
(check_known, lookup_indian_company, fetch_url, submit_candidate, finish_batch). You have no shell and no \
file access; record everything through submit_candidate."""

# Effort-level instructions injected into the system prompt to control research depth.
EFFORT_NOTES = {
    "low": "\n\nEFFORT MODE: LOW (credit-saving). For each company: run at most 3 web searches, open at most 2 "
           "pages. If basic evidence is found, submit immediately — do not dig further. Skip companies that "
           "need more than one page fetch to locate the Indian CIN or US registry record. Move on quickly.",
    "medium": "\n\nEFFORT MODE: MEDIUM (balanced). For each company: run up to 6 searches, open up to 4 "
              "pages. Find the CIN on the company's own site, the overseas entity's exact name and one page linking "
              "both, then submit. "
              "Don't over-research; move to the next company once the required evidence is found.",
    "high": "\n\nEFFORT MODE: HIGH (thorough). Research each company deeply: CIN from the company's own site, "
            "the overseas entity on its official registry where it opens without a CAPTCHA, the relationship "
            "from two independent sources, and every requested contact field before submitting.",
}


class ClaudeCodeError(RuntimeError):
    pass


def find_claude():
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude")


def child_env(token):
    env = {k: v for k, v in os.environ.items() if k not in API_KEY_VARS and not k.startswith("AF_")}
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


class ClaudeCodeResearcher(Researcher):
    backend = "claude_code"

    def __init__(self, db, run, settings=None, claude_bin=None):
        settings = settings or appsettings.get_all(db, include_secrets=True)
        super().__init__(db, run, client=object(), settings=settings)
        self.claude_bin = claude_bin or find_claude()
        if not self.claude_bin:
            raise ClaudeCodeError("Claude Code CLI not found. Install it (npm install -g @anthropic-ai/claude-code) "
                                  "or use the Docker image, which includes it.")
        self.token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or settings.get("claude_code_oauth_token") or ""
        self.model = settings.get("claude_code_model") or "sonnet"
        self.batch_seconds = int(settings.get("claude_code_batch_minutes") or 10) * 60
        self.effort = settings.get("claude_code_effort") or "medium"
        self.consecutive_failures = 0

    # ---- one headless Claude Code session --------------------------------------------------------------
    def _session(self, prompt, workdir, use_tools=True):
        # --tools fixes the built-in tool set (verified against Claude Code 2.1.311): only web search/fetch for
        # research, none for the recommendations call. --disallowedTools is a second line of defence.
        builtins = ["WebSearch", "WebFetch"] if use_tools else [""]
        cmd = [self.claude_bin, "-p", "--output-format", "stream-json", "--verbose", "--model", self.model,
               "--permission-mode", "dontAsk", "--no-session-persistence", "--strict-mcp-config",
               "--tools", *builtins, "--disallowedTools", *DENIED_TOOLS]
        if use_tools:
            cfg = self._mcp_config(workdir)
            effort_note = EFFORT_NOTES.get(self.effort, EFFORT_NOTES["medium"])
            cmd += ["--mcp-config", cfg, "--allowedTools", "WebSearch", "WebFetch", "mcp__afleads",
                    "--append-system-prompt", prompts.SYSTEM + TOOL_NOTE + effort_note]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=workdir, env=child_env(self.token))
        deadline = time.monotonic() + self.batch_seconds
        out = err = ""
        pending_input = prompt
        while True:
            try:
                o, e = proc.communicate(input=pending_input, timeout=15)
                out, err = out + (o or ""), err + (e or "")
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                self.save()  # heartbeat
                stop = None
                try:
                    self.check_limits()
                except StopRun as s:
                    stop = s
                if stop or time.monotonic() > deadline:
                    proc.kill()
                    o, e = proc.communicate()
                    out, err = out + (o or ""), err + (e or "")
                    if stop:
                        raise stop
                    self.note("assignment hit its time limit; moving on")
                    break
        return proc.returncode, out, err

    def _mcp_config(self, workdir):
        self._state_file = os.path.join(workdir, "state.json")
        env = {
            "DATABASE_URL": dbmod.engine.url.render_as_string(hide_password=False),
            "AF_RUN_ID": str(self.run.id), "AF_STATE_FILE": self._state_file,
            "SECRET_KEY": Config.secret_key(), "DATA_DIR": Config.DATA_DIR, "PYTHONPATH": PROJECT_ROOT,
            "RESEARCH_USER_AGENT": Config.USER_AGENT,
        }
        path = os.path.join(workdir, "mcp.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds the DB URL
        with os.fdopen(fd, "w") as fh:
            json.dump({"mcpServers": {"afleads": {"type": "stdio", "command": sys.executable,
                                                  "args": ["-m", "app.research.mcp_server"], "env": env}}}, fh)
        return path

    @staticmethod
    def _parse(out):
        result, queries, fetched = None, [], []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                result = ev
            elif ev.get("type") == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use" and b.get("name") == "WebSearch":
                        queries.append((b.get("input") or {}).get("query", ""))
                    elif b.get("type") == "tool_use" and b.get("name") == "WebFetch":
                        fetched.append((b.get("input") or {}).get("url", ""))
        return result, queries, fetched

    def _classify_failure(self, code, result, err):
        text = " ".join(filter(None, [str((result or {}).get("result") or ""), err[-2000:]]))
        if LIMIT_RE.search(text):
            raise StopRun("Claude subscription usage limit reached - research resumes on the next run")
        if AUTH_RE.search(text):
            raise ClaudeCodeError("Claude Code is not signed in or the token expired. Create a new token with "
                                  "`claude setup-token` and paste it in Settings -> Research engine.")
        return text.strip()[:400] or f"claude exited with code {code}"

    # ---- Researcher hooks --------------------------------------------------------------------------------
    def run_batch(self, group):
        from datetime import date
        left = self.leads_left()
        prompt = prompts.batch_prompt(group, date.today().isoformat(), left)
        workdir = tempfile.mkdtemp(prefix="afcc-")
        self.tools.batch_finished, self.tools.batch_verified = None, 0
        try:
            stop = None
            try:
                code, out, err = self._session(prompt, workdir)
            except StopRun as s:
                stop, code, out, err = s, None, "", ""
            self._merge_state()
            if stop:
                raise stop
            result, queries, fetched = self._parse(out)
            self.tools.sources.update(filter(None, (_host(u) for u in fetched)))
            if result:
                self.stats["cc_turns"] = self.stats.get("cc_turns", 0) + int(result.get("num_turns") or 0)
                self.stats["subscription_equiv_usd"] = round(
                    self.stats.get("subscription_equiv_usd", 0) + float(result.get("total_cost_usd") or 0), 4)
                self.run.web_searches = (self.run.web_searches or 0) + len(queries)
            if code not in (0, None) or not result or result.get("is_error"):
                msg = self._classify_failure(code, result, err)
                self.run.errors = (self.run.errors or []) + [f"assignment {group['id']}: {msg}"]
                self.consecutive_failures += 1
                if self.consecutive_failures >= 3:
                    raise ClaudeCodeError(f"Claude Code failed 3 assignments in a row; last error: {msg}")
            else:
                self.consecutive_failures = 0
            self.save()
            return queries
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _merge_state(self):
        path = getattr(self, "_state_file", None)
        if not path or not os.path.exists(path):
            return
        with open(path) as fh:
            st = json.load(fh)
        for k, v in (st.get("stats") or {}).items():
            if isinstance(v, (int, float)):
                self.stats[k] = self.stats.get(k, 0) + v
        self.tools.blocked.update(st.get("blocked") or [])
        self.tools.sources.update(st.get("sources") or [])
        self.tools.batch_finished = st.get("finished")
        self.tools.batch_verified = int(st.get("verified") or 0)
        if st.get("errors"):
            self.run.errors = (self.run.errors or []) + st["errors"][-10:]

    def recommendations(self, coverage_rows):
        data = json.dumps({"stats": self.stats, "blocked_sources": sorted(self.tools.blocked),
                           "assignments": coverage_rows}, default=str)[:30000]
        ask = prompts.RECOMMEND_PROMPT.format(data=data) + \
            '\n\nReply with ONLY a JSON object: {"recommendations": ["...", "...", "..."]}'
        workdir = tempfile.mkdtemp(prefix="afcc-")
        try:
            code, out, err = self._session(ask, workdir, use_tools=False)
            result, _, _ = self._parse(out)
            text = str((result or {}).get("result") or "")
            m = re.search(r"\{.*\}", text, re.S)
            return list(json.loads(m.group(0))["recommendations"])[:3] if m else []
        except (StopRun, ValueError, KeyError, TypeError) as e:
            self.run.errors = (self.run.errors or []) + [f"recommendations: {e}"[:200]]
            return []
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def _host(url):
    from ..rules import domain_of
    return domain_of(url)


def make_researcher(db, run):
    """Pick the research backend configured in Settings -> Research engine."""
    settings = appsettings.get_all(db, include_secrets=True)
    backend = settings.get("research_backend")
    if backend == "gemini":
        from .gemini import GeminiResearcher
        return GeminiResearcher(db, run, settings=settings)
    if backend == "api":
        return Researcher(db, run, settings=settings)
    return ClaudeCodeResearcher(db, run, settings=settings)
