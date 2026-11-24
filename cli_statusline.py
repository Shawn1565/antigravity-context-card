#!/usr/bin/env python3
"""Antigravity CLI (`agy`) native statusLine renderer + async suggestion worker.

Plan A (any terminal):
  Renders a responsive 1-2 line ANSI status bar docked below the `agy` prompt box:
  - Line 1: Segmented color context bar + granular token breakdown (Msg / Think / Tool / Cache / Ckpt)
  - Line 2: Suggested next reply (generated asynchronously so statusLine never blocks)

Plan B (inside `tmux`):
  Automatically detects `$TMUX_PANE`, writes pane-isolated suggestion state to
  `~/.gemini/antigravity-cli/plugin_data/context-card/panes/<pane_id>.json` (0600),
  and displays the configured keybinding badge (e.g. `[Alt+y]`) for one-key prompt insertion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import core  # noqa: E402
import habits  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SAFE_PANE_RE = re.compile(r"^[%A-Za-z0-9._-]{1,32}$")

# 256-color ANSI palette (works cleanly on dark & light terminals without truecolor issues)
RST = "\x1b[0m"
DIM = "\x1b[2m"
C_LABEL = "\x1b[38;5;245m"
C_SEP = "\x1b[38;5;240m"
C_FREE = "\x1b[38;5;238m"
C_OK = "\x1b[38;5;78m"
C_WARN = "\x1b[38;5;221m"
C_CRIT = "\x1b[1;38;5;203m"
C_CYAN = "\x1b[38;5;81m"
C_SUGGEST = "\x1b[38;5;223m"
C_KEY = "\x1b[38;5;114m"

CATEGORY_ANSI = {
    "messages": "\x1b[38;5;75m",
    "thinking": "\x1b[38;5;141m",
    "tool_outputs": "\x1b[38;5;209m",
    "memory_files": "\x1b[38;5;78m",
    "skills": "\x1b[38;5;221m",
    "system_tools": "\x1b[38;5;211m",
    "system_prompt": "\x1b[38;5;211m",
    "checkpoints": "\x1b[38;5;177m",
    "since_last_update": "\x1b[38;5;248m",
}

CACHE_MAX_FILES = 40
WORKER_STALE_SEC = 28.0
LOCK_STARTUP_GRACE_SEC = 5.0


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


def _display_width(text: str) -> int:
    clean = ANSI_RE.sub("", text)
    width = 0
    for ch in clean:
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ("W", "F") else 1
    return width


def _truncate_display(text: str, max_cols: int) -> str:
    if max_cols <= 0:
        return ""
    if _display_width(text) <= max_cols:
        return text
    if max_cols <= 1:
        return "…"
    out: list[str] = []
    used = 0
    limit = max_cols - 1
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out).rstrip() + "…"


def _sanitize_pane_id(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s or not SAFE_PANE_RE.match(s):
        return ""
    return s.replace("%", "p_")


def _cli_data_dir(override: str | None = None) -> Path:
    return habits.use_cli_context(override or None)


def _load_cli_prefs(data_dir: Path) -> dict[str, Any]:
    prefs: dict[str, Any] = {"auto_suggest": True}
    for cand in (
        data_dir / "prefs.json",
        Path.home() / ".gemini" / "antigravity" / "plugin_data" / "context-card" / "prefs.json",
    ):
        try:
            if cand.is_file():
                raw = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    prefs.update(raw)
                    break
        except Exception:
            continue
    return prefs


def _prune_dir(directory: Path, pattern: str, max_keep: int = CACHE_MAX_FILES) -> None:
    try:
        files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[max_keep:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _read_json_safe(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _load_or_parse_transcript(
    cid: str,
    tpath: Path,
    ws_hint: str,
    data_dir: Path,
) -> dict[str, Any]:
    st = tpath.stat()
    cache_dir = data_dir / "cli_cache"
    cache_path = cache_dir / f"transcript_{cid}.json"
    cached = _read_json_safe(cache_path)
    if (
        cached.get("_mtime_ns") == st.st_mtime_ns
        and cached.get("_size") == st.st_size
        and cached.get("_ws_hint") == ws_hint
        and isinstance(cached.get("snapshot"), dict)
    ):
        return cached["snapshot"]

    cli_home = Path.home() / ".gemini" / "antigravity-cli"
    snap = core.parse_transcript(
        cid,
        tpath,
        ws_hint=ws_hint or None,
        brain_dir=cli_home / "brain",
        builtin_skills_dir=cli_home / "builtin" / "skills",
        cache_dir=cache_dir,
        fast_cli=True,
    )

    # Persist compact breakdown + turn metadata for fast statusLine cache hits
    compact = {
        "conversation_id": snap["conversation_id"],
        "model_display": snap["model_display"],
        "used_tokens": snap["used_tokens"],
        "active_input_tokens": snap["active_input_tokens"],
        "limit_tokens": snap["limit_tokens"],
        "used_pct": snap["used_pct"],
        "cache_hit_pct": snap["cache_hit_pct"],
        "checkpoint_count": snap["checkpoint_count"],
        "categories": [
            {"key": c.get("key"), "tokens": c.get("tokens", 0), "is_free": bool(c.get("is_free"))}
            for c in (snap.get("categories") or [])
        ],
        "workspace_name": (snap.get("environment") or {}).get("workspace_name") or "",
        "_turn_info": snap.get("_turn_info") or {},
    }
    try:
        core.write_json_atomic(
            cache_path,
            {
                "_mtime_ns": st.st_mtime_ns,
                "_size": st.st_size,
                "_ws_hint": ws_hint,
                "snapshot": compact,
            },
            mode=0o600,
        )
        if int(time.time()) % 20 == 0:
            _prune_dir(cache_dir, "transcript_*.json")
    except Exception:
        pass
    return compact


def sync_pane_state(
    data_dir: Path,
    raw_pane_id: str,
    cid: str,
    turn_key: str,
    primary: str,
    alternatives: list[str],
    stale: bool = False,
    *,
    only_if_matching: bool = False,
) -> dict[str, Any] | None:
    """Persist pane-isolated suggestion state ONLY when meaningful fields changed.

    When `only_if_matching=True` (used by background workers), refuses to overwrite
    a pane that has already moved to another cid/turn_key or been marked `stale`.
    """
    safe_pane = _sanitize_pane_id(raw_pane_id)
    if not safe_pane or not cid:
        return None
    pane_path = data_dir / "panes" / f"{safe_pane}.json"
    existing = _read_json_safe(pane_path)
    if only_if_matching:
        if not existing:
            return None
        if existing.get("cid") != cid:
            return existing
        if existing.get("stale"):
            return existing
        ex_tk = str(existing.get("turn_key") or "").strip()
        if ex_tk and turn_key and ex_tk != turn_key:
            return existing

    alt_index = 0
    if existing.get("cid") == cid and existing.get("turn_key") == turn_key and isinstance(existing.get("alt_index"), int):
        total = 1 + len(alternatives) if primary else 0
        alt_index = existing["alt_index"] % total if total > 0 else 0

    same_content = (
        existing.get("pane_id") == raw_pane_id.strip()
        and existing.get("cid") == cid
        and existing.get("turn_key") == turn_key
        and existing.get("primary") == primary
        and list(existing.get("alternatives") or []) == list(alternatives)
        and bool(existing.get("stale")) == bool(stale)
    )
    if same_content:
        return existing

    payload = {
        "pane_id": raw_pane_id.strip(),
        "cid": cid,
        "turn_key": turn_key,
        "primary": primary,
        "alternatives": list(alternatives),
        "alt_index": alt_index,
        "stale": bool(stale),
        "updated_at": time.time(),
    }
    try:
        core.write_json_atomic(pane_path, payload, mode=0o600)
    except Exception:
        pass
    return payload


def _bind_pane_working(data_dir: Path, raw_pane_id: str, cid: str, turn_key: str = "") -> None:
    """Bind pane to current cid while marking suggestions stale during active work."""
    safe_pane = _sanitize_pane_id(raw_pane_id)
    if not safe_pane or not cid:
        return
    pane_path = data_dir / "panes" / f"{safe_pane}.json"
    existing = _read_json_safe(pane_path)
    if existing.get("cid") == cid and bool(existing.get("stale")) is True and not existing.get("primary"):
        return
    payload = {
        "pane_id": raw_pane_id.strip(),
        "cid": cid,
        "turn_key": turn_key,
        "primary": "",
        "alternatives": [],
        "alt_index": 0,
        "stale": True,
        "updated_at": time.time(),
    }
    try:
        core.write_json_atomic(pane_path, payload, mode=0o600)
    except Exception:
        pass


def _is_lock_active(lock_path: Path) -> bool:
    if not lock_path.is_file():
        return False
    info = _read_json_safe(lock_path)
    now = time.time()
    try:
        mtime_age = now - lock_path.stat().st_mtime
    except OSError:
        return False
    started_at = float(info.get("started_at") or 0.0)
    age = (now - started_at) if started_at > 0 else mtime_age
    if age > WORKER_STALE_SEC:
        return False
    pid = int(info.get("pid") or 0)
    if pid > 0:
        return _pid_alive(pid)
    # Parent just created the lock and is spawning the worker
    return age < LOCK_STARTUP_GRACE_SEC


def _try_acquire_worker_lock(lock_path: Path, cid: str, turn_key: str) -> str | None:
    """Atomically acquire `suggest_<cid>.lock` using O_CREAT|O_EXCL. Returns nonce or None."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex[:16]
    lock_payload = json.dumps(
        {
            "cid": cid,
            "turn_key": turn_key,
            "nonce": nonce,
            "pid": 0,
            "started_at": time.time(),
        }
    ).encode("utf-8")

    for attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, lock_payload)
            finally:
                os.close(fd)
            return nonce
        except FileExistsError:
            if attempt == 0 and not _is_lock_active(lock_path):
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    return None
                continue
            return None
        except OSError:
            return None
    return None


