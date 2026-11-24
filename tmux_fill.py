#!/usr/bin/env python3
"""tmux keybinding helper for Antigravity Context Card (Plan B).

Injects the bound pane's own suggested reply directly into the `agy` CLI prompt box
(literal mode, without pressing Enter so you can review/edit before sending).

Strictly pane-isolated: uses `--pane #{pane_id}` captured by tmux at keypress time
so async `run-shell -b` execution never targets another pane, never falls back across
conversations, and never fills stale suggestions while the agent is working.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent
SAFE_PANE_RE = re.compile(r"^[%A-Za-z0-9._-]{1,32}$")
CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _tmux(*args: str, timeout: float = 2.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _get_pane_info(target_pane: str = "") -> tuple[str, str]:
    cmd_args = ["display-message"]
    if target_pane:
        cmd_args.extend(["-t", target_pane])
    cmd_args.extend(["-p", "#{pane_id}\t#{pane_current_command}"])
    res = _tmux(*cmd_args)
    if res.returncode != 0 or "\t" not in res.stdout:
        return ("", "")
    pane_id, cmd = res.stdout.strip().split("\t", 1)
    return (pane_id.strip(), cmd.strip())


def _is_agy_command(cmd: str) -> bool:
    low = cmd.strip().lower()
    return low in ("agy", "agy.exe") or low.startswith("agy-")


def _sanitize_pane_id(raw: str) -> str:
    s = raw.strip()
    if not s or not SAFE_PANE_RE.match(s):
        return ""
    return s.replace("%", "p_")


def _sanitize_prompt_text(text: str) -> str:
    # Collapse newlines/tabs/control chars into single spaces so send-keys never submits Enter
    cleaned = CTRL_CHARS_RE.sub(" ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _display_msg(text: str, pane_id: str = "", duration_ms: int = 2500) -> None:
    cmd = ["display-message"]
    if pane_id:
        cmd.extend(["-t", pane_id])
    cmd.extend(["-d", str(duration_ms), text])
    _tmux(*cmd)


def _load_pane_data(data_dir: Path, pane_id: str) -> tuple[Path | None, dict[str, Any]]:
    safe = _sanitize_pane_id(pane_id)
    if not safe:
        return (None, {})
    path = data_dir / "panes" / f"{safe}.json"
    if not path.is_file():
        return (path, {})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return (path, raw)
    except Exception:
        pass
    return (path, {})


def _refresh_from_suggest_cache(data_dir: Path, pane_data: dict[str, Any], core_mod: Any) -> dict[str, Any]:
    """Pick up a ready suggestion for THIS pane's exact (cid, turn_key) if worker finished after last redraw."""
    if pane_data.get("stale"):
        return pane_data
    cid = core_mod.sanitize_cid(str(pane_data.get("cid") or ""))
    expected_tk = str(pane_data.get("turn_key") or "").strip()
    if not cid or not expected_tk:
        return pane_data
    spath = data_dir / "cli_cache" / f"suggest_{cid}.json"
    if not spath.is_file():
        return pane_data
    try:
        sdata = json.loads(spath.read_text(encoding="utf-8"))
    except Exception:
        return pane_data
    if not isinstance(sdata, dict):
        return pane_data
    if sdata.get("status") != "ready" or not sdata.get("primary"):
        return pane_data
    if str(sdata.get("turn_key") or "").strip() != expected_tk:
        return pane_data

    primary = str(sdata.get("primary") or "").strip()
    alts = [str(a).strip() for a in (sdata.get("alternatives") or []) if str(a).strip()]
    if pane_data.get("primary") == primary and list(pane_data.get("alternatives") or []) == alts:
        return pane_data
    return {
        **pane_data,
        "primary": primary,
        "alternatives": alts,
        "alt_index": 0,
        "stale": False,
        "updated_at": float(sdata.get("updated_at") or time.time()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill Context Card suggested reply into target agy tmux pane")
    parser.add_argument("action", choices=("accept", "cycle"), default="accept", nargs="?")
    parser.add_argument("--pane", default="", help="Target tmux pane_id captured at keypress time")
    parser.add_argument("--data-dir", default="", help="Override CLI plugin data directory")
    args = parser.parse_args()

    target = args.pane.strip()
    if target and not SAFE_PANE_RE.match(target):
        return 0
    pane_id, pane_cmd = _get_pane_info(target)
    if not pane_id:
        return 0

    if not _is_agy_command(pane_cmd):
        _display_msg("Context Card: active pane is not running agy", pane_id=pane_id, duration_ms=1800)
        return 0

    # Lazy-import plugin modules only when target pane is confirmed to be `agy`
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    import core  # noqa: E402
    import habits  # noqa: E402

    data_dir = habits.use_cli_context(args.data_dir or None)
    if not habits.has_llm_configured():
        _display_msg("Context Card: suggestions disabled (auth not configured)", pane_id=pane_id, duration_ms=2000)
        return 0

    pane_path, pane_data = _load_pane_data(data_dir, pane_id)

    if not pane_data or not pane_data.get("cid"):
        _display_msg("Context Card: no suggested reply ready for this pane", pane_id=pane_id, duration_ms=2000)
        return 0

    if pane_data.get("stale"):
        _display_msg("Context Card: agent is working (no suggestion yet)", pane_id=pane_id, duration_ms=2000)
        return 0

    pane_data = _refresh_from_suggest_cache(data_dir, pane_data, core)

    primary = str(pane_data.get("primary") or "").strip()
    alts = [str(a).strip() for a in (pane_data.get("alternatives") or []) if str(a).strip()]
    choices = [c for c in [primary, *alts] if c]

    if not choices:
        _display_msg("Context Card: no suggested reply ready yet", pane_id=pane_id, duration_ms=2000)
        return 0

    idx = int(pane_data.get("alt_index") or 0) % len(choices)

    if args.action == "cycle":
        if len(choices) <= 1:
            _display_msg(f"Context Card [1/1]: {choices[0][:80]}", pane_id=pane_id, duration_ms=2200)
            return 0
        idx = (idx + 1) % len(choices)
        pane_data["alt_index"] = idx
        pane_data["pane_id"] = pane_id
        pane_data["updated_at"] = time.time()
        if pane_path is not None:
            try:
                core.write_json_atomic(pane_path, pane_data, mode=0o600)
            except Exception:
                pass
        accept_label, _alt_label = core.load_cli_key_labels(data_dir)
        preview = _sanitize_prompt_text(choices[idx])
        if len(preview) > 78:
            preview = preview[:77] + "…"
        _display_msg(
            f"💡 [{idx + 1}/{len(choices)}] {preview}  ({accept_label} to fill)",
            pane_id=pane_id,
            duration_ms=3200,
        )
        return 0

    # action == "accept"
    chosen = _sanitize_prompt_text(choices[idx])
    if not chosen:
        return 0
    _tmux("send-keys", "-t", pane_id, "-l", "--", chosen)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(0)
