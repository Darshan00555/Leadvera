"""Google Gemini research backend: uses Gemini 2.0 / 1.5 models with Function Calling for lead research."""
import copy
import json
import logging
import re
import time
from datetime import date

from google import genai
from google.genai import types

from .. import appsettings
from ..config import Config
from . import prompts
from .agent import Researcher, StopRun
from .tools import CLIENT_TOOLS, WEB_SEARCH_TOOL

log = logging.getLogger("research.gemini")

# USD per million tokens (input, output)
GEMINI_PRICES = {
    "gemini-3.1-pro-preview": (1.25, 5.00),
    "gemini-pro-latest": (1.25, 5.00),
    "gemini-3.8-flash": (0.10, 0.40),
    "gemini-3.5-flash": (0.075, 0.30),
    "gemini-3.1-flash-lite": (0.05, 0.20),
    "gemini-flash-latest": (0.10, 0.40),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
}


def sanitize_schema_for_gemini(schema):
    """Convert OpenAPI 3.1 schema (e.g. ['string', 'null']) to Gemini-compatible OpenAPI 3.0 schema."""
    if not isinstance(schema, dict):
        return schema
    s = copy.deepcopy(schema)
    s.pop("additionalProperties", None)
    s.pop("additional_properties", None)
    s.pop("$schema", None)
    if isinstance(s.get("type"), list):
        types_list = s["type"]
        if "null" in types_list:
            s["nullable"] = True
            non_null = [t for t in types_list if t != "null"]
            s["type"] = non_null[0] if non_null else "string"
        else:
            s["type"] = types_list[0]
    if "properties" in s and isinstance(s["properties"], dict):
        s["properties"] = {k: sanitize_schema_for_gemini(v) for k, v in s["properties"].items()}
    if "items" in s:
        s["items"] = sanitize_schema_for_gemini(s["items"])
    return s


class GeminiResearcher(Researcher):
    backend = "gemini"

    def __init__(self, db, run, settings=None):
        settings = settings or appsettings.get_all(db, include_secrets=True)
        super().__init__(db, run, client=object(), settings=settings)
        self.api_key = Config.GEMINI_API_KEY or settings.get("gemini_api_key") or ""
        if not self.api_key:
            raise RuntimeError("No Google Gemini API key configured. Enter it in Settings -> Research engine, "
                               "or set GEMINI_API_KEY environment variable. Get a free key at aistudio.google.com.")
        self.model = settings.get("gemini_model") or "gemini-2.0-flash"
        self.client = genai.Client(api_key=self.api_key)
        self._gemini_tools = self._build_tools()

    def _build_tools(self):
        declarations = []
        for t in CLIENT_TOOLS + [WEB_SEARCH_TOOL]:
            decl = types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=sanitize_schema_for_gemini(t.get("input_schema", {})),
            )
            declarations.append(decl)
        return [types.Tool(function_declarations=declarations)]

    def _account_gemini(self, usage):
        if not usage:
            return
        inp = getattr(usage, "prompt_token_count", 0) or 0
        out = getattr(usage, "candidates_token_count", 0) or 0
        p_in, p_out = GEMINI_PRICES.get(self.model, (0.10, 0.40))
        cost = (inp * p_in + out * p_out) / 1e6
        self.run.input_tokens = (self.run.input_tokens or 0) + inp
        self.run.output_tokens = (self.run.output_tokens or 0) + out
        self.run.cost_usd = round((self.run.cost_usd or 0) + cost, 4)

    def _send_with_retry(self, chat, message, max_retries=3):
        delay = 3
        for attempt in range(max_retries + 1):
            try:
                return chat.send_message(message)
            except Exception as e:
                err_str = str(e)
                is_transient = any(code in err_str for code in (
                    "503", "504", "429", "UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "high demand"
                ))
                if is_transient and attempt < max_retries:
                    self.note(f"Gemini API transient spike ({type(e).__name__}); waiting {delay}s to retry (attempt {attempt + 1}/{max_retries})...")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

    def run_batch(self, group):
        today = date.today().isoformat()
        prompt = prompts.batch_prompt(group, today, self.leads_left())
        self.tools.batch_finished = None
        self.tools.batch_candidates = self.tools.batch_verified = 0
        queries = []

        config = types.GenerateContentConfig(
            system_instruction=prompts.SYSTEM,
            tools=self._gemini_tools,
            temperature=0.2,
            max_output_tokens=8192,
        )

        chat = self.client.chats.create(model=self.model, config=config)
        try:
            resp = self._send_with_retry(chat, prompt)
        except Exception as e:
            err_msg = f"Gemini initial batch error: {type(e).__name__}: {e}"[:300]
            self.run.errors = (self.run.errors or []) + [err_msg]
            self.note(err_msg)
            return queries

        for turn in range(int(self.settings.get("max_turns_per_batch") or 12)):
            self.check_limits()
            if hasattr(resp, "usage_metadata"):
                self._account_gemini(resp.usage_metadata)
            self.save()

            calls = resp.function_calls or []
            if not calls:
                break

            tool_parts = []
            for fc in calls:
                name = fc.name
                args = dict(fc.args or {})
                if name == "web_search":
                    q = args.get("query", "")
                    if q:
                        queries.append(q)
                        self.run.web_searches = (self.run.web_searches or 0) + 1
                result = self.tools.execute(name, args)
                tool_parts.append(types.Part.from_function_response(name=name, response={"result": result}))

            self.save()
            if self.tools.batch_finished is not None:
                break

            try:
                resp = self._send_with_retry(chat, tool_parts)
            except Exception as e:
                err_msg = f"Gemini turn error: {type(e).__name__}: {e}"[:300]
                self.run.errors = (self.run.errors or []) + [err_msg]
                self.note(err_msg)
                break

        return queries

    def recommendations(self, coverage_rows):
        data = json.dumps({"stats": self.stats, "blocked_sources": sorted(self.tools.blocked),
                           "assignments": coverage_rows}, default=str)[:30000]
        prompt = prompts.RECOMMEND_PROMPT.format(data=data) + '\n\nReply in JSON: {"recommendations": ["..."]}'
        delay = 3
        for attempt in range(3):
            try:
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                    ),
                )
                if hasattr(resp, "usage_metadata"):
                    self._account_gemini(resp.usage_metadata)
                m = re.search(r"\{.*\}", resp.text, re.S)
                return list(json.loads(m.group(0))["recommendations"])[:3] if m else []
            except Exception as e:
                err_str = str(e)
                if any(code in err_str for code in ("503", "504", "429", "UNAVAILABLE", "DEADLINE_EXCEEDED")) and attempt < 2:
                    time.sleep(delay)
                    delay *= 2
                    continue
                self.run.errors = (self.run.errors or []) + [f"recommendations: {type(e).__name__}: {e}"[:200]]
                return []
        return []