def _release_worker_lock(lock_path: Path, nonce: str) -> None:
    if not lock_path.is_file():
        return
    try:
        info = _read_json_safe(lock_path)
        if not nonce or not info or info.get("nonce") == nonce or int(info.get("pid") or 0) == os.getpid():
            lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def spawn_suggestion_worker(
    cid: str,
    tpath: Path,
    turn_key: str,
    ws_hint: str,
    data_dir: Path,
    raw_pane_id: str = "",
    force: bool = False,
) -> bool:
    """Atomically ensure a detached background worker is running for (cid, turn_key).

    Never overwrites `suggest_<cid>.json` after spawning the worker, eliminating the
    parent/child race where a fast worker result gets clobbered by a stale `generating` write.
    Returns True if a worker is running (newly spawned or already active).
    """
    cache_dir = data_dir / "cli_cache"
    suggest_path = cache_dir / f"suggest_{cid}.json"
    existing = _read_json_safe(suggest_path)

    if not force and existing.get("turn_key") == turn_key:
        if existing.get("status") in ("ready", "error", "needs_key", "skipped"):
            return False

    lock_path = cache_dir / f"suggest_{cid}.lock"
    nonce = _try_acquire_worker_lock(lock_path, cid, turn_key)
    if not nonce:
        return _is_lock_active(lock_path)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--cid",
        cid,
        "--transcript",
        str(tpath),
        "--turn-key",
        turn_key,
        "--lock-nonce",
        nonce,
        "--data-dir",
        str(data_dir),
    ]
    if ws_hint:
        cmd.extend(["--ws", ws_hint])
    if raw_pane_id:
        cmd.extend(["--pane", raw_pane_id])
    if force:
        cmd.append("--force")

    env = dict(os.environ)
    env["CONTEXT_CARD_INTERNAL_SUBPROCESS"] = "1"
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        return True
    except Exception as exc:
        _release_worker_lock(lock_path, nonce)
        try:
            core.write_json_atomic(
                suggest_path,
                {
                    "cid": cid,
                    "turn_key": turn_key,
                    "status": "error",
                    "primary": "",
                    "alternatives": [],
                    "model": "",
                    "error": str(exc)[:120],
                    "updated_at": time.time(),
                },
                mode=0o600,
            )
        except Exception:
            pass
        return False


