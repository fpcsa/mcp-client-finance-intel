# bot.py
import os
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

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

TELEGRAM_BOT_TOKEN = _get_env("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = _get_env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _get_env("CLAUDE_MODEL", "claude-sonnet-4-5") or "claude-sonnet-4-5"
SYSTEM_PROMPT = _get_env("SYSTEM_PROMPT") or ""
_allowed_ids_raw = _get_env("TELEGRAM_ALLOWED_USER_IDS", "") or ""

MCP_SERVER_URL = _get_env("MCP_SERVER_URL")


def _normalize_mcp_auth(raw_auth: Optional[str]) -> Optional[str]:
    """
    FastMCP's Client expects just the bearer token. Allow env var to contain
    either the token itself or a full 'Bearer <token>' header value.
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

if not TELEGRAM_BOT_TOKEN or not ANTHROPIC_API_KEY or not MCP_SERVER_URL:
    raise RuntimeError("Missing required env vars. Check TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, MCP_SERVER_URL")

def _parse_allowed_ids(raw: str) -> Set[int]:
    ids: Set[int] = set()
    if not raw:
        return ids
    for chunk in raw.split(","):
        candidate = chunk.strip()
        if not candidate:
            continue
        try:
            ids.add(int(candidate))
        except ValueError:
            logger.warning("Ignoring invalid TELEGRAM_ALLOWED_USER_IDS entry: %r", candidate)
    return ids


ALLOWED_TELEGRAM_USER_IDS = _parse_allowed_ids(_allowed_ids_raw)

def _is_user_allowed(user_id: int) -> bool:
    return not ALLOWED_TELEGRAM_USER_IDS or user_id in ALLOWED_TELEGRAM_USER_IDS


# -------------------------
# MCP bridge (FastMCP client)
# -------------------------
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
        # FastMCP supports list_tools() when connected. :contentReference[oaicite:6]{index=6}
        self._cached_tools = await self.client.list_tools()
        return self._cached_tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]):
        # call_tool() executes the MCP tool. :contentReference[oaicite:7]{index=7}
        return await self.client.call_tool(name, arguments)

    async def claude_tool_schemas_old(self):
        """
        Convert MCP tool definitions to Claude tool JSON Schemas.
        Claude expects: [{name, description, input_schema}, ...] :contentReference[oaicite:8]{index=8}
        """
        tools = await self.list_tools()
        schemas = []
        for t in tools:
            # Be defensive about attribute names across SDK versions
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
        """
        Use MCP inputSchema as-is, only convert casing for Claude.
        This ensures Claude will emit {"input": {...}} which your tools require.
        """
        tools = await self.list_tools()
        schemas = []

        for t in tools:
            # FastMCP Tool objects can expose either snake or camel fields depending on version.
            name = getattr(t, "name", None) or t.get("name")
            desc = getattr(t, "description", "") or t.get("description", "")

            # MCP uses inputSchema; Claude wants input_schema
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

# -------------------------
# Claude tool-calling loop
# -------------------------
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# per-chat state (simple memory)
histories: Dict[int, List[Dict[str, Any]]] = {}


async def _safe_markdown_reply(message: types.Message, text: str) -> types.Message:
    """
    Send replies using the bot's Markdown parse mode, but gracefully fall back to plain text
    when Telegram rejects the content (e.g., unbalanced formatting from Claude).
    """
    try:
        return await message.reply(text)
    except TelegramBadRequest as exc:
        details = str(exc).lower()
        if "can't parse entities" not in details:
            raise
        logger.warning("Telegram refused Markdown content, resending as plain text: %s", exc)
        return await message.reply(text, parse_mode=None)


async def ask_claude_with_mcp(chat_id: int, user_text: str) -> str:
    """
    1) Send user message + MCP tools to Claude.
    2) If Claude emits tool_use blocks, execute via MCP and send tool_result blocks.
    3) Repeat until final text.
    """
    tools = await mcp_bridge.claude_tool_schemas()

    history = histories.get(chat_id, [])

    # append user turn
    history = history + [{"role": "user", "content": user_text}]

    while True:
        kwargs = {
            "model": CLAUDE_MODEL,
            "max_tokens": 10000,
            "tools": tools,
            # default tool_choice is auto :contentReference[oaicite:9]{index=9}
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "messages": history,
        }
        if SYSTEM_PROMPT:
            kwargs["system"] = SYSTEM_PROMPT

        resp = anthropic_client.messages.create(**kwargs)

        # Claude returns content blocks; tool use stops with stop_reason=tool_use :contentReference[oaicite:10]{index=10}
        stop_reason = getattr(resp, "stop_reason", None)
        content_blocks = resp.content

        # Save assistant message to history
        assistant_msg = {
            "role": "assistant",
            "content": [cb.model_dump() if hasattr(cb, "model_dump") else cb for cb in content_blocks],
        }
        history.append(assistant_msg)

        if stop_reason != "tool_use":
            # gather all text blocks
            texts = []
            for cb in content_blocks:
                cb_type = cb.type if hasattr(cb, "type") else cb.get("type")
                if cb_type == "text":
                    texts.append(cb.text if hasattr(cb, "text") else cb.get("text", ""))
            final = "\n".join(texts).strip() or "(no text returned)"
            histories[chat_id] = history[-20:]  # keep last N turns
            return final

        # Otherwise, execute all tool_use blocks
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
                # IMPORTANT: tool_args already includes {"input": {...}}
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


        # Per Claude rules:
        # - tool_result blocks must immediately follow the assistant tool_use message
        # - tool_result blocks must come first in user content :contentReference[oaicite:11]{index=11}
        user_tool_msg = {
            "role": "user",
            "content": tool_results_blocks,
        }
        history.append(user_tool_msg)

# -------------------------
# Telegram long-polling bot
# -------------------------

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
dp = Dispatcher()

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    if not _is_user_allowed(message.from_user.id):
        await message.answer("Sorry, this bot is restricted to authorized users.")
        return
    await message.answer(
        "Hi! Send me a finance question (e.g., *analyze AAPL*, *BTC 1h candles*, *risk of TSLA*).\n"
        "I'll call the MCP finance tools when needed."
    )

@dp.message()
async def text_handler(message: types.Message):
    if not _is_user_allowed(message.from_user.id):
        logger.info("Blocked unauthorized user_id=%s", message.from_user.id)
        await message.reply("Sorry, this bot is restricted to authorized users.")
        return
    chat_id = message.chat.id
    user_text = message.text.strip()

    await message.chat.do("typing")

    try:
        answer = await ask_claude_with_mcp(chat_id, user_text)
        await _safe_markdown_reply(message, answer)
    except Exception as e:
        logger.exception("Failed to create response for chat_id=%s", chat_id)
        await _safe_markdown_reply(message, f"Sorry, something went wrong:\n`{repr(e)}`")


async def main():
    await mcp_bridge.connect()
    try:
        # Long polling loop. :contentReference[oaicite:12]{index=12}
        await dp.start_polling(bot)
    finally:
        await mcp_bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
