#!/usr/bin/env python3
"""Stop lifecycle hook for Antigravity CLI Context Card suggestion pre-warming.

Strictly opt-in: runs ONLY when the user has explicitly installed the Context Card
CLI statusLine (`cli_setup.py install`, without `--no-suggest`) AND configured LLM
suggestion auth. Spawns a detached background worker (<20ms hook latency) so the
suggested reply is already warmed by the time the TUI transitions to idle.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import cli_statusline  # noqa: E402
import core  # noqa: E402
import habits  # noqa: E402


def main() -> None:
    if os.environ.get("CONTEXT_CARD_INTERNAL_SUBPROCESS") == "1":
        print("{}")
        return

    raw_in = sys.stdin.read()
    payload = json.loads(raw_in) if raw_in.strip() else {}
    if not isinstance(payload, dict):
        print("{}")
        return

    tpath_str = str(payload.get("transcriptPath") or "").strip()
    if "antigravity-cli" not in tpath_str:
        print("{}")
        return

    cwd_in = str(payload.get("cwd") or "")
    if cwd_in.startswith("/tmp/"):
        print("{}")
        return

    # Strict opt-in guard: never send CLI conversations to an LLM unless user installed CLI statusLine
    installed, suggest_enabled = core.is_cli_statusline_installed()
    if not installed or not suggest_enabled:
        print("{}")
        return

    term_reason = str(payload.get("terminationReason") or "").strip()
    if term_reason in ("USER_CANCELED", "ERROR"):
        print("{}")
        return

    data_dir = habits.use_cli_context()
    cid = core.sanitize_cid(str(payload.get("conversationId") or ""))
    if not cid or core.is_subagent(cid, habits.SUMMARIES_DB):
        print("{}")
        return

    tpath = Path(tpath_str)
    if not tpath.is_file():
        print("{}")
        return

    prefs = cli_statusline._load_cli_prefs(data_dir)
    if not prefs.get("auto_suggest", True) or not habits.has_llm_configured():
        print("{}")
        return

    # Read transcript rows to extract the current turn key
    rows = []
    try:
        with tpath.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    rows.append(json.loads(s))
                except json.JSONDecodeError:
                    continue
    except OSError:
        print("{}")
        return

    turn_info = core.detect_turn_state(cid, rows)
    turn_key = str(turn_info.get("turn_key") or turn_info.get("latest_reply_turn_key") or "")
    if not turn_key:
        print("{}")
        return

    ws_paths = payload.get("workspacePaths") or []
    ws_hint = str(ws_paths[0] or "").strip() if isinstance(ws_paths, list) and ws_paths else cwd_in
    raw_pane_id = os.environ.get("TMUX_PANE") or ""

    # Bind current tmux pane to (cid, turn_key) so `tmux_fill.py` can match immediately
    if raw_pane_id:
        cli_statusline.sync_pane_state(data_dir, raw_pane_id, cid, turn_key, "", [], stale=False)

    cli_statusline.spawn_suggestion_worker(
        cid=cid,
        tpath=tpath,
        turn_key=turn_key,
        ws_hint=ws_hint,
        data_dir=data_dir,
        raw_pane_id=raw_pane_id,
    )
    print("{}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