def run_worker(args: argparse.Namespace) -> int:
    os.environ["CONTEXT_CARD_INTERNAL_SUBPROCESS"] = "1"
    data_dir = _cli_data_dir(args.data_dir)
    cid = core.sanitize_cid(args.cid)
    tpath = Path(args.transcript)
    cache_dir = data_dir / "cli_cache"
    lock_path = cache_dir / f"suggest_{cid}.lock" if cid else None
    suggest_path = cache_dir / f"suggest_{cid}.json" if cid else None
    fallback_tk = str(args.turn_key or "").strip()
    wrote_terminal = False

    def _write_terminal(status: str, turn_key: str, **extra: Any) -> None:
        nonlocal wrote_terminal
        if suggest_path is None or not cid:
            return
        payload: dict[str, Any] = {
            "cid": cid,
            "turn_key": turn_key,
            "status": status,
            "primary": "",
            "alternatives": [],
            "model": "",
            "error": None,
            "updated_at": time.time(),
            "pid": os.getpid(),
        }
        payload.update(extra)
        core.write_json_atomic(suggest_path, payload, mode=0o600)
        wrote_terminal = True

    try:
        if not cid or suggest_path is None:
            return 0

        # Record real worker PID into our lock file
        if lock_path is not None and lock_path.is_file():
            lock_info = _read_json_safe(lock_path)
            if not args.lock_nonce or lock_info.get("nonce") == args.lock_nonce:
                lock_info["pid"] = os.getpid()
                try:
                    core.write_json_atomic(lock_path, lock_info, mode=0o600)
                except Exception:
                    pass

        if not tpath.is_file():
            _write_terminal("skipped", fallback_tk, error="transcript not found")
            return 0

        cli_home = Path.home() / ".gemini" / "antigravity-cli"
        snap = core.parse_transcript(
            cid,
            tpath,
            ws_hint=args.ws or None,
            brain_dir=cli_home / "brain",
            builtin_skills_dir=cli_home / "builtin" / "skills",
            cache_dir=cache_dir,
            fast_cli=True,
        )
        turn_info = snap.get("_turn_info") or {}
        turn_key = str(turn_info.get("turn_key") or turn_info.get("latest_reply_turn_key") or fallback_tk)
        dialogue_text = snap.get("_dialogue_text") or ""
        assistant_tail = snap.get("_assistant_tail") or ""
        ws_name = (snap.get("environment") or {}).get("workspace_name") or ""

        if not turn_key or not dialogue_text:
            _write_terminal("skipped", turn_key or fallback_tk)
            return 0

        existing = _read_json_safe(suggest_path)
        if not args.force and existing.get("turn_key") == turn_key and existing.get("status") == "ready":
            wrote_terminal = True
            if args.pane:
                sync_pane_state(
                    data_dir,
                    args.pane,
                    cid,
                    turn_key,
                    str(existing.get("primary") or ""),
                    list(existing.get("alternatives") or []),
                    stale=False,
                    only_if_matching=True,
                )
            return 0

        try:
            primary, alts, model_used, latency_ms = core.call_suggest_llm(
                dialogue_text,
                assistant_tail,
                ws_name,
                force_variation=bool(args.force),
            )
            _write_terminal(
                "ready",
                turn_key,
                primary=primary,
                alternatives=alts,
                model=model_used,
                latency_ms=latency_ms,
                generated_at=datetime.now(tz=timezone.utc).isoformat(),
            )
            if args.pane:
                sync_pane_state(
                    data_dir,
                    args.pane,
                    cid,
                    turn_key,
                    primary,
                    alts,
                    stale=False,
                    only_if_matching=True,
                )
            _prune_dir(cache_dir, "suggest_*.json")
        except habits.AgyAuthRequiredError as exc:
            _write_terminal("needs_key", turn_key, error=str(exc)[:120])
        except Exception as exc:
            _write_terminal("error", turn_key, error=str(exc)[:120])
    except Exception as exc:
        if not wrote_terminal and fallback_tk:
            try:
                _write_terminal("error", fallback_tk, error=str(exc)[:120])
            except Exception:
                pass
    finally:
        if not wrote_terminal and fallback_tk:
            try:
                _write_terminal("error", fallback_tk, error="worker exited unexpectedly")
            except Exception:
                pass
        if lock_path is not None:
            _release_worker_lock(lock_path, str(args.lock_nonce or ""))
    return 0


