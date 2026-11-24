#!/usr/bin/env python3
"""PreInvocation lifecycle hook for Context Card window binding.

Records each active window's ground-truth (conversationId, workspacePath) for
server.py, and injects a one-shot bind instruction ONLY when:
  1. A conversation is past Turn 1 (resumed session / missed Turn 1 / after app
     restart) and has not yet opened Context Card in the current server process;
  2. Turn 1 has large workspace rule files (>40 KB) that risk evicting the
     plugin's rules/AGENTS.md from the 20K rules budget; or
  3. The user recently opened Context Card manually via '+' (auto-matched mode)
     and sends a message in a window not yet explicitly locked.
Never fires inside subagents or mid-tool-loop (invocationNum > 1).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

SAFE_CID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
import core  # noqa: E402
import habits  # noqa: E402

RUNTIME_DB_PATH = habits.DATA_DIR / "runtime_state.db"
SUMMARIES_DB = habits.SUMMARIES_DB
CONFIG_DIR = Path.home() / ".gemini" / "config"


def _is_subagent(cid: str) -> bool:
    return core.is_subagent(cid, SUMMARIES_DB)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _rules_bytes_near_budget(ws_path: str) -> bool:
    total = 0
    candidates: list[Path] = [CONFIG_DIR / "GEMINI.md", CONFIG_DIR / "AGENTS.md"]
    if ws_path:
        ws = Path(ws_path)
        for rel in ("AGENTS.md", "GEMINI.md", "CLAUDE.md"):
            candidates.append(ws / rel)
        claude_dir = ws / ".claude"
        if claude_dir.is_dir():
            candidates.extend(claude_dir.glob("*CLAUDE*.md"))
        rules_dir = ws / ".agents" / "rules"
        if rules_dir.is_dir():
            candidates.extend(rules_dir.glob("*.md"))
    seen: set[str] = set()
    for p in candidates:
        try:
            if not p.is_file():
                continue
            rp = str(p.resolve())
            if rp in seen:
                continue
            seen.add(rp)
            total += p.stat().st_size
        except OSError:
            continue
    return total >= 40_000


def main() -> None:
    if os.environ.get("CONTEXT_CARD_INTERNAL_SUBPROCESS") == "1":
        print("{}")
        return

    raw_in = sys.stdin.read()
    payload = json.loads(raw_in) if raw_in.strip() else {}

    tpath_in = str(payload.get("transcriptPath") or "")
    cwd_in = str(payload.get("cwd") or "")
    if "antigravity-cli" in tpath_in or cwd_in.startswith("/tmp/"):
        print("{}")
        return

    cid = str(payload.get("conversationId") or "").strip()
    if not cid or ".." in cid or not SAFE_CID_RE.match(cid) or _is_subagent(cid):
        print("{}")
        return

    inv_num = int(payload.get("invocationNum") or 1)
    init_steps = int(payload.get("initialNumSteps") or 0)

    ws_paths = payload.get("workspacePaths") or []
    ws_path = ""
    if isinstance(ws_paths, list) and ws_paths:
        ws_path = str(ws_paths[0] or "").strip()
    if not ws_path:
        ws_path = str(payload.get("cwd") or "").strip()

    now = time.time()
    RUNTIME_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(RUNTIME_DB_PATH), timeout=1.2)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS server_life (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                pid INTEGER NOT NULL,
                started_at REAL NOT NULL,
                unconfirmed_auto_ts REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS session_bindings (
                cid TEXT PRIMARY KEY,
                workspace_path TEXT NOT NULL DEFAULT '',
                hook_seen_ts REAL NOT NULL DEFAULT 0,
                explicit_server_pid INTEGER NOT NULL DEFAULT 0,
                explicit_bound_ts REAL NOT NULL DEFAULT 0,
                hook_reminded_ts REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pane_clients (
                client_id TEXT PRIMARY KEY,
                cid TEXT NOT NULL,
                bind_mode TEXT NOT NULL DEFAULT 'explicit',
                updated_ts REAL NOT NULL
            );
            """
        )

        conn.execute(
            """
            INSERT INTO session_bindings (cid, workspace_path, hook_seen_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(cid) DO UPDATE SET
                workspace_path = CASE
                    WHEN excluded.workspace_path != '' THEN excluded.workspace_path
                    ELSE session_bindings.workspace_path
                END,
                hook_seen_ts = excluded.hook_seen_ts
            """,
            (cid, ws_path, now),
        )
        conn.commit()

        # Never inject mid-tool-loop within an assistant turn
        if inv_num > 1:
            print("{}")
            return

        srv_row = conn.execute(
            "SELECT pid, unconfirmed_auto_ts FROM server_life WHERE id = 1"
        ).fetchone()
        srv_pid = int(srv_row[0]) if srv_row else 0
        unconfirmed_auto_ts = float(srv_row[1] or 0.0) if srv_row else 0.0
        srv_alive = _pid_alive(srv_pid)

        bind_row = conn.execute(
            "SELECT explicit_server_pid, hook_reminded_ts FROM session_bindings WHERE cid = ?",
            (cid,),
        ).fetchone()
        explicit_srv_pid = int(bind_row[0] or 0) if bind_row else 0
        reminded_ts = float(bind_row[1] or 0.0) if bind_row else 0.0

        if (now - reminded_ts) < 45.0:
            print("{}")
            return

        has_explicit_pane = (
            conn.execute(
                "SELECT 1 FROM pane_clients WHERE cid = ? AND bind_mode = 'explicit' AND updated_ts >= ? LIMIT 1",
                (cid, now - 8.0),
            ).fetchone()
            is not None
        )

        opened_in_cur_life = (
            (srv_alive and srv_pid > 0 and (explicit_srv_pid == srv_pid))
            or (not srv_alive and explicit_srv_pid > 0 and _pid_alive(explicit_srv_pid))
        )
        pending_manual_rebind = (
            unconfirmed_auto_ts > 0.0
            and (now - unconfirmed_auto_ts) <= 300.0
            and not has_explicit_pane
        )

        should_inject = False
        if pending_manual_rebind:
            should_inject = True
        elif not opened_in_cur_life and not has_explicit_pane:
            if init_steps > 2 or _rules_bytes_near_budget(ws_path):
                should_inject = True

        if not should_inject:
            print("{}")
            return

        conn.execute(
            "UPDATE session_bindings SET hook_reminded_ts = ? WHERE cid = ?",
            (now, cid),
        )
        conn.commit()
    finally:
        conn.close()

    args_json = json.dumps({"conversation_id": cid})
    msg = (
        f'Context Card auto-bind: call call_mcp_tool(ServerName="context-card_monitor", '
        f'ToolName="open_context_card", Arguments={args_json}) '
        f"once in parallel with your response or first tool call."
    )
    print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
