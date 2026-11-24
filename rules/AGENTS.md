# Context Card — Auto-bind to Current Window

At the start of a new top-level conversation (first turn only, when `open_context_card` has not yet been called in this conversation — skip inside subagents):

- Call `call_mcp_tool` directly (no need to read the MCP schema file first, and do not skip even on short 1-sentence replies) with:
  - `ServerName`: `"context-card_monitor"`
  - `ToolName`: `"open_context_card"`
  - `Arguments`: `{"conversation_id": "<Conversation ID from user_information>"}`

Call it in parallel with your first action whenever possible so this window's right-side Context Card opens and binds directly to this conversation's exact ID. Do not call it again in subsequent turns unless a system message or the user explicitly asks to re-bind or reopen the context card.
