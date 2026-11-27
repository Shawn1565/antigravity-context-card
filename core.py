#!/usr/bin/env python3
"""Pure-stdlib transcript parser, context breakdown engine, and suggestion helpers.

Shared by both the Desktop/IDE MCP App (`server.py`) and the Antigravity CLI
status line (`cli_statusline.py`), keeping CLI refreshes lightweight (~70ms warm
including Python startup) by avoiding any dependency on the `mcp` SDK.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import habits

TRUNCATED_BYTES_RE = re.compile(r"<truncated\s+(\d+)\s+(?:of\s+(\d+)\s+)?bytes[^>]*>")
USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
ABS_PATH_RE = re.compile(r"(/[\w.\-]+(?:/[\w.\-]+)+)")
SAFE_CID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
MAX_CACHE_ENTRIES = 40

_GIT_BRANCH_CACHE: dict[str, tuple[float, str | None]] = {}
_SKILLS_CATALOG_CACHE: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}

SUGGEST_SYSTEM_PROMPT = """You predict what the USER will reply next in an AI assistant/coding chat (like Claude Desktop's prompt suggestions).
Return ONLY valid JSON with this exact schema:
{"primary": "<most likely user reply>", "alternatives": ["<alt 1>", "<alt 2>"]}

Rules:
- Write strictly in the USER's voice (direct answer, follow-up request, or command), NEVER the assistant's voice.
- Match the user's language and concise style (if the user writes in Chinese, output natural concise Chinese; if English, English).
- Ground every suggestion in the assistant's latest message:
  * If the assistant asked a question or proposed concrete options, answer directly (primary = most likely choice; alternatives = other distinct options or tweaks).
  * If the assistant just finished coding/explaining, suggest natural concrete next steps (verify/test, refine a detail, proceed, commit, etc.).
- Keep "primary" short and ready to send as-is (typically 6-45 Chinese characters or 4-28 English words).
- Provide 1-2 short, genuinely distinct "alternatives" (different directions, not mere rephrasings of primary).
- Do NOT wrap text in quotes or add prefixes like "User:" or "你可以说：".
"""

_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "\u201c": "\u201d",
    "\u300c": "\u300d",
    "\u300e": "\u300f",
}


def fmt_k(n: int) -> str:
    if n >= 1_000_000:
        val = n / 1_000_000
        return f"{val:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        val = n / 1_000
        return f"{val:.1f}K".replace(".0K", "K")
    return str(n)


def sanitize_cid(raw: str | None, brain_dir: Path | None = None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if not s or ".." in s or not SAFE_CID_RE.match(s):
        return None
    if brain_dir is not None:
        try:
            base_resolved = brain_dir.resolve()
            cand_resolved = (brain_dir / s).resolve()
            if cand_resolved.parent != base_resolved:
                return None
        except OSError:
            return None
    return s


ANTIGRAVITY_COMPACT_LIMIT = 512_000
_OVERFLOW_INPUT_BUFFER = 32_000
_OVERFLOW_TOTAL_BUFFER = 48_000


def format_model_limits(model_id: str | None) -> tuple[str, int, int]:
    """Return (human_readable_name, effective_limit_tokens, native_limit_tokens).

    Antigravity auto-compacts Gemini 1M-window models around ~512K tokens (500K-512K
    observed across real sessions), while `agy statusLine` / API specs report the
    1,048,576 native window. Returning both lets the UI track real headroom before
    compaction while auto-lifting back to the native 1M ceiling if a session ever
    exceeds the 512K threshold.
    """
    if not model_id:
        return ("Unknown model", ANTIGRAVITY_COMPACT_LIMIT, ANTIGRAVITY_COMPACT_LIMIT)

    raw = model_id.strip()
    low = raw.lower()

    # Handle already-human-readable display names passed by CLI statusLine JSON
    if " " in low:
        if any(
            k in low
            for k in (
                "gemini 4",
                "gemini 3",
                "gemini 2.5",
                "orchestrator",
            )
        ):
            return (raw, ANTIGRAVITY_COMPACT_LIMIT, 1_048_576)
        if any(k in low for k in ("claude opus 5.5", "claude sonnet 5.5", "claude opus 5", "claude sonnet 5")):
            return (raw, 1_000_000, 1_000_000)
        if "claude" in low:
            return (raw, 200_000, 200_000)
        if "gpt-oss" in low:
            return (raw, 131_072, 131_072)
        return (raw, ANTIGRAVITY_COMPACT_LIMIT, ANTIGRAVITY_COMPACT_LIMIT)

    effort_map = {
        "-xhigh": " (XHigh)",
        "-high": " (High)",
        "-medium": " (Medium)",
        "-low": " (Low)",
    }
    effort_suffix = ""
    base = low
    for suf, label in effort_map.items():
        if base.endswith(suf):
            effort_suffix = label
            base = base[: -len(suf)]
            break

    # Known Antigravity model limits: (prefix, display_name, effective_compact_limit, native_limit).
    # Unknown models fall back conservatively to 512K so compaction warnings fire early rather than late.
    rules: list[tuple[str, str, int, int]] = [
        ("gemini-4-neon", "Gemini 4 Neon", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-4-carbon", "Gemini 4 Carbon", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-4-argon", "Gemini 4 Argon", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-4-hydrogen", "Gemini 4 Hydrogen", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-pro-orchestrator-neon", "Orchestrator (Neon)", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-pro-orchestrator-argon", "Orchestrator (Argon)", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-pro-orchestrator-carbon", "Orchestrator (Carbon)", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-pro-orchestrator", "Orchestrator (Carbon)", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-3.8-flash", "Gemini 3.8 Flash", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-3-8-flash", "Gemini 3.8 Flash", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-3.7-flash", "Gemini 3.7 Flash", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-3-7-flash", "Gemini 3.7 Flash", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-3.6-flash", "Gemini 3.6 Flash", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-3-6-flash", "Gemini 3.6 Flash", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-3.1-pro", "Gemini 3.1 Pro", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-3-1-pro", "Gemini 3.1 Pro", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-3", "Gemini 3", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-2.5", "Gemini 2.5", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("gemini-2-5", "Gemini 2.5", ANTIGRAVITY_COMPACT_LIMIT, 1_048_576),
        ("claude-opus-5-5", "Claude Opus 5.5", 1_000_000, 1_000_000),
        ("claude-opus-5.5", "Claude Opus 5.5", 1_000_000, 1_000_000),
        ("claude-sonnet-5-5", "Claude Sonnet 5.5", 1_000_000, 1_000_000),
        ("claude-sonnet-5.5", "Claude Sonnet 5.5", 1_000_000, 1_000_000),
        ("claude-opus-4-6", "Claude Opus 4.6", 200_000, 200_000),
        ("claude-opus-4.6", "Claude Opus 4.6", 200_000, 200_000),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6", 200_000, 200_000),
        ("claude-sonnet-4.6", "Claude Sonnet 4.6", 200_000, 200_000),
        ("claude-opus-4", "Claude Opus 4", 200_000, 200_000),
        ("claude-sonnet-4", "Claude Sonnet 4", 200_000, 200_000),
        ("gpt-oss-120b", "GPT-OSS 120B", 131_072, 131_072),
    ]
    for prefix, display, eff_limit, native_limit in rules:
        if base.startswith(prefix):
            return (f"{display}{effort_suffix}", eff_limit, native_limit)

    pretty = " ".join(part.capitalize() for part in base.replace("_", "-").split("-") if part)
    return (f"{pretty}{effort_suffix}", ANTIGRAVITY_COMPACT_LIMIT, ANTIGRAVITY_COMPACT_LIMIT)


def format_model(model_id: str | None) -> tuple[str, int]:
    """Return (human_readable_name, effective_context_limit_tokens)."""
    display, eff_limit, _native = format_model_limits(model_id)
    return (display, eff_limit)


def resolve_effective_limit(
    effective_limit: int,
    native_limit: int,
    used_tokens: int,
    active_input_tokens: int = 0,
) -> tuple[int, int]:
    """Resolve (limit_tokens, native_limit_tokens) with adaptive overflow lift.

    Stays on the effective auto-compact limit (e.g. 512K) right up to compaction
    (including small ~1-2% overshoot right before a CHECKPOINT fires), and lifts
    automatically to the native window (e.g. 1M) if the active session genuinely
    exceeds the compaction ceiling (e.g. if Antigravity raises the cap later).
    """
    eff = max(1, int(effective_limit or ANTIGRAVITY_COMPACT_LIMIT))
    nat = max(eff, int(native_limit or eff))

    env_override = (os.environ.get("CONTEXT_CARD_CONTEXT_LIMIT") or "").strip()
    if env_override:
        try:
            custom = int(env_override)
            if custom >= 1_000:
                return (custom, max(nat, custom))
        except ValueError:
            pass

    if nat > eff:
        if (
            active_input_tokens > eff + _OVERFLOW_INPUT_BUFFER
            or used_tokens > eff + _OVERFLOW_TOTAL_BUFFER
        ):
            return (nat, nat)
    return (eff, nat)


def est_tokens_from_text(text: str | None) -> int:
    if not text:
        return 0
    extra_bytes = 0
    for m in TRUNCATED_BYTES_RE.finditer(text):
        try:
            extra_bytes += int(m.group(1))
        except ValueError:
            pass
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    est = (ascii_chars / 4.0) + (non_ascii / 1.6) + (extra_bytes / 3.8)
    return max(1, int(round(est)))


def est_tokens_from_file(path: Path) -> tuple[int, int]:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        return (len(raw.encode("utf-8")), est_tokens_from_text(raw))
    except OSError:
        return (0, 0)


def clean_user_text(raw: str) -> str:
    m = USER_REQUEST_RE.search(raw)
    text = m.group(1) if m else raw
    text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
    text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
    return text.strip()


def extract_title(rows: list[dict[str, Any]], cid: str) -> str:
    for r in rows:
        if r.get("type") == "USER_INPUT":
            content = r.get("content") or ""
            m = USER_REQUEST_RE.search(content)
            body = m.group(1) if m else content
            for line in body.splitlines():
                s = line.strip()
                if s and not s.startswith("<"):
                    return s[:54] + ("…" if len(s) > 54 else "")
    return cid[:8]


def find_git_root(path_str: str) -> Path | None:
    try:
        p = Path(path_str)
        if not p.exists():
            p = p.parent
        if p.is_file():
            p = p.parent
        home = Path.home()
        for cur in [p, *p.parents]:
            if cur == home or cur == Path("/"):
                break
            if (cur / ".git").exists():
                return cur
    except OSError:
        pass
    return None


def get_git_branch(repo_root: Path) -> str | None:
    key = str(repo_root)
    now = time.time()
    cached = _GIT_BRANCH_CACHE.get(key)
    if cached and (now - cached[0]) < 10.0:
        return cached[1]
    branch: str | None = None
    try:
        res = subprocess.run(
            ["git", "-C", key, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if res.returncode == 0:
            val = res.stdout.strip()
            if val:
                branch = val
    except Exception:
        pass
    _GIT_BRANCH_CACHE[key] = (now, branch)
    return branch


def detect_workspace(
    rows: list[dict[str, Any]],
    fallback_ws_path: str | None = None,
    *,
    skip_git_subprocess: bool = False,
    prefer_fallback: bool = False,
) -> dict[str, Any]:
    home = Path.home()
    gemini_dir = str(home / ".gemini")
    best_root: Path | None = None

    if prefer_fallback and fallback_ws_path:
        fp = Path(fallback_ws_path)
        if fp.exists():
            best_root = find_git_root(str(fp)) or (fp if fp.is_dir() else fp.parent)

    if best_root is None:
        candidate_counts: dict[str, int] = {}
        for r in rows:
            tcalls = r.get("tool_calls") or []
            for tc in tcalls:
                args = tc.get("args") or {}
                if not isinstance(args, dict):
                    continue
                for k in ("Path", "Cwd", "Directory", "TargetFile", "CommandLine"):
                    v = args.get(k)
                    if not isinstance(v, str):
                        continue
                    cleaned = v.strip().strip('"').strip("'")
                    for m in ABS_PATH_RE.finditer(cleaned):
                        cand = m.group(1)
                        if (
                            cand.startswith(gemini_dir)
                            or cand.startswith("/tmp/")
                            or cand.startswith("/usr/")
                            or cand.startswith("/etc/")
                        ):
                            continue
                        root = find_git_root(cand)
                        if root is not None:
                            key = str(root)
                            candidate_counts[key] = candidate_counts.get(key, 0) + 1

        if candidate_counts:
            best_root = Path(max(candidate_counts.items(), key=lambda kv: kv[1])[0])
        elif fallback_ws_path:
            fp = Path(fallback_ws_path)
            if fp.exists():
                best_root = find_git_root(str(fp)) or (fp if fp.is_dir() else fp.parent)

    if best_root is not None:
        branch = None if skip_git_subprocess else get_git_branch(best_root)
        try:
            rel = "~/" + str(best_root.relative_to(home))
        except ValueError:
            rel = str(best_root)
        return {
            "host": socket.gethostname(),
            "workspace_name": best_root.name,
            "workspace_path": str(best_root),
            "workspace_display": rel,
            "branch": branch,
        }

    return {
        "host": socket.gethostname(),
        "workspace_name": "~",
        "workspace_path": None,
        "workspace_display": "No active workspace",
        "branch": None,
    }


def collect_memory_files(
    workspace_path: str | None,
    config_dir: Path | None = None,
) -> list[dict[str, Any]]:
    cfg_dir = config_dir or (Path.home() / ".gemini" / "config")
    candidates: list[Path] = []
    global_gemini = cfg_dir / "GEMINI.md"
    if global_gemini.is_file():
        candidates.append(global_gemini)

    if workspace_path:
        ws = Path(workspace_path)
        for rel in ("AGENTS.md", "GEMINI.md", "CLAUDE.md"):
            p = ws / rel
            if p.is_file():
                candidates.append(p)
        claude_dir = ws / ".claude"
        if claude_dir.is_dir():
            for p in sorted(claude_dir.glob("*CLAUDE*.md")):
                if p.is_file():
                    candidates.append(p)
        rules_dir = ws / ".agents" / "rules"
        if rules_dir.is_dir():
            for p in sorted(rules_dir.glob("*.md")):
                if p.is_file():
                    candidates.append(p)

    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    for p in candidates:
        rp = str(p.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        size_bytes, tok = est_tokens_from_file(p)
        if size_bytes == 0:
            continue
        files.append(
            {
                "name": p.name,
                "path": rp,
                "uri": p.as_uri(),
                "bytes": size_bytes,
                "tokens": tok,
            }
        )
    return files


def _skills_dir_signature(b_dir: Path, cfg_dir: Path) -> str:
    parts: list[str] = []
    for root in (b_dir, cfg_dir / "skills", cfg_dir / "plugins"):
        try:
            st = root.stat()
            parts.append(f"{root}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{root}:0")
    return "|".join(parts)


def collect_skills_catalog(
    builtin_skills_dir: Path | None = None,
    config_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    b_dir = builtin_skills_dir or (habits.AGY_HOME / "builtin" / "skills")
    cfg_dir = config_dir or (Path.home() / ".gemini" / "config")
    cache_key = f"{b_dir}|{cfg_dir}"
    now = time.time()
    cached = _SKILLS_CATALOG_CACHE.get(cache_key)
    if cached and (now - cached[0]) < 30.0:
        return (cached[1], cached[2])

    sig = _skills_dir_signature(b_dir, cfg_dir) if cache_dir is not None else ""
    disk_path = (cache_dir / "skills_catalog_cache.json") if cache_dir is not None else None
    if disk_path is not None and disk_path.is_file():
        try:
            dc = json.loads(disk_path.read_text(encoding="utf-8"))
            cached_at = float(dc.get("cached_at") or 0.0) if isinstance(dc, dict) else 0.0
            if (
                isinstance(dc, dict)
                and dc.get("sig") == sig
                and (now - cached_at) < 300.0
                and isinstance(dc.get("total_meta_tokens"), int)
                and isinstance(dc.get("catalog"), list)
            ):
                _SKILLS_CATALOG_CACHE[cache_key] = (now, dc["total_meta_tokens"], dc["catalog"])
                return (dc["total_meta_tokens"], dc["catalog"])
        except Exception:
            pass

    skill_paths: list[Path] = []
    for root in (b_dir, cfg_dir / "skills"):
        if root.is_dir():
            skill_paths.extend(sorted(root.glob("*/SKILL.md")))
    plugins_dir = cfg_dir / "plugins"
    if plugins_dir.is_dir():
        skill_paths.extend(sorted(plugins_dir.glob("*/skills/*/SKILL.md")))

    total_meta_tokens = 0
    catalog: list[dict[str, Any]] = []
    for sp in skill_paths:
        try:
            head = sp.read_text(encoding="utf-8", errors="ignore")[:1500]
        except OSError:
            continue
        fm = head
        if head.startswith("---"):
            parts = head.split("---", 2)
            if len(parts) >= 2:
                fm = parts[1]
        tok = max(20, est_tokens_from_text(fm))
        total_meta_tokens += tok
        catalog.append(
            {
                "name": sp.parent.name,
                "path": str(sp.resolve()),
                "uri": sp.resolve().as_uri(),
                "tokens": tok,
                "loaded_full": False,
            }
        )

    _SKILLS_CATALOG_CACHE[cache_key] = (now, total_meta_tokens, catalog)
    if disk_path is not None:
        try:
            write_json_atomic(
                disk_path,
                {
                    "sig": sig,
                    "cached_at": now,
                    "total_meta_tokens": total_meta_tokens,
                    "catalog": catalog,
                },
                mode=0o600,
            )
        except Exception:
            pass
    return (total_meta_tokens, catalog)


def collect_outputs(cid: str, brain_dir: Path | None = None) -> list[dict[str, Any]]:
    b_dir = brain_dir or habits.BRAIN_DIR
    safe_cid = sanitize_cid(cid, b_dir)
    if not safe_cid:
        return []
    conv_dir = b_dir / safe_cid
    if not conv_dir.is_dir():
        return []
    outputs: list[dict[str, Any]] = []
    skip_names = {".system_generated", ".user_uploaded", "scratch"}
    try:
        for entry in conv_dir.iterdir():
            if entry.name in skip_names or entry.name.startswith("."):
                continue
            if entry.name.endswith(".metadata.json"):
                continue
            if not entry.is_file():
                continue
            st = entry.stat()
            outputs.append(
                {
                    "name": entry.name,
                    "path": str(entry.resolve()),
                    "uri": entry.resolve().as_uri(),
                    "ext": entry.suffix.lower().lstrip("."),
                    "bytes": st.st_size,
                    "updated_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    "_mtime": st.st_mtime,
                }
            )
    except OSError:
        return []
    outputs.sort(key=lambda x: x["_mtime"], reverse=True)
    for item in outputs:
        item.pop("_mtime", None)
    return outputs[:25]


def detect_turn_state(cid: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"state": "idle", "turn_key": "", "latest_reply_turn_key": ""}

    latest_reply_key = ""
    for idx in range(len(rows) - 1, -1, -1):
        r = rows[idx]
        if (
            r.get("type") == "PLANNER_RESPONSE"
            and r.get("status") == "DONE"
            and (r.get("content") or "").strip()
            and not (r.get("tool_calls") or [])
        ):
            content = (r.get("content") or "").strip()
            step_idx = r.get("step_index", idx)
            digest = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()[:10]
            latest_reply_key = f"{cid}:{step_idx}:{digest}"
            break

    last = rows[-1]
    last_type = last.get("type")
    last_status = last.get("status")
    last_content = (last.get("content") or "").strip()
    last_tcalls = last.get("tool_calls") or []

    if last_type == "PLANNER_RESPONSE" and last_status == "DONE" and last_content and not last_tcalls:
        return {
            "state": "user_turn",
            "turn_key": latest_reply_key,
            "latest_reply_turn_key": latest_reply_key,
        }

    return {
        "state": "working",
        "turn_key": "",
        "latest_reply_turn_key": latest_reply_key,
    }


def _truncate_middle(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail + 16:
        return text
    return text[:head].rstrip() + "\n…\n" + text[-tail:].lstrip()


def extract_recent_dialogue(rows: list[dict[str, Any]], env: dict[str, Any]) -> str:
    turns: list[tuple[str, str]] = []
    pending_actions: list[str] = []

    for r in rows[-80:]:
        rtype = r.get("type")
        if rtype == "USER_INPUT":
            pending_actions.clear()
            body = clean_user_text(r.get("content") or "")
            if body:
                turns.append(("User", body))
        elif rtype == "PLANNER_RESPONSE":
            tcalls = r.get("tool_calls") or []
            for tc in tcalls:
                name = tc.get("name") or ""
                if name in ("open_context_card", "update_focus", "update_steps"):
                    continue
                args = tc.get("args") or {}
                if name in ("edit_file", "write_file") and isinstance(args.get("Path"), str):
                    clean_p = args["Path"].strip().strip('"').strip("'")
                    pending_actions.append(f"{name}({Path(clean_p).name})")
                elif name == "run_command" and isinstance(args.get("toolSummary"), str):
                    clean_s = args["toolSummary"].strip().strip('"').strip("'")
                    pending_actions.append(f"cmd:{clean_s}")
                elif name:
                    pending_actions.append(name)

            body = (r.get("content") or "").strip()
            if body and not tcalls:
                prefix = ""
                if pending_actions:
                    dedup: list[str] = []
                    for act in pending_actions[-6:]:
                        if act not in dedup:
                            dedup.append(act)
                    prefix = f"[Recent actions: {', '.join(dedup)}]\n"
                    pending_actions.clear()
                turns.append(("Assistant", prefix + body))

    recent = turns[-6:]
    if not recent:
        return ""

    parts: list[str] = []
    ws_name = env.get("workspace_name")
    branch = env.get("branch")
    if ws_name and ws_name != "~":
        header = f"Workspace: {ws_name}"
        if branch:
            header += f" (branch: {branch})"
        parts.append(header)

    for idx, (role, content) in enumerate(recent):
        is_last = idx == len(recent) - 1
        if is_last and role == "Assistant":
            snippet = _truncate_middle(content, 950, 1750)
        elif is_last:
            snippet = _truncate_middle(content, 600, 600)
        else:
            snippet = _truncate_middle(content, 240, 220)
        parts.append(f"{role}:\n{snippet}")

    return "\n\n".join(parts)


def strip_outer_quotes(s: str) -> str:
    s = s.strip()
    while len(s) >= 2 and s[0] in _QUOTE_PAIRS and s[-1] == _QUOTE_PAIRS[s[0]]:
        s = s[1:-1].strip()
    return s


def parse_suggest_json(raw: str) -> tuple[str, list[str]]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    obj: Any = None
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            parsed, _end = decoder.raw_decode(text, idx)
            if isinstance(parsed, dict) and ("primary" in parsed or "alternatives" in parsed):
                obj = parsed
                break
            if obj is None and isinstance(parsed, dict):
                obj = parsed
        except json.JSONDecodeError:
            pass
        idx = text.find("{", idx + 1)

    if not isinstance(obj, dict):
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        return (strip_outer_quotes(first_line), [])

    primary = strip_outer_quotes(str(obj.get("primary") or ""))
    raw_alts = obj.get("alternatives") or []
    alts: list[str] = []
    seen = {primary.lower()} if primary else set()
    if isinstance(raw_alts, list):
        for item in raw_alts:
            cand = strip_outer_quotes(str(item or ""))
            if cand and cand.lower() not in seen:
                seen.add(cand.lower())
                alts.append(cand)
            if len(alts) >= 2:
                break
    return (primary, alts)


def call_suggest_llm(
    dialogue_text: str,
    assistant_tail: str,
    workspace: str,
    force_variation: bool = False,
) -> tuple[str, list[str], str, int]:
    system_prompt = SUGGEST_SYSTEM_PROMPT
    personalization = habits.build_personalization_block(assistant_tail, workspace)
    if personalization:
        system_prompt = system_prompt.rstrip() + "\n\n" + personalization

    user_prompt = dialogue_text
    if force_variation:
        user_prompt += "\n\n[Note: Generate fresh, varied next-reply suggestions different from generic defaults.]"

    content, model_used, latency_ms = habits.call_gemini_json(
        system_prompt,
        user_prompt,
        temperature=0.72 if force_variation else 0.32,
        max_output_tokens=260,
        timeout=15.0,
    )
    primary, alts = parse_suggest_json(content)
    if not primary and alts:
        primary = alts.pop(0)
    if not primary:
        raise ValueError("Empty primary suggestion returned")
    return (primary, alts, model_used, latency_ms)


def parse_transcript(
    cid: str,
    tpath: Path,
    ws_hint: str | None = None,
    *,
    brain_dir: Path | None = None,
    builtin_skills_dir: Path | None = None,
    config_dir: Path | None = None,
    cache_dir: Path | None = None,
    fast_cli: bool = False,
    recent_predictions: dict[str, str] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
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
        pass

    title = extract_title(rows, cid)
    env = detect_workspace(
        rows,
        ws_hint,
        skip_git_subprocess=fast_cli,
        prefer_fallback=fast_cli,
    )
    memory_files = collect_memory_files(env["workspace_path"], config_dir=config_dir)
    skills_meta_tokens, skills_catalog = collect_skills_catalog(
        builtin_skills_dir=builtin_skills_dir,
        config_dir=config_dir,
        cache_dir=cache_dir,
    )
    outputs = [] if fast_cli else collect_outputs(cid, brain_dir=brain_dir)

    checkpoint_indices = [i for i, r in enumerate(rows) if r.get("type") == "CHECKPOINT"]
    last_cp_idx = checkpoint_indices[-1] if checkpoint_indices else -1
    planner_indices = [
        i
        for i, r in enumerate(rows)
        if r.get("type") == "PLANNER_RESPONSE" and r.get("input_tokens") is not None
    ]

    model_id: str | None = None
    for idx in reversed(planner_indices):
        if rows[idx].get("model"):
            model_id = rows[idx]["model"]
            break
    model_display, eff_model_limit, native_model_limit = format_model_limits(model_id)

    active_start = last_cp_idx + 1 if last_cp_idx >= 0 else 0
    active_planners = [i for i in planner_indices if i >= active_start]

    read_skill_paths: set[str] = set()
    read_skill_items: list[dict[str, Any]] = []
    for i in range(active_start, len(rows)):
        r = rows[i]
        for tc in r.get("tool_calls") or []:
            if tc.get("name") == "read_file":
                args = tc.get("args") or {}
                p_raw = args.get("Path")
                if isinstance(p_raw, str):
                    p_clean = p_raw.strip().strip('"').strip("'")
                    if p_clean.endswith("/SKILL.md") and p_clean not in read_skill_paths:
                        read_skill_paths.add(p_clean)
                        sp = Path(p_clean)
                        sz, tok = est_tokens_from_file(sp)
                        read_skill_items.append(
                            {
                                "name": sp.parent.name,
                                "path": p_clean,
                                "uri": sp.as_uri() if sp.is_absolute() else "",
                                "bytes": sz,
                                "tokens": tok,
                                "loaded_full": True,
                            }
                        )

    memory_est_total = sum(m["tokens"] for m in memory_files)
    system_baseline_total = 0
    if planner_indices:
        first_p = rows[planner_indices[0]]
        first_total = int(first_p.get("input_tokens") or 0) + int(first_p.get("cache_read_tokens") or 0)
        pre_first_est = 0
        for i in range(0, planner_indices[0]):
            r = rows[i]
            pre_first_est += est_tokens_from_text(r.get("content"))
            pre_first_est += 258 * len(r.get("media") or [])
        system_baseline_total = max(0, first_total - pre_first_est)

    memory_tokens = min(memory_est_total, system_baseline_total)
    rem_base = max(0, system_baseline_total - memory_tokens)
    skills_base_tokens = min(skills_meta_tokens, rem_base)
    system_tools_prompt_tokens = max(0, rem_base - skills_base_tokens)
    system_prompt_tokens = min(3500, int(round(system_tools_prompt_tokens * 0.22)))
    system_tools_tokens = max(0, system_tools_prompt_tokens - system_prompt_tokens)

    messages_tokens = 0
    thinking_tokens = 0
    tool_outputs_tokens = 0
    skills_loaded_tokens = 0
    checkpoint_tokens = 0
    since_last_update_tokens = 0
    active_input_total = 0
    last_cache_read = 0
    last_input_uncached = 0
    last_output_tokens = 0

    if active_planners:
        first_act_idx = active_planners[0]
        last_act_idx = active_planners[-1]
        first_act = rows[first_act_idx]
        last_act = rows[last_act_idx]

        last_input_uncached = int(last_act.get("input_tokens") or 0)
        last_cache_read = int(last_act.get("cache_read_tokens") or 0)
        last_output_tokens = int(last_act.get("output_tokens") or 0)
        active_input_total = last_input_uncached + last_cache_read

        first_act_total = int(first_act.get("input_tokens") or 0) + int(first_act.get("cache_read_tokens") or 0)

        if last_cp_idx >= 0:
            cp_row = rows[last_cp_idx]
            cp_known = int(cp_row.get("output_tokens") or 0)
            between_est = 0
            for j in range(active_start, first_act_idx):
                r = rows[j]
                between_est += est_tokens_from_text(r.get("content")) + 258 * len(r.get("media") or [])
            above_base = max(0, first_act_total - system_baseline_total)
            if cp_known > 0 and above_base > 0:
                if between_est > 0:
                    checkpoint_tokens += min(cp_known, above_base)
                    messages_tokens += max(0, above_base - checkpoint_tokens)
                else:
                    checkpoint_tokens += above_base
            elif between_est > 0 and above_base > 0:
                msg_part = min(between_est, above_base)
                messages_tokens += msg_part
                checkpoint_tokens += max(0, above_base - msg_part)
            else:
                checkpoint_tokens += above_base
        else:
            messages_tokens += max(0, first_act_total - system_baseline_total)

        for p_pos in range(1, len(active_planners)):
            prev_i = active_planners[p_pos - 1]
            curr_i = active_planners[p_pos]
            prev_r = rows[prev_i]
            curr_r = rows[curr_i]
            prev_tot = int(prev_r.get("input_tokens") or 0) + int(prev_r.get("cache_read_tokens") or 0)
            curr_tot = int(curr_r.get("input_tokens") or 0) + int(curr_r.get("cache_read_tokens") or 0)
            delta = curr_tot - prev_tot
            if delta == 0:
                continue
            if delta < 0:
                shrink = -delta
                take_think = min(thinking_tokens, shrink)
                thinking_tokens -= take_think
                rem_shrink = shrink - take_think
                if rem_shrink > 0:
                    take_tool = min(tool_outputs_tokens, rem_shrink)
                    tool_outputs_tokens -= take_tool
                    rem_shrink -= take_tool
                if rem_shrink > 0:
                    messages_tokens = max(0, messages_tokens - rem_shrink)
                continue

            weights: dict[str, float] = {
                "messages": 0.0,
                "thinking": 0.0,
                "tool_outputs": 0.0,
                "skills": 0.0,
            }

            prev_out_actual = int(prev_r.get("output_tokens") or 0)
            prev_text_est = est_tokens_from_text(prev_r.get("content"))
            prev_tcalls = prev_r.get("tool_calls") or []
            prev_tc_est = est_tokens_from_text(json.dumps(prev_tcalls, ensure_ascii=False)) if prev_tcalls else 0
            has_thinking = bool((prev_r.get("thinking") or "").strip())

            if prev_out_actual > 0:
                non_think_est = prev_text_est + prev_tc_est
                if non_think_est >= prev_out_actual and non_think_est > 0:
                    scale = prev_out_actual / float(non_think_est)
                    weights["messages"] += prev_text_est * scale
                    weights["tool_outputs"] += prev_tc_est * scale
                else:
                    weights["messages"] += prev_text_est
                    weights["tool_outputs"] += prev_tc_est
                    rem_out = prev_out_actual - non_think_est
                    if has_thinking:
                        weights["thinking"] += rem_out
                    elif prev_tc_est > 0 and prev_text_est == 0:
                        weights["tool_outputs"] += rem_out
                    else:
                        weights["messages"] += rem_out
            else:
                if prev_text_est > 0:
                    weights["messages"] += prev_text_est
                if has_thinking:
                    weights["thinking"] += est_tokens_from_text(prev_r.get("thinking"))
                if prev_tc_est > 0:
                    weights["tool_outputs"] += prev_tc_est

            skill_read_count = sum(
                1
                for tc in prev_tcalls
                if tc.get("name") == "read_file"
                and isinstance((tc.get("args") or {}).get("Path"), str)
                and (tc["args"]["Path"].strip().strip('"').strip("'")).endswith("/SKILL.md")
            )
            skill_generics_seen = 0

            for j in range(prev_i + 1, curr_i):
                step = rows[j]
                stype = step.get("type")
                est = est_tokens_from_text(step.get("content")) + 258 * len(step.get("media") or [])
                if stype == "USER_INPUT":
                    weights["messages"] += max(1, est)
                elif stype == "GENERIC":
                    if skill_generics_seen < skill_read_count and (
                        "SKILL.md" in (step.get("content") or "")[:300]
                        or "name:" in (step.get("content") or "")[:300]
                    ):
                        weights["skills"] += max(1, est)
                        skill_generics_seen += 1
                    else:
                        weights["tool_outputs"] += max(1, est)
                else:
                    weights["messages"] += max(1, est)

            w_sum = sum(weights.values())
            if w_sum <= 0:
                messages_tokens += delta
            else:
                m_add = int(round(delta * weights["messages"] / w_sum))
                th_add = int(round(delta * weights["thinking"] / w_sum))
                s_add = int(round(delta * weights["skills"] / w_sum))
                t_add = max(0, delta - m_add - th_add - s_add)
                messages_tokens += m_add
                thinking_tokens += th_add
                skills_loaded_tokens += s_add
                tool_outputs_tokens += t_add

        accounted = (
            messages_tokens
            + thinking_tokens
            + tool_outputs_tokens
            + memory_tokens
            + skills_base_tokens
            + skills_loaded_tokens
            + system_tools_tokens
            + system_prompt_tokens
            + checkpoint_tokens
        )
        diff = active_input_total - accounted
        if diff != 0:
            dyn_sum = messages_tokens + thinking_tokens + tool_outputs_tokens
            if dyn_sum > 0 and (dyn_sum + diff) >= 0:
                m_adj = int(round(diff * messages_tokens / dyn_sum))
                th_adj = int(round(diff * thinking_tokens / dyn_sum))
                t_adj = diff - m_adj - th_adj
                messages_tokens = max(0, messages_tokens + m_adj)
                thinking_tokens = max(0, thinking_tokens + th_adj)
                tool_outputs_tokens = max(0, tool_outputs_tokens + t_adj)
                recheck = (
                    messages_tokens
                    + thinking_tokens
                    + tool_outputs_tokens
                    + memory_tokens
                    + skills_base_tokens
                    + skills_loaded_tokens
                    + system_tools_tokens
                    + system_prompt_tokens
                    + checkpoint_tokens
                )
                rem_diff = active_input_total - recheck
                if rem_diff != 0:
                    if tool_outputs_tokens + rem_diff >= 0:
                        tool_outputs_tokens += rem_diff
                    elif messages_tokens + rem_diff >= 0:
                        messages_tokens += rem_diff
            elif messages_tokens + diff >= 0:
                messages_tokens += diff
            else:
                system_tools_tokens = max(0, system_tools_tokens + diff)

        trailing_est = 0
        for j in range(last_act_idx + 1, len(rows)):
            step = rows[j]
            trailing_est += est_tokens_from_text(step.get("content")) + 258 * len(step.get("media") or [])
        since_last_update_tokens = last_output_tokens + trailing_est

    elif last_cp_idx >= 0:
        cp_row = rows[last_cp_idx]
        cp_out = int(cp_row.get("output_tokens") or 0)
        checkpoint_tokens = cp_out if cp_out > 0 else est_tokens_from_text(cp_row.get("content"))
        active_input_total = system_baseline_total + checkpoint_tokens
        for j in range(active_start, len(rows)):
            step = rows[j]
            since_last_update_tokens += est_tokens_from_text(step.get("content")) + 258 * len(step.get("media") or [])

    elif rows:
        for r in rows:
            since_last_update_tokens += est_tokens_from_text(r.get("content")) + 258 * len(r.get("media") or [])

    skills_total_tokens = skills_base_tokens + skills_loaded_tokens
    total_used = active_input_total + since_last_update_tokens
    active_max_input = active_input_total
    for idx in active_planners:
        r_act = rows[idx]
        p_tot = int(r_act.get("input_tokens") or 0) + int(r_act.get("cache_read_tokens") or 0)
        if p_tot > active_max_input:
            active_max_input = p_tot
    model_limit, native_limit = resolve_effective_limit(
        eff_model_limit,
        native_model_limit,
        total_used,
        active_max_input,
    )
    free_space = max(0, model_limit - total_used)
    used_pct = min(100.0, round((total_used / model_limit) * 100, 1)) if model_limit > 0 else 0.0
    cache_hit_pct = round((last_cache_read / active_input_total) * 100, 1) if active_input_total > 0 else 0.0

    categories: list[dict[str, Any]] = [
        {
            "key": "messages",
            "label": "Messages",
            "tokens": messages_tokens,
            "pct": round((messages_tokens / model_limit) * 100, 1) if model_limit else 0.0,
            "color": "#3b82f6",
            "hint": "User prompts and agent visible replies in the active context window",
        },
    ]

    if thinking_tokens > 0:
        categories.append(
            {
                "key": "thinking",
                "label": "Thinking",
                "tokens": thinking_tokens,
                "pct": round((thinking_tokens / model_limit) * 100, 1) if model_limit else 0.0,
                "color": "#8b5cf6",
                "hint": "Model reasoning / chain-of-thought retained in active context",
            }
        )

    categories.extend(
        [
            {
                "key": "tool_outputs",
                "label": "Tool calls & outputs",
                "tokens": tool_outputs_tokens,
                "pct": round((tool_outputs_tokens / model_limit) * 100, 1) if model_limit else 0.0,
                "color": "#f97316",
                "hint": "Tool invocations, file reads, and command outputs",
            },
            {
                "key": "system_tools",
                "label": "System tools",
                "tokens": system_tools_tokens,
                "pct": round((system_tools_tokens / model_limit) * 100, 1) if model_limit else 0.0,
                "color": "#fb7185",
                "hint": "Built-in tool definitions and MCP tool schemas (estimated from Turn 1 baseline)",
            },
            {
                "key": "memory_files",
                "label": "Memory files",
                "tokens": memory_tokens,
                "pct": round((memory_tokens / model_limit) * 100, 1) if model_limit else 0.0,
                "color": "#10b981",
                "hint": "GEMINI.md, AGENTS.md, and active workspace rule files",
            },
            {
                "key": "skills",
                "label": "Skills",
                "tokens": skills_total_tokens,
                "pct": round((skills_total_tokens / model_limit) * 100, 1) if model_limit else 0.0,
                "color": "#eab308",
                "hint": "Skill descriptions catalog plus any SKILL.md read in this session",
            },
            {
                "key": "system_prompt",
                "label": "System prompt",
                "tokens": system_prompt_tokens,
                "pct": round((system_prompt_tokens / model_limit) * 100, 1) if model_limit else 0.0,
                "color": "#ec4899",
                "hint": "Core system instructions (estimated from Turn 1 baseline)",
            },
        ]
    )

    if checkpoint_tokens > 0:
        categories.append(
            {
                "key": "checkpoints",
                "label": "Checkpoint summary",
                "tokens": checkpoint_tokens,
                "pct": round((checkpoint_tokens / model_limit) * 100, 1) if model_limit else 0.0,
                "color": "#a855f7",
                "hint": "Compacted continuation summary replacing older turns",
            }
        )

    if since_last_update_tokens > 0:
        categories.append(
            {
                "key": "since_last_update",
                "label": "Since last update",
                "tokens": since_last_update_tokens,
                "pct": round((since_last_update_tokens / model_limit) * 100, 1) if model_limit else 0.0,
                "color": "#9ca3af",
                "hint": "Latest model output and any tool results pending the next model call",
            }
        )

    free_hint = (
        f"Remaining tokens before Antigravity auto-compaction (~{fmt_k(model_limit)}; model native window {fmt_k(native_limit)})"
        if native_limit > model_limit
        else "Remaining tokens before hitting the model context limit"
    )
    categories.append(
        {
            "key": "free_space",
            "label": "Free space",
            "tokens": free_space,
            "pct": round((free_space / model_limit) * 100, 1) if model_limit else 0.0,
            "color": "var(--ctx-free, #323238)",
            "hint": free_hint,
            "is_free": True,
        }
    )

    last_ts = None
    for r in reversed(rows):
        if r.get("created_at"):
            last_ts = r["created_at"]
            break

    turn_info = detect_turn_state(cid, rows)
    dialogue_text = extract_recent_dialogue(rows, env)
    assistant_tail = ""
    for r in reversed(rows):
        if (
            r.get("type") == "PLANNER_RESPONSE"
            and r.get("status") == "DONE"
            and (r.get("content") or "").strip()
            and not (r.get("tool_calls") or [])
        ):
            assistant_tail = (r.get("content") or "").strip()[-400:]
            break

    if recent_predictions:
        habits.capture_implicit_feedback(cid, rows, env.get("workspace_name") or "", recent_predictions)

    return {
        "conversation_id": cid,
        "title": title,
        "environment": env,
        "model_id": model_id,
        "model_display": model_display,
        "used_tokens": total_used,
        "active_input_tokens": active_input_total,
        "limit_tokens": model_limit,
        "native_limit_tokens": native_limit,
        "used_pct": used_pct,
        "free_tokens": free_space,
        "cache_read_tokens": last_cache_read,
        "input_uncached_tokens": last_input_uncached,
        "last_output_tokens": last_output_tokens,
        "cache_hit_pct": cache_hit_pct,
        "checkpoint_count": len(checkpoint_indices),
        "step_count": len(rows),
        "active_step_count": len(rows) - active_start,
        "categories": categories,
        "memory_files": memory_files,
        "loaded_skills": read_skill_items,
        "total_skills_count": len(skills_catalog),
        "outputs": outputs,
        "last_event_at": last_ts,
        "refreshed_at": datetime.now(tz=timezone.utc).isoformat(),
        "_turn_info": turn_info,
        "_dialogue_text": dialogue_text,
        "_assistant_tail": assistant_tail,
    }


def write_json_atomic(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.write("\n")
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def is_subagent(cid: str, summaries_db: Path | None = None) -> bool:
    db = summaries_db or habits.SUMMARIES_DB
    if not cid or not db.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.4)
        try:
            try:
                row = conn.execute(
                    "SELECT parent_id FROM conversation_parents WHERE conversation_id = ?",
                    (cid,),
                ).fetchone()
                if row and str(row[0] or "").strip():
                    return True
            except sqlite3.Error:
                pass
            row2 = conn.execute(
                "SELECT parent_conversation_id FROM conversation_summaries WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
            if row2 and str(row2[0] or "").strip():
                return True
        finally:
            conn.close()
    except Exception:
        pass
    return False


def is_our_statusline_config(sl: Any, plugin_root: Path | None = None, data_dir: Path | None = None) -> bool:
    if not isinstance(sl, dict):
        return False
    cmd = str(sl.get("command") or "").strip()
    if not cmd or "cli_statusline.py" not in cmd:
        return False
    root = (plugin_root or Path(__file__).resolve().parent).resolve()
    abs_script = str((root / "cli_statusline.py").resolve())
    if abs_script in cmd:
        return True
    try:
        rel_home = f"~/{(root / 'cli_statusline.py').resolve().relative_to(Path.home().resolve()).as_posix()}"
        if rel_home in cmd:
            return True
    except ValueError:
        pass
    # Also match the exact recorded command from cli_config.json (handles custom clone dir names)
    ddir = data_dir or habits.resolve_cli_data_dir()
    cfg_path = ddir / "cli_config.json"
    try:
        if cfg_path.is_file():
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("statusline_command") == cmd:
                return True
    except Exception:
        pass
    return False


def is_cli_statusline_installed(settings_path: Path | None = None) -> tuple[bool, bool]:
    """Return (installed_and_enabled, suggest_enabled) for Antigravity CLI statusLine."""
    spath = settings_path or (Path.home() / ".gemini" / "antigravity-cli" / "settings.json")
    if not spath.is_file():
        return (False, False)
    try:
        raw = json.loads(spath.read_text(encoding="utf-8"))
    except Exception:
        return (False, False)
    if not isinstance(raw, dict):
        return (False, False)
    sl = raw.get("statusLine")
    if not is_our_statusline_config(sl):
        return (False, False)
    if sl.get("enabled") is False:
        return (False, False)
    cmd = str(sl.get("command") or "")
    return (True, "--no-suggest" not in cmd)


def format_tmux_key_label(tmux_key: str) -> str:
    k = (tmux_key or "").strip()
    if not k:
        return "Alt+y"
    if k.startswith("M-") and len(k) > 2:
        return f"Alt+{k[2:]}"
    if k.startswith("C-") and len(k) > 2:
        return f"Ctrl+{k[2:]}"
    return k


def load_cli_key_labels(data_dir: Path | None = None) -> tuple[str, str]:
    ddir = data_dir or habits.resolve_cli_data_dir()
    cfg_path = ddir / "cli_config.json"
    accept_k, alt_k = "M-y", "M-u"
    try:
        if cfg_path.is_file():
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                accept_k = str(raw.get("accept_key") or accept_k)
                alt_k = str(raw.get("alt_key") or alt_k)
    except Exception:
        pass
    return (format_tmux_key_label(accept_k), format_tmux_key_label(alt_k))