def _render_segmented_bar(
    categories: list[dict[str, Any]],
    limit_tokens: int,
    used_tokens: int,
    bar_width: int,
) -> str:
    if limit_tokens <= 0 or bar_width <= 0:
        return f"{C_FREE}{'░' * max(1, bar_width)}{RST}"

    total_filled = min(bar_width, max(0, int(round((used_tokens / float(limit_tokens)) * bar_width))))
    if used_tokens > 0 and total_filled == 0:
        total_filled = 1

    non_free = [c for c in categories if not c.get("is_free") and int(c.get("tokens") or 0) > 0]
    if not non_free or total_filled == 0:
        if total_filled == 0:
            return f"{C_FREE}{'░' * bar_width}{RST}"
        return f"{CATEGORY_ANSI['messages']}{'█' * total_filled}{C_FREE}{'░' * (bar_width - total_filled)}{RST}"

    non_free_sum = sum(int(c.get("tokens") or 0) for c in non_free)
    if non_free_sum <= 0:
        return f"{C_FREE}{'░' * bar_width}{RST}"

    alloc: list[tuple[str, int]] = []
    assigned = 0
    remainders: list[tuple[float, int]] = []
    for idx, cat in enumerate(non_free):
        tok = int(cat.get("tokens") or 0)
        exact = (tok / float(non_free_sum)) * total_filled
        whole = int(exact)
        alloc.append((str(cat.get("key") or "messages"), whole))
        assigned += whole
        remainders.append((exact - whole, idx))

    leftover = total_filled - assigned
    remainders.sort(key=lambda x: x[0], reverse=True)
    for _frac, idx in remainders[: max(0, leftover)]:
        k, cnt = alloc[idx]
        alloc[idx] = (k, cnt + 1)

    parts: list[str] = []
    for k, cnt in alloc:
        if cnt <= 0:
            continue
        color = CATEGORY_ANSI.get(k, C_CYAN)
        parts.append(f"{color}{'█' * cnt}")
    empty_cnt = max(0, bar_width - total_filled)
    if empty_cnt > 0:
        parts.append(f"{C_FREE}{'░' * empty_cnt}")
    parts.append(RST)
    return "".join(parts)


