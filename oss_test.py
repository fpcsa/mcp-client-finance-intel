import asyncio
import os
from dotenv import load_dotenv
from typing import Optional
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_nvidia_ai_endpoints import ChatNVIDIA
import prompts

load_dotenv()

# print("LIST NVIDIA MODELS: ")
# # print(ChatNVIDIA.get_available_models())

# tool_models = [
#     model for model in ChatNVIDIA.get_available_models() if model.supports_tools
# ]
# print("LIST NVIDIA MODELS: ")
# print(tool_models)

def _get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(key, default)
    if value is None:
        return None
    return value.strip()


# -------------------------------------------------------
# 0. Environment
# -------------------------------------------------------

# export NVIDIA_API_KEY="nvapi-..."
NVIDIA_API_KEY = _get_env("NVIDIA_API_KEY")
MCP_SERVER_URL = _get_env("MCP_SERVER_URL")


# -------------------------------------------------------
# 1. LLM: via Langchain & NVIDIA NIM
# -------------------------------------------------------

llm = ChatNVIDIA(
    # model="deepseek-ai/deepseek-v3.1-terminus",  # NIM model id 
    # model="qwen/qwen3-next-80b-a3b-thinking",
    model="qwen/qwen3-235b-a22b",
    api_key=NVIDIA_API_KEY,
    temperature=0.2,
    #top_p=0.7,
    # max_tokens=8192,
    max_completion_tokens=10240,
    # Enable DeepSeek "thinking" stream (NVIDIA template flag)
    # extra_body={"chat_template_kwargs": {"thinking": True}},
)


# -------------------------------------------------------
# 2. MCP client: connect to your finance-intel MCP server
# -------------------------------------------------------

# Server is exposed at http://localhost:8000/mcp
mcp_client = MultiServerMCPClient(
    {
        "finance_intel": {
            "transport": "http",
            "url": MCP_SERVER_URL,
            # Add headers/auth here for the MCP endpoint that are secured:
            # "headers": {"Authorization": "Bearer <TOKEN>"},
        }
    }
)

async def build_agent():
    """
    Build a LangChain agent that:
    - Uses NVIDIA NIM as the model
    - Uses MCP tools (quote, timeseries, analyze_asset)
    - Is wired through create_agent (new LC agents API)
    """
    # Load MCP tools → LangChain BaseTool objects
    tools = await mcp_client.get_tools()  # quote, timeseries, analyze_asset for your server

    system_prompt = prompts.OSS_SYSTEM_PROMPT

    # create_agent builds a compiled StateGraph that handles:
    # - model <-> tool-calls loop
    # - messages as a list of role/content dicts
    agent = create_agent(
        model=llm,          # we pass the ChatNVIDIA instance directly
        tools=tools,        # MCP tools from your finance-intel server
        system_prompt=system_prompt,
        debug=False,        # set True if you want verbose graph logs
        name="finance_intel_mcp_agent",
    )
    return agent


# -------------------------------------------------------
# 3. Run a sample query that should trigger MCP tools
# -------------------------------------------------------

async def main():
    agent = await build_agent()

    # Example user question that should cause:
    # - quote for current prices
    # - analyze_asset with mode="full" for indicators and risk metrics
    user_question = (
        "What's the BTC/USDT trend?"
    )

    # Agent expects {"messages": [...]} following the new LC schema
    inputs = {
        "messages": [
            {"role": "user", "content": user_question}
        ]
    }

    # Asynchronous execution; for sync you can use agent.invoke(...)
    result_state = await agent.ainvoke(inputs)

    # result_state["messages"] is the full conversation (System, User, AI, Tool, AI, ...)
    messages = result_state["messages"]

    # Get the last AI message as the final answer
    final_ai_messages = [m for m in messages if getattr(m, "type", None) == "ai"]
    if final_ai_messages:
        final_answer = final_ai_messages[-1].content
    else:
        # Fallback: just print everything
        final_answer = messages

    # Collect tool calls, tool outputs, and any returned reasoning
    tool_calls = []
    tool_outputs = []
    reasoning_chunks = []

    for msg in messages:
        msg_type = getattr(msg, "type", None)

        if msg_type == "ai":
            for call in getattr(msg, "tool_calls", []) or []:
                name = (
                    getattr(call, "name", None)
                    or (call.get("name") if isinstance(call, dict) else None)
                )
                args = getattr(call, "args", None)
                if args is None and isinstance(call, dict):
                    args = (
                        call.get("args")
                        or call.get("arguments")
                        or call.get("function", {}).get("arguments")
                    )
                tool_calls.append({"name": name, "args": args})

            ak = getattr(msg, "additional_kwargs", {}) or {}
            reasoning = ak.get("reasoning_content") or ak.get("reasoning")
            if reasoning:
                if isinstance(reasoning, list):
                    reasoning_chunks.append("\n".join(str(r) for r in reasoning))
                else:
                    reasoning_chunks.append(str(reasoning))

        elif msg_type == "tool":
            tool_outputs.append(
                {
                    "tool_call_id": getattr(msg, "tool_call_id", None),
                    "name": getattr(msg, "name", None),
                    "content": msg.content,
                }
            )

    print("\n=== FINAL ANSWER ===")
    print(final_answer)

    """

    print("\n=== TOOL CALLS ===")
    if tool_calls:
        for idx, tc in enumerate(tool_calls, 1):
            print(f"{idx}. {tc['name']}: {tc['args']}")
    else:
        print("None")

    print("\n=== TOOL OUTPUTS ===")
    if tool_outputs:
        for idx, out in enumerate(tool_outputs, 1):
            print(f"{idx}. {out['name'] or out['tool_call_id']}: {out['content']}")
    else:
        print("None")

    print("\n=== REASONING ===")
    if reasoning_chunks:
        for idx, chunk in enumerate(reasoning_chunks, 1):
            print(f"{idx}. {chunk}")
    else:
        print("None")
    """

if __name__ == "__main__":
    asyncio.run(main())

# TODO integrate this into main_oss to build a telegram bot that will be named bot_oss.py which uses the OSS model like above instead of Claude