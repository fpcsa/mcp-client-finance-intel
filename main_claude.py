import argparse
import asyncio
import json
import os
import logging
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from anthropic import Anthropic

from fastmcp import Client  # FastMCP client


load_dotenv()


def _get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(key, default)
    if value is None:
        return None
    return value.strip()


LOG_LEVEL = (_get_env("LOG_LEVEL", "INFO") or "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = _get_env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _get_env("CLAUDE_MODEL", "claude-sonnet-4-5") or "claude-sonnet-4-5"
SYSTEM_PROMPT = _get_env("SYSTEM_PROMPT") or ""

MCP_SERVER_URL = _get_env("MCP_SERVER_URL")


def _normalize_mcp_auth(raw_auth: Optional[str]) -> Optional[str]:
    """
    FastMCP Client.Auth expects the raw bearer token; support env strings that
    already include the 'Bearer ' prefix for convenience.
    """
    if not raw_auth:
        return None
    value = raw_auth.strip()
    if not value:
        return None
    if value.lower() == "oauth":
        return "oauth"
    if value.lower().startswith("bearer "):
        value = value.split(" ", 1)[1].strip()
    return value or None


MCP_AUTH = _normalize_mcp_auth(_get_env("MCP_AUTH"))

if not ANTHROPIC_API_KEY or not MCP_SERVER_URL:
    raise RuntimeError("Missing required env vars. Check ANTHROPIC_API_KEY and MCP_SERVER_URL")


class MCPBridge:
    def __init__(self, url: str, auth: Optional[str] = None):
        self.url = url
        self.auth = auth
        self.client = Client(url, auth=auth) if auth else Client(url)
        self._connected = False
        self._cached_tools = None  # list[mcp.types.Tool]

    async def connect(self):
        if not self._connected:
            await self.client.__aenter__()
            self._connected = True

    async def close(self):
        if self._connected:
            await self.client.__aexit__(None, None, None)
            self._connected = False

    async def list_tools(self):
        self._cached_tools = await self.client.list_tools()
        return self._cached_tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]):
        return await self.client.call_tool(name, arguments)

    async def claude_tool_schemas_old(self):
        tools = await self.list_tools()
        schemas = []
        for t in tools:
            name = getattr(t, "name", None)
            desc = getattr(t, "description", "") or ""
            input_schema = (
                getattr(t, "input_schema", None)
                or getattr(t, "inputSchema", None)
                or {}
            )
            if not name:
                continue

            schemas.append(
                {
                    "name": name,
                    "description": desc,
                    "input_schema": input_schema,
                }
            )
        return schemas

    async def claude_tool_schemas(self):
        tools = await self.list_tools()
        schemas = []

        for t in tools:
            name = getattr(t, "name", None) or t.get("name")
            desc = getattr(t, "description", "") or t.get("description", "")

            input_schema = (
                getattr(t, "inputSchema", None)
                or getattr(t, "input_schema", None)
                or t.get("inputSchema")
                or t.get("input_schema")
                or {}
            )

            if not name:
                continue

            schemas.append({
                "name": name,
                "description": desc,
                "input_schema": input_schema,   # NOTE: keep wrapper "input"
            })

        return schemas


mcp_bridge = MCPBridge(MCP_SERVER_URL, MCP_AUTH)

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

histories: Dict[str, List[Dict[str, Any]]] = {}


