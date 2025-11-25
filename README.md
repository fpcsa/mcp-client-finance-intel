# MCP Finance Intel Client

This project hosts two ways to interact with the Finance Intel MCP stack using Anthropic’s Claude models:

1. **CLI (`main_claude.py`)** – conversational terminal client that can run interactively or take a single prompt.
2. **Telegram bot (`bot.py`)** – exposes the same MCP-powered experience over Telegram.

Both clients ingest environment variables (via `.env`) to connect to your MCP server and Anthropic account, then translate tool calls between Claude and FastMCP.

## Requirements

- Python 3.11+
- Anthropic API key (`ANTHROPIC_API_KEY`)
- MCP server URL (`MCP_SERVER_URL`) and optional auth token (`MCP_AUTH`)
- For the bot: Telegram bot token (`TELEGRAM_BOT_TOKEN`)

Install dependencies once:

```bash
python -m venv .venv
. .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Copy `.env.example` to `.env` (or create your own) and populate the required keys:

```
TELEGRAM_BOT_TOKEN=123456:ABC
ANTHROPIC_API_KEY=sk-ant-...
MCP_SERVER_URL=https://your-mcp-host/mcp
MCP_AUTH=Bearer <token>         # optional
CLAUDE_MODEL=claude-opus-4-1    # optional override
SYSTEM_PROMPT="Your custom instruction"
TELEGRAM_ALLOWED_USER_IDS=12345 # optional comma-separated list
```

## Running locally

CLI single prompt:

```bash
python main_claude.py "Analyze the latest TSLA earnings."
```

CLI interactive session:

```bash
python main_claude.py
```

Telegram bot:

```bash
python bot.py
```

## Docker

A minimal Dockerfile is provided for the Telegram bot.

Build the image:

```bash
docker build -t finance-mcp-bot .
```

Run with your environment variables (ensure `.env` contains only what you want to share):

```bash
docker run -d --name finance-mcp-bot --env-file .env finance-mcp-bot
```

Alternatively set variables explicitly:

```bash
docker run -d \
  --name finance-mcp-bot \
  -e TELEGRAM_BOT_TOKEN=123:abc \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e MCP_SERVER_URL=https://... \
  finance-mcp-bot
```

Tail logs in real time:

```bash
docker logs -f finance-mcp-bot
```

### Talking to a host MCP server

If your MCP server runs on the host machine (e.g., `http://localhost:8000/mcp`), containers can’t reach `localhost` by default. Either:

1. Point `MCP_SERVER_URL` to the host gateway, for example `http://host.docker.internal:8000/mcp` on Docker Desktop, or your LAN IP.
2. Run the bot with host networking so `localhost` resolves the same as on the host:

   ```bash
   docker run -d --network host --env-file .env --name finance-mcp-bot finance-mcp-bot
   ```

Choose whichever option fits your environment.

## Notes

- The CLI and bot maintain lightweight chat histories per session/chat, trimming to the last ~20 turns.
- `SYSTEM_PROMPT` is optional but recommended to keep Claude focused on MCP Finance workflows.
- The Telegram bot can be restricted to specific user IDs via `TELEGRAM_ALLOWED_USER_IDS`.

## ☕ Support the Project

If you enjoy using **mcp-client-finance-intel** and want to help me keep building,  
you can support me here:

<a href="https://buymeacoffee.com/fpcsa" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" width="180" />
</a>

👉 [Buy Me a Coffee](https://buymeacoffee.com/fpcsa)