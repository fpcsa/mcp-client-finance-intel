# bot_oss.py
import os
import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_nvidia_ai_endpoints import ChatNVIDIA

import prompts


load_dotenv()


def _get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(key, default)
    if value is None:
        return None
    return value.strip()

def _parse_float(raw: Optional[str], default: float) -> float:
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default

def _parse_int(raw: Optional[str], default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


LOG_LEVEL = (_get_env("LOG_LEVEL", "INFO") or "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = _get_env("TELEGRAM_BOT_TOKEN")
NVIDIA_API_KEY = _get_env("NVIDIA_API_KEY")
NVIDIA_MODEL = _get_env("NVIDIA_MODEL", "qwen/qwen3-235b-a22b") or "qwen/qwen3-235b-a22b"
SYSTEM_PROMPT = _get_env("OSS_SYSTEM_PROMPT") or prompts.OSS_SYSTEM_PROMPT.strip()
TEMPERATURE = _parse_float(_get_env("OSS_TEMPERATURE"), 0.2)
MAX_COMPLETION_TOKENS = _parse_int(_get_env("OSS_MAX_COMPLETION_TOKENS"), 10240)
_allowed_ids_raw = _get_env("TELEGRAM_ALLOWED_USER_IDS", "") or ""

MCP_SERVER_URL = _get_env("MCP_SERVER_URL")


def _normalize_mcp_auth(raw_auth: Optional[str]) -> Optional[str]:
    """
    Normalize MCP auth so we can accept either a raw token or a full
    'Bearer <token>' string.
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

if not TELEGRAM_BOT_TOKEN or not NVIDIA_API_KEY or not MCP_SERVER_URL:
    raise RuntimeError("Missing required env vars. Check TELEGRAM_BOT_TOKEN, NVIDIA_API_KEY, MCP_SERVER_URL")


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
# MCP bridge via LangChain
# -------------------------
mcp_server_config: Dict[str, Any] = {
    "transport": "http",
    "url": MCP_SERVER_URL,
}

if MCP_AUTH:
    mcp_server_config["headers"] = {"Authorization": f"Bearer {MCP_AUTH}"}

mcp_client = MultiServerMCPClient(
    {
        "finance_intel": mcp_server_config,
    }
)

# -------------------------
# OSS model (NVIDIA NIM) setup
# -------------------------
llm = ChatNVIDIA(
    model=NVIDIA_MODEL,
    api_key=NVIDIA_API_KEY,
    temperature=TEMPERATURE,
    max_completion_tokens=MAX_COMPLETION_TOKENS,
)

class OSSAgentManager:
    """Lazy-load the LangChain agent so we only fetch tools once."""

    def __init__(self):
        self._agent = None
        self._lock = asyncio.Lock()

    async def get_agent(self):
        if self._agent is None:
            async with self._lock:
                if self._agent is None:
                    tools = await mcp_client.get_tools()
                    logger.info("Loaded %s MCP tools", len(tools) if hasattr(tools, "__len__") else "unknown")
                    self._agent = create_agent(
                        model=llm,
                        tools=tools,
                        system_prompt=SYSTEM_PROMPT,
                        debug=False,
                        name="finance_intel_mcp_agent",
                    )
        return self._agent


agent_manager = OSSAgentManager()

# per-chat state (simple memory)
histories: Dict[int, List[Any]] = {}


async def _safe_markdown_reply(message: types.Message, text: str) -> types.Message:
    """
    Send replies using the bot's Markdown parse mode, but gracefully fall back to plain text
    when Telegram rejects the content (e.g., unbalanced formatting).
    """
    try:
        return await message.reply(text)
    except TelegramBadRequest as exc:
        details = str(exc).lower()
        if "can't parse entities" not in details:
            raise
        logger.warning("Telegram refused Markdown content, resending as plain text: %s", exc)
        return await message.reply(text, parse_mode=None)


async def ask_oss_with_mcp(chat_id: int, user_text: str) -> str:
    """
    Send the user message to the LangChain agent backed by the OSS model and MCP tools.
    Maintains a short per-chat history for context.
    """
    agent = await agent_manager.get_agent()
    history = histories.get(chat_id, [])

    inputs = {"messages": history + [HumanMessage(content=user_text)]}
    result_state = await agent.ainvoke(inputs)

    messages = result_state["messages"] if isinstance(result_state, dict) else result_state
    if not isinstance(messages, list):
        raise RuntimeError("Unexpected agent output format")

    final_answer = "(no text returned)"
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "ai":
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                joined = "".join(str(part) for part in content if part is not None).strip()
                if joined:
                    final_answer = joined
                    break
            else:
                text = str(content).strip()
                if text:
                    final_answer = text
                    break

    histories[chat_id] = messages[-20:]  # keep last N turns
    return final_answer


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
        answer = await ask_oss_with_mcp(chat_id, user_text)
        await _safe_markdown_reply(message, answer)
    except Exception as e:
        logger.exception("Failed to create response for chat_id=%s", chat_id)
        await _safe_markdown_reply(message, f"Sorry, something went wrong:\n`{repr(e)}`")


async def main():
    # Build agent up front so we fail fast if tools/model are unavailable.
    await agent_manager.get_agent()
    # Long polling loop.
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