def _cat_tokens(categories: list[dict[str, Any]], key: str) -> int:
    for c in categories:
        if c.get("key") == key:
            return int(c.get("tokens") or 0)
    return 0


def _pct_color(used_pct: float) -> str:
    if used_pct >= 80.0:
        return C_CRIT
    if used_pct >= 60.0:
        return C_WARN
    return C_OK


def render_statusline(payload: dict[str, Any], args: argparse.Namespace) -> str:
    data_dir = _cli_data_dir(args.data_dir)
    term_width = int(payload.get("terminal_width") or 100)
    if term_width < 40:
        term_width = 80

    cid_raw = str(payload.get("conversation_id") or payload.get("session_id") or "").strip()
    cid = core.sanitize_cid(cid_raw)
    tpath_raw = str(payload.get("transcript_path") or "").strip()
    tpath = Path(tpath_raw) if tpath_raw else None
    if (not tpath or not tpath.is_file()) and cid:
        cand = Path.home() / ".gemini" / "antigravity-cli" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if cand.is_file():
            tpath = cand

    ws_obj = payload.get("workspace") or {}
    ws_hint = str(ws_obj.get("project_dir") or ws_obj.get("current_dir") or payload.get("cwd") or "").strip()
    agent_state = str(payload.get("agent_state") or "idle").strip().lower()
    cycle_mode = str(payload.get("cycle_mode") or "").strip()
    cw = payload.get("context_window") or {}
    cw_size = int(cw.get("context_window_size") or 0)
    model_obj = payload.get("model") or {}
    model_label = str(model_obj.get("display_name") or model_obj.get("id") or "").strip()

    snap: dict[str, Any] | None = None
    if cid and tpath and tpath.is_file():
        snap = _load_or_parse_transcript(cid, tpath, ws_hint, data_dir)

    if snap is not None:
        categories = snap.get("categories") or []
        used_tokens = int(snap.get("used_tokens") or 0)
        limit_tokens = cw_size if cw_size > 0 else int(snap.get("limit_tokens") or 512_000)
        used_pct = round((used_tokens / float(limit_tokens)) * 100.0, 1) if limit_tokens > 0 else 0.0
        cache_hit_pct = float(snap.get("cache_hit_pct") or 0.0)
        ckpt_count = int(snap.get("checkpoint_count") or 0)
        if not model_label:
            model_label = str(snap.get("model_display") or "")
        turn_info = snap.get("_turn_info") or {}
    else:
        categories = []
        _, fallback_limit = core.format_model(model_label or None)
        limit_tokens = cw_size if cw_size > 0 else fallback_limit
        used_pct = round(float(cw.get("used_percentage") or 0.0), 1)
        cur_u = cw.get("current_usage") or {}
        cur_in = int(cur_u.get("input_tokens") or 0)
        cur_cr = int(cur_u.get("cache_read_input_tokens") or 0)
        if cur_in + cur_cr > 0:
            used_tokens = cur_in + cur_cr + int(cur_u.get("output_tokens") or 0)
            cache_hit_pct = round((cur_cr / float(cur_in + cur_cr)) * 100.0, 1)
        else:
            used_tokens = int(round(limit_tokens * used_pct / 100.0)) if limit_tokens > 0 else 0
            cache_hit_pct = 0.0
        ckpt_count = 0
        turn_info = {"state": "idle", "turn_key": ""}

    # Build Line 1
    msg_tok = _cat_tokens(categories, "messages")
    think_tok = _cat_tokens(categories, "thinking")
    tool_tok = _cat_tokens(categories, "tool_outputs")
    sys_tok = (
        _cat_tokens(categories, "system_tools")
        + _cat_tokens(categories, "system_prompt")
        + _cat_tokens(categories, "memory_files")
        + _cat_tokens(categories, "skills")
    )

    if term_width >= 108:
        bar_w = 16
    elif term_width >= 78:
        bar_w = 12
    else:
        bar_w = 8

    bar = _render_segmented_bar(categories, limit_tokens, used_tokens, bar_w)
    pct_c = _pct_color(used_pct)
    pct_str = f"{pct_c}{used_pct:.1f}%{RST}"
    tok_str = f"{C_LABEL}({core.fmt_k(used_tokens)}/{core.fmt_k(limit_tokens)}){RST}"

    prefix = ""
    if args.replace:
        head_bits: list[str] = []
        if model_label:
            head_bits.append(f"{C_CYAN}{model_label}{RST}")
        if cycle_mode:
            head_bits.append(f"{C_WARN}{cycle_mode}{RST}")
        if head_bits:
            prefix = f"{' · '.join(head_bits)} {C_SEP}│{RST} "

    if term_width >= 108:
        detail_parts = [
            f"{CATEGORY_ANSI['messages']}Msg{RST} {core.fmt_k(msg_tok)}",
            f"{CATEGORY_ANSI['thinking']}Think{RST} {core.fmt_k(think_tok)}",
            f"{CATEGORY_ANSI['tool_outputs']}Tool{RST} {core.fmt_k(tool_tok)}",
        ]
        if sys_tok > 0 and term_width >= 126:
            detail_parts.append(f"{C_LABEL}Sys{RST} {core.fmt_k(sys_tok)}")
        detail_parts.append(f"{C_LABEL}Cache{RST} {cache_hit_pct:.0f}%")
        if ckpt_count > 0:
            detail_parts.append(f"{CATEGORY_ANSI['checkpoints']}Ckpt{RST} {ckpt_count}")
        if used_pct >= 80.0:
            detail_parts.append(f"{C_CRIT}⚠ /compact{RST}")
        line1 = f"{prefix}{C_LABEL}Ctx{RST} [{bar}] {pct_str} {tok_str} {C_SEP}│{RST} " + f" {C_SEP}·{RST} ".join(detail_parts)
    elif term_width >= 78:
        detail_parts = [
            f"{CATEGORY_ANSI['messages']}M{RST}:{core.fmt_k(msg_tok)}",
            f"{CATEGORY_ANSI['thinking']}Th{RST}:{core.fmt_k(think_tok)}",
            f"{CATEGORY_ANSI['tool_outputs']}IO{RST}:{core.fmt_k(tool_tok)}",
            f"{C_LABEL}Cache{RST} {cache_hit_pct:.0f}%",
        ]
        if used_pct >= 80.0:
            detail_parts.append(f"{C_CRIT}/compact{RST}")
        line1 = f"{prefix}{C_LABEL}Ctx{RST} [{bar}] {pct_str} {tok_str} {C_SEP}│{RST} " + " ".join(detail_parts)
    else:
        compact_tail = f" {C_CRIT}/compact{RST}" if used_pct >= 80.0 else f" {C_LABEL}C:{cache_hit_pct:.0f}%{RST}"
        line1 = f"{prefix}[{bar}] {pct_str} {tok_str}{compact_tail}"

    lines = [line1]

    # Build Line 2: Suggested Reply (Plan A preview + Plan B tmux pane sync)
    in_tmux = bool(os.environ.get("TMUX") or os.environ.get("TMUX_PANE"))
    raw_pane_id = os.environ.get("TMUX_PANE") or ""

    if raw_pane_id and cid and (args.no_suggest or not tpath or not tpath.is_file()):
        _bind_pane_working(data_dir, raw_pane_id, cid)
        return "\n".join(lines)

    if args.no_suggest or not cid or not tpath or not tpath.is_file():
        return "\n".join(lines)

    prefs = _load_cli_prefs(data_dir)
    if not prefs.get("auto_suggest", True):
        if raw_pane_id:
            _bind_pane_working(data_dir, raw_pane_id, cid)
        return "\n".join(lines)

    turn_state = str(turn_info.get("state") or "idle")
    turn_key = str(turn_info.get("turn_key") or "")

    if agent_state != "idle" or turn_state != "user_turn" or not turn_key:
        if raw_pane_id:
            _bind_pane_working(data_dir, raw_pane_id, cid, turn_key)
        return "\n".join(lines)

    # Strict opt-in: if auth is cleared/disabled, do not display or serve cached suggestions
    if not habits.has_llm_configured():
        if raw_pane_id:
            sync_pane_state(data_dir, raw_pane_id, cid, turn_key, "", [], stale=False)
        return "\n".join(lines)

    suggest_path = data_dir / "cli_cache" / f"suggest_{cid}.json"
    sdata = _read_json_safe(suggest_path)

    if sdata.get("turn_key") == turn_key and sdata.get("status") == "ready" and sdata.get("primary"):
        primary = str(sdata.get("primary") or "").strip()
        alts = [str(a).strip() for a in (sdata.get("alternatives") or []) if str(a).strip()]
        pane_state = None
        if raw_pane_id:
            pane_state = sync_pane_state(data_dir, raw_pane_id, cid, turn_key, primary, alts, stale=False)

        choices = [primary, *alts]
        idx = int((pane_state or {}).get("alt_index") or 0) % len(choices)
        shown = choices[idx].replace("\n", " ").strip()
        idx_tag = f"[{idx + 1}/{len(choices)}] " if len(choices) > 1 and idx > 0 else ""

        if in_tmux:
            accept_lbl, alt_lbl = core.load_cli_key_labels(data_dir)
            if term_width >= 100 and len(choices) > 1:
                hint_plain = f"  [{accept_lbl} fill · {alt_lbl} alt]"
                hint_ansi = f"  {C_KEY}[{accept_lbl} fill · {alt_lbl} alt]{RST}"
            else:
                hint_plain = f"  [{accept_lbl}]"
                hint_ansi = f"  {C_KEY}[{accept_lbl}]{RST}"
        else:
            hint_plain = ""
            hint_ansi = ""

        lead_plain = f"💡 {idx_tag}"
        avail = max(12, term_width - _display_width(lead_plain) - _display_width(hint_plain) - 1)
        shown_trunc = _truncate_display(shown, avail)
        lines.append(f"💡 {C_LABEL}{idx_tag}{RST}{C_SUGGEST}{shown_trunc}{RST}{hint_ansi}")
        return "\n".join(lines)

    if sdata.get("turn_key") == turn_key and sdata.get("status") == "error":
        if raw_pane_id:
            sync_pane_state(data_dir, raw_pane_id, cid, turn_key, "", [], stale=False)
        err = _truncate_display(str(sdata.get("error") or "suggestion failed"), max(20, term_width - 14))
        lines.append(f"{DIM}💡 {err}{RST}")
        return "\n".join(lines)

    if sdata.get("turn_key") == turn_key and sdata.get("status") in ("skipped", "needs_key"):
        if raw_pane_id:
            sync_pane_state(data_dir, raw_pane_id, cid, turn_key, "", [], stale=False)
        return "\n".join(lines)

    # Bind pane to current (cid, turn_key) while waiting for async worker
    if raw_pane_id:
        sync_pane_state(data_dir, raw_pane_id, cid, turn_key, "", [], stale=False)

    # Strict opt-in: only spawn worker when LLM auth is explicitly configured
    if os.environ.get("CONTEXT_CARD_INTERNAL_SUBPROCESS") != "1" and habits.has_llm_configured():
        running = spawn_suggestion_worker(
            cid=cid,
            tpath=tpath,
            turn_key=turn_key,
            ws_hint=ws_hint,
            data_dir=data_dir,
            raw_pane_id=raw_pane_id,
        )
        if running:
            lines.append(f"{DIM}💡 Generating suggested reply…{RST}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Antigravity Context Card CLI statusLine")
    parser.add_argument("--replace", action="store_true", help="Include model & mode prefix for replace mode")
    parser.add_argument("--no-suggest", action="store_true", help="Disable Line 2 suggested reply display")
    parser.add_argument("--data-dir", default="", help="Override plugin data directory")
    parser.add_argument("--worker", action="store_true", help="Run as detached background suggestion worker")
    parser.add_argument("--cid", default="", help="Conversation ID (worker mode)")
    parser.add_argument("--transcript", default="", help="Transcript path (worker mode)")
    parser.add_argument("--turn-key", default="", help="Expected turn key (worker mode)")
    parser.add_argument("--lock-nonce", default="", help="Lock file ownership nonce (worker mode)")
    parser.add_argument("--ws", default="", help="Workspace hint (worker mode)")
    parser.add_argument("--pane", default="", help="TMUX_PANE ID (worker mode)")
    parser.add_argument("--force", action="store_true", help="Force regenerate suggestion (worker mode)")
    args = parser.parse_args()

    if args.worker:
        return run_worker(args)

    raw_in = sys.stdin.read() if not sys.stdin.isatty() else ""
    payload = json.loads(raw_in) if raw_in.strip() else {}
    if not isinstance(payload, dict):
        payload = {}
    out = render_statusline(payload, args)
    if out:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Never exit non-zero so `agy` never disables the statusLine after 30 failures
        raise SystemExit(0)