async def ask_claude_with_mcp(session_id: str, user_text: str) -> str:
    tools = await mcp_bridge.claude_tool_schemas()

    history = histories.get(session_id, [])

    history = history + [{"role": "user", "content": user_text}]

    while True:
        kwargs = {
            "model": CLAUDE_MODEL,
            "max_tokens": 10000,
            "tools": tools,
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "messages": history,
        }
        if SYSTEM_PROMPT:
            kwargs["system"] = SYSTEM_PROMPT

        resp = anthropic_client.messages.create(**kwargs)

        stop_reason = getattr(resp, "stop_reason", None)
        content_blocks = resp.content

        assistant_msg = {
            "role": "assistant",
            "content": [cb.model_dump() if hasattr(cb, "model_dump") else cb for cb in content_blocks],
        }
        history.append(assistant_msg)

        if stop_reason != "tool_use":
            texts = []
            for cb in content_blocks:
                cb_type = cb.type if hasattr(cb, "type") else cb.get("type")
                if cb_type == "text":
                    texts.append(cb.text if hasattr(cb, "text") else cb.get("text", ""))
            final = "\n".join(texts).strip() or "(no text returned)"
            histories[session_id] = history[-20:]
            return final

        tool_results_blocks = []
        for cb in content_blocks:
            cb_type = cb.type if hasattr(cb, "type") else cb.get("type")
            if cb_type != "tool_use":
                continue

            tool_name = cb.name if hasattr(cb, "name") else cb.get("name")
            tool_args = cb.input if hasattr(cb, "input") else cb.get("input", {})
            tool_use_id = cb.id if hasattr(cb, "id") else cb.get("id")

            try:
                logger.info("Tool %s (id=%s) input: %s", tool_name, tool_use_id, json.dumps(tool_args, ensure_ascii=False))
                mcp_result = await mcp_bridge.call_tool(tool_name, tool_args)
                logger.info("Tool %s (id=%s) raw response: %r", tool_name, tool_use_id, mcp_result)

                def _to_text_segment(segment: Any) -> Dict[str, Any]:
                    if hasattr(segment, "model_dump"):
                        segment = segment.model_dump()
                    if isinstance(segment, dict):
                        seg_type = segment.get("type")
                        if seg_type == "text" and "text" in segment:
                            return {"type": "text", "text": str(segment.get("text", ""))}
                        try:
                            return {"type": "text", "text": json.dumps(segment, ensure_ascii=False)}
                        except TypeError:
                            return {"type": "text", "text": str(segment)}
                    if isinstance(segment, str):
                        return {"type": "text", "text": segment}
                    try:
                        return {"type": "text", "text": json.dumps(segment, ensure_ascii=False)}
                    except TypeError:
                        return {"type": "text", "text": repr(segment)}

                content_segments = []
                raw_content = getattr(mcp_result, "content", None)
                if isinstance(raw_content, list):
                    for seg in raw_content:
                        content_segments.append(_to_text_segment(seg))

                if not content_segments:
                    payload = getattr(mcp_result, "data", None) or getattr(mcp_result, "structured_content", None) or getattr(mcp_result, "content", None) or mcp_result
                    if isinstance(payload, str):
                        text_payload = payload
                    else:
                        try:
                            text_payload = json.dumps(payload, ensure_ascii=False)
                        except TypeError:
                            text_payload = repr(payload)
                    content_segments = [{"type": "text", "text": text_payload}]

                tool_results_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content_segments,
                    "is_error": False,
                })
            except Exception as e:
                tool_results_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": f"Tool error: {repr(e)}"}],
                    "is_error": True,
                })

        history.append(
            {
                "role": "user",
                "content": tool_results_blocks,
            }
        )


async def run_single(session_id: str, prompt: str) -> None:
    answer = await ask_claude_with_mcp(session_id, prompt)
    print(answer)


async def run_interactive(session_id: str) -> None:
    print("Interactive CLI session. Type 'exit' or 'quit' to stop.")
    while True:
        try:
            user_text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_text:
            continue
        if user_text.lower() in {"exit", "quit"}:
            break
        print("Claude is thinking...")
        try:
            answer = await ask_claude_with_mcp(session_id, user_text)
            print(f"Claude: {answer}\n")
        except Exception as exc:
            print(f"Error: {exc}")
    print("Session ended.")


async def main():
    parser = argparse.ArgumentParser(description="Direct Claude CLI client using MCP tools.")
    parser.add_argument("prompt", nargs="?", help="Single prompt to send to Claude. Defaults to interactive mode.")
    parser.add_argument("--session", default="cli", help="Optional session id for maintaining conversation context.")
    args = parser.parse_args()

    await mcp_bridge.connect()
    try:
        if args.prompt:
            await run_single(args.session, args.prompt.strip())
        else:
            await run_interactive(args.session)
    finally:
        await mcp_bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
