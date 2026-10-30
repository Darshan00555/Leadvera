#!/usr/bin/env python3
"""Stand-in for the `claude` CLI in tests. Behaves like `claude -p --output-format stream-json`:
reads the prompt from stdin, starts the MCP server from --mcp-config exactly as Claude Code would,
calls tools through MCP, and prints stream-json events. FAKE_CLAUDE_MODE selects the scenario."""
import asyncio
import json
import os
import sys


def emit(ev):
    print(json.dumps(ev), flush=True)


async def call_tools(cfg_path, calls):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    srv = json.load(open(cfg_path))["mcpServers"]["afleads"]
    params = StdioServerParameters(command=srv["command"], args=srv["args"], env={**os.environ, **srv["env"]})
    out = []
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            out.append(sorted(names))
            for name, args in calls:
                res = await s.call_tool(name, args)
                out.append(res.content[0].text)
    return out


def main():
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps({"argv": argv, "prompt": prompt[:200],
                                 "env": {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                                                                        "ANTHROPIC_BASE_URL")}}) + "\n")
    mode = os.environ.get("FAKE_CLAUDE_MODE", "research")
    if "--mcp-config" not in argv:  # the tool-less recommendations call
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "total_cost_usd": 0.01,
              "result": '{"recommendations": ["r1", "r2", "r3"]}'})
        return
    if mode == "limit":
        emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
              "result": "Claude AI usage limit reached|1793120000"})
        return
    if mode == "auth":
        print("Invalid API key · Please run /login", file=sys.stderr)
        sys.exit(1)
    cfg = argv[argv.index("--mcp-config") + 1]
    candidate = json.loads(os.environ["FAKE_CANDIDATE"])
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "fake query"}},
        {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://www.testco.example/terms"}}]}})
    results = asyncio.run(call_tools(cfg, [
        ("check_known", {"name": candidate["indian_legal_name"]}),
        ("submit_candidate", {"candidate": candidate}),
        ("finish_batch", {"summary": "done", "blocked_sources": ["icis.corp.delaware.gov - CAPTCHA"]}),
    ]))
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": json.dumps(results)}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 7, "total_cost_usd": 0.42,
          "result": "Recorded 1 company."})


if __name__ == "__main__":
    main()
