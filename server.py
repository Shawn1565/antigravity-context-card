#!/usr/bin/env python3
"""Antigravity Context Card MCP App server.

Reads Antigravity's documented conversation transcripts under
~/.gemini/antigravity/brain/<conversation-id>/.system_generated/logs/transcript.jsonl
to render a live Claude Desktop-style Context panel bound to each window's own conversation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from urllib.parse import unquote, urlparse

from mcp import types
from mcp.server.fastmcp import Context, FastMCP

APP_URI = "ui://context-card/app.html"
PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
import habits  # noqa: E402

DEFAULT_HTML_PATH = PLUGIN_ROOT / "dist" / "app.html"
PREFS_PATH = habits.DATA_DIR / "prefs.json"
RUNTIME_DB_PATH = habits.DATA_DIR / "runtime_state.db"
BRAIN_DIR = habits.BRAIN_DIR
SUMMARIES_DB = habits.SUMMARIES_DB
CONFIG_DIR = Path.home() / ".gemini" / "config"
BUILTIN_SKILLS_DIR = habits.AGY_HOME / "builtin" / "skills"

DEFAULT_SUGGEST_MODEL = habits.DEFAULT_GEMINI_MODEL
SERVER_PID = os.getpid()
SERVER_STARTED_AT = time.time()
ACTIVE_CLIENT_TTL_SEC = 5.5

TRUNCATED_BYTES_RE = re.compile(r"<truncated\s+(\d+)\s+(?:of\s+(\d+)\s+)?bytes[^>]*>")
USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
ABS_PATH_RE = re.compile(r"(/[\w.\-]+(?:/[\w.\-]+)+)")

# Caches keyed by (cid -> (mtime_ns, size, ws_hint, parsed_dict))
_TRANSCRIPT_CACHE: dict[str, tuple[int, int, str, dict[str, Any]]] = {}
_GIT_BRANCH_CACHE: dict[str, tuple[float, str | None]] = {}
_SKILLS_CATALOG_CACHE: tuple[float, int, list[dict[str, Any]]] | None = None

_RUNTIME_DB_LOCK = threading.Lock()
_RUNTIME_DB_READY = False
_SUGGEST_LOCK = threading.Lock()
_SUGGEST_STATE: dict[str, dict[str, Any]] = {}
_RECENT_PREDICTIONS: dict[str, dict[str, str]] = {}
_PREFS_CACHE: dict[str, Any] | None = None

mcp = FastMCP("context-card-monitor")


def _open_runtime_db() -> sqlite3.Connection:
    global _RUNTIME_DB_READY
    conn = sqlite3.connect(str(RUNTIME_DB_PATH), timeout=1.5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if not _RUNTIME_DB_READY:
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
            INSERT INTO server_life (id, pid, started_at, unconfirmed_auto_ts)
            VALUES (1, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                pid = excluded.pid,
                started_at = excluded.started_at,
                unconfirmed_auto_ts = CASE
                    WHEN server_life.pid = excluded.pid THEN server_life.unconfirmed_auto_ts
                    ELSE 0
                END
            """,
            (SERVER_PID, SERVER_STARTED_AT),
        )
        conn.commit()
        _RUNTIME_DB_READY = True
    return conn


def _init_runtime_state() -> None:
    try:
        with _RUNTIME_DB_LOCK:
            conn = _open_runtime_db()
            conn.close()
    except Exception:
        pass


_init_runtime_state()


def _record_explicit_open(cid: str) -> None:
    if not cid:
        return
    now = time.time()
    try:
        with _RUNTIME_DB_LOCK:
            conn = _open_runtime_db()
            try:
                conn.execute(
                    """
                    INSERT INTO session_bindings (cid, explicit_server_pid, explicit_bound_ts)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cid) DO UPDATE SET
                        explicit_server_pid = excluded.explicit_server_pid,
                        explicit_bound_ts = excluded.explicit_bound_ts
                    """,
                    (cid, SERVER_PID, now),
                )
                conn.execute("UPDATE server_life SET unconfirmed_auto_ts = 0 WHERE id = 1")
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _record_manual_auto_open() -> None:
    now = time.time()
    try:
        with _RUNTIME_DB_LOCK:
            conn = _open_runtime_db()
            try:
                conn.execute(
                    "UPDATE server_life SET unconfirmed_auto_ts = ? WHERE id = 1",
                    (now,),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _record_client_heartbeat(client_id: str | None, cid: str, bind_mode: str) -> None:
    cl = (client_id or "").strip()
    if not cl or not cid:
        return
    now = time.time()
    mode = "auto" if bind_mode == "auto" else "explicit"
    try:
        with _RUNTIME_DB_LOCK:
            conn = _open_runtime_db()
            try:
                conn.execute(
                    """
                    INSERT INTO pane_clients (client_id, cid, bind_mode, updated_ts)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(client_id) DO UPDATE SET
                        cid = excluded.cid,
                        bind_mode = excluded.bind_mode,
                        updated_ts = excluded.updated_ts
                    """,
                    (cl[:64], cid, mode, now),
                )
                if int(now) % 15 == 0:
                    conn.execute("DELETE FROM pane_clients WHERE updated_ts < ?", (now - 120.0,))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _get_runtime_maps() -> tuple[set[str], dict[str, tuple[str, float]]]:
    """Return (actively_displayed_cids, {cid: (workspace_path, hook_seen_ts)})."""
    active_cids: set[str] = set()
    hook_map: dict[str, tuple[str, float]] = {}
    now = time.time()
    try:
        with _RUNTIME_DB_LOCK:
            conn = _open_runtime_db()
            try:
                for (cid,) in conn.execute(
                    "SELECT DISTINCT cid FROM pane_clients WHERE updated_ts >= ?",
                    (now - ACTIVE_CLIENT_TTL_SEC,),
                ).fetchall():
                    if cid:
                        active_cids.add(str(cid))
                for cid, ws_path, seen_ts in conn.execute(
                    "SELECT cid, workspace_path, hook_seen_ts FROM session_bindings"
                ).fetchall():
                    if cid:
                        hook_map[str(cid)] = (str(ws_path or ""), float(seen_ts or 0.0))
            finally:
                conn.close()
    except Exception:
        pass
    return (active_cids, hook_map)


def _parse_iso_ts(val: str | None) -> float:
    if not val:
        return 0.0
    s = val.strip()
    if not s:
        return 0.0
    try:
        # Handle Go/SQLite 9-digit nanosecond timestamps cleanly across Python versions
        s_norm = s.replace("Z", "+00:00")
        m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", s_norm)
        if m:
            frac = (m.group(2) + "000000")[:6]
            s_norm = f"{m.group(1)}.{frac}{m.group(3)}"
        dt = datetime.fromisoformat(s_norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _uri_to_local_path(raw_uri: str | None) -> str | None:
    if not raw_uri:
        return None
    s = raw_uri.strip()
    if not s:
        return None
    if s.startswith("["):
        try:
            arr = json.loads(s)
            if isinstance(arr, list) and arr:
                s = str(arr[0] or "").strip()
        except Exception:
            pass
    for part in re.split(r"[\s,]+", s):
        part = part.strip().strip('"').strip("'")
        if part.startswith("file://"):
            parsed = urlparse(part)
            p = unquote(parsed.path or "")
            if p and Path(p).exists():
                return p
        elif part.startswith("/") and Path(part).exists():
            return part
    return None


def _resolve_root_conversation_id(cid: str | None) -> str | None:
    """Resolve subagent/child conversation IDs up to their top-level interactive conversation."""
    cur = (cid or "").strip()
    if not cur or not SUMMARIES_DB.is_file():
        return cur or None
    try:
        conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=0.5)
        try:
            visited: set[str] = set()
            for _ in range(6):
                if cur in visited:
                    break
                visited.add(cur)
                parent = ""
                try:
                    row = conn.execute(
                        "SELECT parent_id FROM conversation_parents WHERE conversation_id = ?",
                        (cur,),
                    ).fetchone()
                    if row and row[0]:
                        parent = str(row[0]).strip()
                except sqlite3.Error:
                    pass
                if not parent:
                    row2 = conn.execute(
                        "SELECT parent_conversation_id FROM conversation_summaries WHERE conversation_id = ?",
                        (cur,),
                    ).fetchone()
                    if row2 and row2[0]:
                        parent = str(row2[0]).strip()
                if not parent:
                    break
                cur = parent
        finally:
            conn.close()
    except Exception:
        pass
    return cur or None


def _get_summary_workspace_path(cid: str) -> str | None:
    if not cid or not SUMMARIES_DB.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=0.5)
        try:
            row = conn.execute(
                "SELECT workspace_uris FROM conversation_summaries WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return _uri_to_local_path(str(row[0]))
    except Exception:
        pass
    return None


def _extract_meta_conversation_id(ctx: Context | None) -> str | None:
    """Check standard MCP request _meta for any host-provided conversation/trajectory ID."""
    if ctx is None:
        return None
    try:
        meta = getattr(ctx.request_context, "meta", None)
        if meta is None:
            return None
        raw = meta.model_dump() if hasattr(meta, "model_dump") else (meta if isinstance(meta, dict) else {})
        for key in (
            "conversationId",
            "conversation_id",
            "trajectoryId",
            "trajectory_id",
            "cascadeId",
            "cascade_id",
            "sessionId",
            "session_id",
        ):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # Check nested vendor keys (e.g. com.google.antigravity/*)
        for v in raw.values():
            if isinstance(v, dict):
                for key in ("conversationId", "conversation_id", "trajectoryId", "cascadeId"):
                    val = v.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
    except Exception:
        return None
    return None


def _query_sidebar_summaries(limit: int = 30) -> list[dict[str, Any]]:
    """Read top-level interactive conversation metadata from Antigravity's summaries DB (read-only)."""
    if not SUMMARIES_DB.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=0.5)
        try:
            child_ids: set[str] = set()
            try:
                for (cid_child,) in conn.execute("SELECT conversation_id FROM conversation_parents").fetchall():
                    if cid_child:
                        child_ids.add(str(cid_child))
            except sqlite3.Error:
                pass

            rows = conn.execute(
                """
                SELECT conversation_id, title, last_user_input_time, last_modified_time, workspace_uris
                FROM conversation_summaries
                WHERE (parent_conversation_id IS NULL OR parent_conversation_id = '')
                  AND (killed IS NULL OR killed = 0)
                ORDER BY last_modified_time DESC
                LIMIT ?
                """,
                (max(40, int(limit) * 3),),
            ).fetchall()
        finally:
            conn.close()

        out: list[dict[str, Any]] = []
        for cid, title, u_ts, m_ts, ws_uris in rows:
            if not cid:
                continue
            cid_str = str(cid).strip()
            if not cid_str or cid_str in child_ids:
                continue
            u_ep = _parse_iso_ts(str(u_ts or ""))
            m_ep = _parse_iso_ts(str(m_ts or ""))
            out.append(
                {
                    "conversation_id": cid_str,
                    "title": str(title or "").strip(),
                    "last_user_input_time": str(u_ts or ""),
                    "last_modified_time": str(m_ts or ""),
                    "user_ts": u_ep,
                    "mod_ts": m_ep,
                    "workspace_path": _uri_to_local_path(str(ws_uris or "")) or "",
                }
            )
        out.sort(key=lambda it: max(it["user_ts"], it["mod_ts"]), reverse=True)
        return out[: max(1, int(limit))]
    except Exception:
        return []


def _get_sidebar_title(cid: str) -> str | None:
    if not cid or not SUMMARIES_DB.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=0.5)
        try:
            row = conn.execute(
                "SELECT title FROM conversation_summaries WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0] and str(row[0]).strip():
            return str(row[0]).strip()
    except Exception:
        return None
    return None


def _find_latest_conversation_id(exclude_active_panes: bool = False) -> str | None:
    """Return the best matching top-level interactive conversation ID.

    When exclude_active_panes=True (manual '+' pane open with empty args),
    conversations already displayed in another actively polling Context Card
    pane are deprioritized so multi-window setups bind to the unbound window.
    """
    active_cids, hook_map = _get_runtime_maps()
    candidates: dict[str, float] = {}

    for item in _query_sidebar_summaries(limit=30):
        cid = item["conversation_id"]
        tpath = BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
        mtime_ep = 0.0
        try:
            mtime_ep = tpath.stat().st_mtime
        except OSError:
            if cid not in hook_map:
                continue
        hook_ep = hook_map.get(cid, ("", 0.0))[1]
        score = max(float(item["user_ts"]), float(item["mod_ts"]), mtime_ep, hook_ep)
        candidates[cid] = max(candidates.get(cid, 0.0), score)

    for cid, (_ws, hook_ep) in hook_map.items():
        if hook_ep <= 0:
            continue
        root_cid = _resolve_root_conversation_id(cid) or cid
        tpath = BRAIN_DIR / root_cid / ".system_generated" / "logs" / "transcript.jsonl"
        mtime_ep = 0.0
        try:
            mtime_ep = tpath.stat().st_mtime
        except OSError:
            pass
        candidates[root_cid] = max(candidates.get(root_cid, 0.0), hook_ep, mtime_ep)

    if not candidates and BRAIN_DIR.is_dir():
        try:
            for entry in BRAIN_DIR.iterdir():
                if not entry.is_dir() or entry.name == "tempmediaStorage":
                    continue
                tpath = entry / ".system_generated" / "logs" / "transcript.jsonl"
                try:
                    st = tpath.stat()
                except OSError:
                    continue
                root_cid = _resolve_root_conversation_id(entry.name) or entry.name
                candidates[root_cid] = max(candidates.get(root_cid, 0.0), st.st_mtime)
        except OSError:
            pass

    if not candidates:
        return None

    ranked = sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)
    top_cid, top_ts = ranked[0]

    if exclude_active_panes and active_cids:
        for cid, ts in ranked:
            if cid not in active_cids:
                # Prefer the newest unbound window as long as it isn't stale by >4h compared to top
                if (top_ts - ts) <= 14_400.0:
                    return cid
                break

    return top_cid


def _format_model(model_id: str | None) -> tuple[str, int]:
    """Return (human_readable_name, context_limit_tokens)."""
    if not model_id:
        return ("Unknown model", 512_000)

    low = model_id.lower()

    effort_map = {
        "-xhigh": " (Extra)",
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

    rules: list[tuple[str, str, int]] = [
        ("gemini-4-neon", "Gemini 4 Neon", 512_000),
        ("gemini-4-carbon", "Gemini 4 Carbon", 1_000_000),
        ("gemini-4-argon", "Gemini 4 Argon", 1_000_000),
        ("gemini-4-hydrogen", "Gemini 4 Hydrogen", 1_000_000),
        ("gemini-3.8-flash", "Gemini 3.8 Flash", 1_000_000),
        ("gemini-3.7-flash", "Gemini 3.7 Flash", 1_000_000),
        ("gemini-3.6-flash", "Gemini 3.6 Flash", 1_000_000),
        ("gemini-3.1-pro", "Gemini 3.1 Pro", 1_000_000),
        ("gemini-3", "Gemini 3", 1_000_000),
        ("gemini-2.5", "Gemini 2.5", 1_000_000),
        ("claude-opus-5.5", "Claude Opus 5.5", 1_000_000),
        ("claude-sonnet-5.5", "Claude Sonnet 5.5", 1_000_000),
        ("claude-opus-4.6", "Claude Opus 4.6", 200_000),
        ("claude-sonnet-4.6", "Claude Sonnet 4.6", 200_000),
        ("claude-opus-4", "Claude Opus 4", 200_000),
        ("claude-sonnet-4", "Claude Sonnet 4", 200_000),
        ("gpt-oss-120b", "GPT-OSS-120b", 128_000),
    ]
    for prefix, display, limit in rules:
        if base.startswith(prefix):
            return (f"{display}{effort_suffix}", limit)

    pretty = " ".join(part.capitalize() for part in base.replace("_", "-").split("-") if part)
    return (f"{pretty}{effort_suffix}", 512_000)


def _est_tokens_from_text(text: str | None) -> int:
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


def _est_tokens_from_file(path: Path) -> tuple[int, int]:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        return (len(raw.encode("utf-8")), _est_tokens_from_text(raw))
    except OSError:
        return (0, 0)


def _extract_title(rows: list[dict[str, Any]], cid: str) -> str:
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


def _find_git_root(path_str: str) -> Path | None:
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


def _get_git_branch(repo_root: Path) -> str | None:
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


def _detect_workspace(rows: list[dict[str, Any]], fallback_ws_path: str | None = None) -> dict[str, Any]:
    home = Path.home()
    gemini_dir = str(home / ".gemini")
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
                    root = _find_git_root(cand)
                    if root is not None:
                        key = str(root)
                        candidate_counts[key] = candidate_counts.get(key, 0) + 1

    best_root: Path | None = None
    if candidate_counts:
        best_root = Path(max(candidate_counts.items(), key=lambda kv: kv[1])[0])
    elif fallback_ws_path:
        fp = Path(fallback_ws_path)
        if fp.exists():
            best_root = _find_git_root(str(fp)) or (fp if fp.is_dir() else fp.parent)

    if best_root is not None:
        branch = _get_git_branch(best_root)
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


def _collect_memory_files(workspace_path: str | None) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    global_gemini = CONFIG_DIR / "GEMINI.md"
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
        size_bytes, tok = _est_tokens_from_file(p)
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


def _collect_skills_catalog() -> tuple[int, list[dict[str, Any]]]:
    global _SKILLS_CATALOG_CACHE
    now = time.time()
    if _SKILLS_CATALOG_CACHE and (now - _SKILLS_CATALOG_CACHE[0]) < 30.0:
        return (_SKILLS_CATALOG_CACHE[1], _SKILLS_CATALOG_CACHE[2])

    skill_paths: list[Path] = []
    for root in (BUILTIN_SKILLS_DIR, CONFIG_DIR / "skills"):
        if root.is_dir():
            skill_paths.extend(sorted(root.glob("*/SKILL.md")))
    plugins_dir = CONFIG_DIR / "plugins"
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
        tok = max(20, _est_tokens_from_text(fm))
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

    _SKILLS_CATALOG_CACHE = (now, total_meta_tokens, catalog)
    return (total_meta_tokens, catalog)


def _collect_outputs(cid: str) -> list[dict[str, Any]]:
    conv_dir = BRAIN_DIR / cid
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


def _parse_transcript_cached(
    cid: str,
    tpath: Path,
    mtime_ns: int,
    size: int,
    ws_hint: str | None = None,
) -> dict[str, Any]:
    hint_key = ws_hint or ""
    cached = _TRANSCRIPT_CACHE.get(cid)
    if cached and cached[0] == mtime_ns and cached[1] == size and cached[2] == hint_key:
        return cached[3]
    data = _parse_transcript(cid, tpath, ws_hint)
    _TRANSCRIPT_CACHE[cid] = (mtime_ns, size, hint_key, data)
    return data


def _parse_transcript(cid: str, tpath: Path, ws_hint: str | None = None) -> dict[str, Any]:
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

    title = _extract_title(rows, cid)
    env = _detect_workspace(rows, ws_hint)
    memory_files = _collect_memory_files(env["workspace_path"])
    skills_meta_tokens, skills_catalog = _collect_skills_catalog()
    outputs = _collect_outputs(cid)

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
    model_display, model_limit = _format_model(model_id)

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
                        sz, tok = _est_tokens_from_file(sp)
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
            pre_first_est += _est_tokens_from_text(r.get("content"))
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
                between_est += _est_tokens_from_text(r.get("content")) + 258 * len(r.get("media") or [])
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
                # Context shrank without a checkpoint (e.g. model pruned prior thinking blocks)
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
            prev_text_est = _est_tokens_from_text(prev_r.get("content"))
            prev_tcalls = prev_r.get("tool_calls") or []
            prev_tc_est = _est_tokens_from_text(json.dumps(prev_tcalls, ensure_ascii=False)) if prev_tcalls else 0
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
                    weights["thinking"] += _est_tokens_from_text(prev_r.get("thinking"))
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
                est = _est_tokens_from_text(step.get("content")) + 258 * len(step.get("media") or [])
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
            trailing_est += _est_tokens_from_text(step.get("content")) + 258 * len(step.get("media") or [])
        since_last_update_tokens = last_output_tokens + trailing_est

    elif last_cp_idx >= 0:
        # Immediately after /compact (CHECKPOINT written, 0 planner steps since checkpoint)
        cp_row = rows[last_cp_idx]
        cp_out = int(cp_row.get("output_tokens") or 0)
        checkpoint_tokens = cp_out if cp_out > 0 else _est_tokens_from_text(cp_row.get("content"))
        active_input_total = system_baseline_total + checkpoint_tokens
        for j in range(active_start, len(rows)):
            step = rows[j]
            since_last_update_tokens += _est_tokens_from_text(step.get("content")) + 258 * len(step.get("media") or [])

    elif rows:
        for r in rows:
            since_last_update_tokens += _est_tokens_from_text(r.get("content")) + 258 * len(r.get("media") or [])

    skills_total_tokens = skills_base_tokens + skills_loaded_tokens
    total_used = active_input_total + since_last_update_tokens
    free_space = max(0, model_limit - total_used)
    used_pct = round((total_used / model_limit) * 100, 1) if model_limit > 0 else 0.0
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

    categories.append(
        {
            "key": "free_space",
            "label": "Free space",
            "tokens": free_space,
            "pct": round((free_space / model_limit) * 100, 1) if model_limit else 0.0,
            "color": "var(--ctx-free, #323238)",
            "hint": "Remaining tokens before hitting the model context limit",
            "is_free": True,
        }
    )

    last_ts = None
    for r in reversed(rows):
        if r.get("created_at"):
            last_ts = r["created_at"]
            break

    turn_info = _detect_turn_state(cid, rows)
    dialogue_text = _extract_recent_dialogue(rows, env)
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

    with _SUGGEST_LOCK:
        preds_copy = dict(_RECENT_PREDICTIONS.get(cid) or {})
    if preds_copy:
        habits.capture_implicit_feedback(cid, rows, env.get("workspace_name") or "", preds_copy)

    return {
        "conversation_id": cid,
        "title": title,
        "environment": env,
        "model_id": model_id,
        "model_display": model_display,
        "used_tokens": total_used,
        "active_input_tokens": active_input_total,
        "limit_tokens": model_limit,
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


def _load_prefs() -> dict[str, Any]:
    global _PREFS_CACHE
    if _PREFS_CACHE is not None:
        return dict(_PREFS_CACHE)
    prefs: dict[str, Any] = {"auto_suggest": True}
    try:
        if PREFS_PATH.is_file():
            raw = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                prefs.update(raw)
    except Exception:
        pass
    _PREFS_CACHE = dict(prefs)
    return prefs


def _save_prefs(updates: dict[str, Any]) -> dict[str, Any]:
    global _PREFS_CACHE
    prefs = _load_prefs()
    prefs.update(updates)
    _PREFS_CACHE = dict(prefs)
    try:
        PREFS_PATH.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return prefs


def _clean_user_text(raw: str) -> str:
    m = USER_REQUEST_RE.search(raw)
    text = m.group(1) if m else raw
    text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
    text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
    return text.strip()


def _detect_turn_state(cid: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
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


def _extract_recent_dialogue(rows: list[dict[str, Any]], env: dict[str, Any]) -> str:
    turns: list[tuple[str, str]] = []
    pending_actions: list[str] = []

    for r in rows[-80:]:
        rtype = r.get("type")
        if rtype == "USER_INPUT":
            pending_actions.clear()
            body = _clean_user_text(r.get("content") or "")
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


def _strip_outer_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'", "“", "”", "「", "」"):
        s = s[1:-1].strip()
    return s


def _parse_suggest_json(raw: str) -> tuple[str, list[str]]:
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
        # Last-resort fallback: treat first non-empty line as primary suggestion
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        return (_strip_outer_quotes(first_line), [])

    primary = _strip_outer_quotes(str(obj.get("primary") or ""))
    raw_alts = obj.get("alternatives") or []
    alts: list[str] = []
    seen = {primary.lower()} if primary else set()
    if isinstance(raw_alts, list):
        for item in raw_alts:
            cand = _strip_outer_quotes(str(item or ""))
            if cand and cand.lower() not in seen:
                seen.add(cand.lower())
                alts.append(cand)
            if len(alts) >= 2:
                break
    return (primary, alts)


def _call_suggest_llm(
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
    primary, alts = _parse_suggest_json(content)
    if not primary:
        raise ValueError("Empty primary suggestion returned")
    return (primary, alts, model_used, latency_ms)


def _run_suggestion_worker(
    cid: str,
    turn_key: str,
    req_seq: int,
    dialogue_text: str,
    assistant_tail: str,
    workspace: str,
    force_variation: bool,
) -> None:
    try:
        primary, alts, model_used, latency_ms = _call_suggest_llm(
            dialogue_text,
            assistant_tail,
            workspace,
            force_variation=force_variation,
        )
        with _SUGGEST_LOCK:
            cur = _SUGGEST_STATE.get(cid)
            if cur and cur.get("turn_key") == turn_key and cur.get("req_seq") == req_seq:
                _SUGGEST_STATE[cid] = {
                    "turn_key": turn_key,
                    "req_seq": req_seq,
                    "status": "ready",
                    "primary": primary,
                    "alternatives": alts,
                    "model": model_used,
                    "error": None,
                    "latency_ms": latency_ms,
                    "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                }
                preds = _RECENT_PREDICTIONS.setdefault(cid, {})
                preds[turn_key] = primary
                if len(preds) > 20:
                    oldest = next(iter(preds))
                    preds.pop(oldest, None)
    except Exception as exc:
        err_msg = str(exc)
        if len(err_msg) > 120:
            err_msg = err_msg[:117] + "…"
        with _SUGGEST_LOCK:
            cur = _SUGGEST_STATE.get(cid)
            if cur and cur.get("turn_key") == turn_key and cur.get("req_seq") == req_seq:
                _SUGGEST_STATE[cid] = {
                    "turn_key": turn_key,
                    "req_seq": req_seq,
                    "status": "error",
                    "primary": "",
                    "alternatives": [],
                    "model": DEFAULT_SUGGEST_MODEL,
                    "error": err_msg,
                    "latency_ms": None,
                    "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                }


def _resolve_suggestion(
    cid: str,
    turn_info: dict[str, Any],
    dialogue_text: str,
    assistant_tail: str,
    workspace: str,
    force: bool = False,
) -> dict[str, Any]:
    auto_suggest = bool(_load_prefs().get("auto_suggest", True))
    state = turn_info.get("state", "idle")
    turn_key = turn_info.get("turn_key") or (turn_info.get("latest_reply_turn_key") if force else "")

    if not turn_key or not dialogue_text:
        return {
            "status": "working" if state == "working" else "idle",
            "turn_key": "",
            "primary": "",
            "alternatives": [],
            "auto_suggest": auto_suggest,
            "model": DEFAULT_SUGGEST_MODEL,
            "error": None,
            "latency_ms": None,
        }

    if state != "user_turn" and not force:
        return {
            "status": "working",
            "turn_key": "",
            "primary": "",
            "alternatives": [],
            "auto_suggest": auto_suggest,
            "model": DEFAULT_SUGGEST_MODEL,
            "error": None,
            "latency_ms": None,
        }

    if not auto_suggest and not force:
        with _SUGGEST_LOCK:
            cur = _SUGGEST_STATE.get(cid)
            if cur and cur.get("turn_key") == turn_key and cur.get("status") == "ready":
                return {**cur, "auto_suggest": False}
        return {
            "status": "disabled",
            "turn_key": turn_key,
            "primary": "",
            "alternatives": [],
            "auto_suggest": False,
            "model": DEFAULT_SUGGEST_MODEL,
            "error": None,
            "latency_ms": None,
        }

    if not habits.has_llm_configured():
        return {
            "status": "needs_key",
            "turn_key": turn_key,
            "primary": "",
            "alternatives": [],
            "auto_suggest": auto_suggest,
            "model": DEFAULT_SUGGEST_MODEL,
            "error": None,
            "latency_ms": None,
        }

    with _SUGGEST_LOCK:
        cur = _SUGGEST_STATE.get(cid)
        if not force and cur and cur.get("turn_key") == turn_key and cur.get("status") in ("generating", "ready", "error"):
            return {**cur, "auto_suggest": auto_suggest}

        req_seq = int((cur or {}).get("req_seq") or 0) + 1
        pending = {
            "turn_key": turn_key,
            "req_seq": req_seq,
            "status": "generating",
            "primary": (cur.get("primary") if (force and cur and cur.get("turn_key") == turn_key) else ""),
            "alternatives": (cur.get("alternatives") if (force and cur and cur.get("turn_key") == turn_key) else []),
            "model": DEFAULT_SUGGEST_MODEL,
            "error": None,
            "latency_ms": None,
            "generated_at": None,
        }
        _SUGGEST_STATE[cid] = pending

    t = threading.Thread(
        target=_run_suggestion_worker,
        args=(cid, turn_key, req_seq, dialogue_text, assistant_tail, workspace, force),
        daemon=True,
    )
    t.start()
    return {**pending, "auto_suggest": auto_suggest}


def _build_snapshot(
    conversation_id: str | None = None,
    ctx: Context | None = None,
    force_suggest: bool = False,
    exclude_active_panes: bool = False,
    bind_mode_override: str | None = None,
) -> dict[str, Any]:
    explicit_raw = (conversation_id or "").strip() or (_extract_meta_conversation_id(ctx) or "").strip()
    if explicit_raw:
        target_id = _resolve_root_conversation_id(explicit_raw) or explicit_raw
        bind_mode = bind_mode_override if bind_mode_override in ("auto", "explicit") else "explicit"
    else:
        target_id = _find_latest_conversation_id(exclude_active_panes=exclude_active_panes)
        bind_mode = bind_mode_override if bind_mode_override in ("auto", "explicit") else "auto"

    if not target_id:
        return {
            "empty": True,
            "refreshed_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    _active_cids, hook_map = _get_runtime_maps()
    ws_hint = hook_map.get(target_id, ("", 0.0))[0] or _get_summary_workspace_path(target_id)

    tpath = BRAIN_DIR / target_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not tpath.is_file():
        # Brand new conversation whose transcript file hasn't flushed yet
        auto_suggest = bool(_load_prefs().get("auto_suggest", True))
        env = _detect_workspace([], ws_hint)
        if not env.get("workspace_path"):
            env["workspace_display"] = "Initializing…"
        return {
            "empty": False,
            "conversation_id": target_id,
            "bind_mode": bind_mode,
            "title": _get_sidebar_title(target_id) or target_id[:8],
            "environment": env,
            "model_id": None,
            "model_display": "Starting…",
            "used_tokens": 0,
            "active_input_tokens": 0,
            "limit_tokens": 512_000,
            "used_pct": 0.0,
            "free_tokens": 512_000,
            "cache_read_tokens": 0,
            "input_uncached_tokens": 0,
            "last_output_tokens": 0,
            "cache_hit_pct": 0.0,
            "checkpoint_count": 0,
            "step_count": 0,
            "active_step_count": 0,
            "categories": [
                {
                    "key": "free_space",
                    "label": "Free space",
                    "tokens": 512_000,
                    "pct": 100.0,
                    "color": "var(--ctx-free, #323238)",
                    "is_free": True,
                }
            ],
            "memory_files": _collect_memory_files(env.get("workspace_path")),
            "loaded_skills": [],
            "total_skills_count": 0,
            "outputs": [],
            "last_event_at": None,
            "refreshed_at": datetime.now(tz=timezone.utc).isoformat(),
            "suggestion": {
                "status": "idle",
                "turn_key": "",
                "primary": "",
                "alternatives": [],
                "auto_suggest": auto_suggest,
                "model": DEFAULT_SUGGEST_MODEL,
                "error": None,
                "latency_ms": None,
            },
        }

    habits.ensure_initialized_async()
    st = tpath.stat()
    snapshot = _parse_transcript_cached(target_id, tpath, st.st_mtime_ns, st.st_size, ws_hint)
    turn_info = snapshot.get("_turn_info") or {"state": "idle", "turn_key": "", "latest_reply_turn_key": ""}
    dialogue_text = snapshot.get("_dialogue_text") or ""
    assistant_tail = snapshot.get("_assistant_tail") or ""
    ws_name = (snapshot.get("environment") or {}).get("workspace_name") or ""
    suggestion = _resolve_suggestion(
        target_id,
        turn_info,
        dialogue_text,
        assistant_tail,
        ws_name,
        force=force_suggest,
    )

    out = {k: v for k, v in snapshot.items() if not k.startswith("_")}
    sidebar_title = _get_sidebar_title(target_id)
    if sidebar_title:
        out["title"] = sidebar_title
    out["empty"] = False
    out["bind_mode"] = bind_mode
    out["suggestion"] = {k: v for k, v in suggestion.items() if k != "req_seq"}
    out["habits"] = habits.get_habits_status()
    return out


def _fmt_k(n: int) -> str:
    if n >= 1_000_000:
        val = n / 1_000_000
        return f"{val:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        val = n / 1_000
        return f"{val:.1f}K".replace(".0K", "K")
    return str(n)


PANE_META = {
    "ui": {"resourceUri": APP_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "Context"},
        ]
    },
}

APP_ONLY_META = {
    "ui": {"visibility": ["app"]},
}


@mcp.tool(
    name="open_context_card",
    title="Context",
    description="Open and bind this window's Context Card in the right side panel to the given conversation_id.",
    meta=PANE_META,
)
def open_context_card(conversation_id: str | None = None, ctx: Context | None = None) -> types.CallToolResult:
    has_explicit = bool((conversation_id or "").strip() or (_extract_meta_conversation_id(ctx) or "").strip())
    snap = _build_snapshot(
        conversation_id,
        ctx,
        exclude_active_panes=(not has_explicit),
        bind_mode_override="explicit" if has_explicit else "auto",
    )
    if snap.get("empty"):
        text = "No Antigravity conversation transcripts found yet."
    else:
        if has_explicit:
            _record_explicit_open(str(snap["conversation_id"]))
        else:
            _record_manual_auto_open()
        text = (
            f"Context Card ({snap['conversation_id'][:8]}) — {snap['model_display']}: "
            f"{snap['used_pct']}% used ({_fmt_k(snap['used_tokens'])} / {_fmt_k(snap['limit_tokens'])}), "
            f"cache hit {snap['cache_hit_pct']}%, {snap['checkpoint_count']} checkpoints."
        )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=snap,
    )


@mcp.tool(
    name="app_get_context_snapshot",
    title="Refresh Context Snapshot",
    description="Internal app-only tool to fetch the latest context snapshot for this window's bound conversation.",
    meta=APP_ONLY_META,
)
def app_get_context_snapshot(
    conversation_id: str | None = None,
    client_id: str | None = None,
    bind_mode: str | None = None,
    ctx: Context | None = None,
) -> types.CallToolResult:
    mode_norm = bind_mode if bind_mode in ("auto", "explicit") else None
    snap = _build_snapshot(conversation_id, ctx, bind_mode_override=mode_norm)
    if not snap.get("empty") and snap.get("conversation_id"):
        _record_client_heartbeat(client_id, str(snap["conversation_id"]), str(snap.get("bind_mode") or "explicit"))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=snap,
    )


@mcp.tool(
    name="app_regenerate_suggestion",
    title="Regenerate Suggested Reply",
    description="Internal app-only tool to force-regenerate next-reply suggestions for the bound conversation.",
    meta=APP_ONLY_META,
)
def app_regenerate_suggestion(conversation_id: str | None = None, ctx: Context | None = None) -> types.CallToolResult:
    snap = _build_snapshot(conversation_id, ctx, force_suggest=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=snap,
    )


def _extract_title_fast(tpath: Path, cid: str) -> str:
    try:
        with tpath.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(12):
                line = f.readline()
                if not line:
                    break
                s = line.strip()
                if not s:
                    continue
                try:
                    r = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if r.get("type") == "USER_INPUT":
                    body = _clean_user_text(r.get("content") or "")
                    for ln in body.splitlines():
                        ln = ln.strip()
                        if ln and not ln.startswith("<"):
                            return ln[:44] + ("…" if len(ln) > 44 else "")
    except OSError:
        pass
    return cid[:8]


def _list_recent_conversations(limit: int = 10) -> list[dict[str, Any]]:
    sidebar_items = _query_sidebar_summaries(limit=max(limit * 2, 20))
    if sidebar_items:
        out: list[dict[str, Any]] = []
        for item in sidebar_items:
            cid = item["conversation_id"]
            tpath = BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
            if not tpath.is_file():
                continue
            title = item["title"] or _extract_title_fast(tpath, cid)
            out.append(
                {
                    "conversation_id": cid,
                    "short_id": cid[:8],
                    "title": title,
                    "updated_at": item["last_modified_time"] or item["last_user_input_time"],
                }
            )
            if len(out) >= limit:
                break
        if out:
            return out

    if not BRAIN_DIR.is_dir():
        return []
    items: list[tuple[int, str, Path]] = []
    seen_roots: set[str] = set()
    try:
        for entry in BRAIN_DIR.iterdir():
            if not entry.is_dir() or entry.name == "tempmediaStorage":
                continue
            root_cid = _resolve_root_conversation_id(entry.name) or entry.name
            if root_cid != entry.name or root_cid in seen_roots:
                continue
            tpath = entry / ".system_generated" / "logs" / "transcript.jsonl"
            try:
                st = tpath.stat()
            except OSError:
                continue
            seen_roots.add(root_cid)
            items.append((st.st_mtime_ns, root_cid, tpath))
    except OSError:
        return []

    items.sort(key=lambda x: x[0], reverse=True)
    out = []
    for mtime_ns, cid, tpath in items[:limit]:
        cached = _TRANSCRIPT_CACHE.get(cid)
        if cached:
            title = cached[3].get("title") or cid[:8]
        else:
            title = _extract_title_fast(tpath, cid)
        out.append(
            {
                "conversation_id": cid,
                "short_id": cid[:8],
                "title": title,
                "updated_at": datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc).isoformat(),
            }
        )
    return out


@mcp.tool(
    name="app_list_conversations",
    title="List Recent Conversations",
    description="Internal app-only tool to list recent Antigravity conversations so the user can re-bind if needed.",
    meta=APP_ONLY_META,
)
def app_list_conversations(limit: int = 10) -> types.CallToolResult:
    convs = _list_recent_conversations(max(1, min(20, int(limit or 10))))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent={"conversations": convs},
    )


@mcp.tool(
    name="app_set_suggest_enabled",
    title="Toggle Auto-Suggest",
    description="Internal app-only tool to enable or disable automatic next-reply suggestions.",
    meta=APP_ONLY_META,
)
def app_set_suggest_enabled(
    enabled: bool,
    conversation_id: str | None = None,
    ctx: Context | None = None,
) -> types.CallToolResult:
    _save_prefs({"auto_suggest": bool(enabled)})
    snap = _build_snapshot(conversation_id, ctx)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=snap,
    )


@mcp.tool(
    name="app_record_suggestion_feedback",
    title="Record Suggestion Feedback",
    description="Internal app-only tool to record accepted or edited suggestion feedback for local habit learning.",
    meta=APP_ONLY_META,
)
def app_record_suggestion_feedback(
    sent_text: str,
    predicted_primary: str = "",
    turn_key: str = "",
    edited: bool = False,
    conversation_id: str | None = None,
    ctx: Context | None = None,
) -> types.CallToolResult:
    target_id = (conversation_id or "").strip() or _extract_meta_conversation_id(ctx) or _find_latest_conversation_id() or ""
    assistant_tail = ""
    workspace = ""
    if target_id:
        cached = _TRANSCRIPT_CACHE.get(target_id)
        if cached:
            assistant_tail = cached[3].get("_assistant_tail") or ""
            workspace = (cached[3].get("environment") or {}).get("workspace_name") or ""
    outcome = "edited" if edited else "accepted"
    tkey = (turn_key or "").strip() or f"{target_id}:fb:{int(time.time())}"
    habits.record_example(
        turn_key=tkey,
        conversation_id=target_id,
        workspace=workspace,
        assistant_tail=assistant_tail,
        predicted_primary=predicted_primary,
        actual_reply=sent_text,
        outcome=outcome,
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent={"habits": habits.get_habits_status()},
    )


@mcp.tool(
    name="app_relearn_habits",
    title="Re-learn User Style Profile",
    description="Internal app-only tool to re-scan recent chats and re-distill the user's style profile.",
    meta=APP_ONLY_META,
)
def app_relearn_habits(
    conversation_id: str | None = None,
    ctx: Context | None = None,
) -> types.CallToolResult:
    habits.relearn_now_async()
    snap = _build_snapshot(conversation_id, ctx)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=snap,
    )


@mcp.tool(
    name="app_reset_habits",
    title="Reset Learned Habits",
    description="Internal app-only tool to clear all locally learned suggestion habits and examples.",
    meta=APP_ONLY_META,
)
def app_reset_habits(
    conversation_id: str | None = None,
    ctx: Context | None = None,
) -> types.CallToolResult:
    habits.reset_habits()
    snap = _build_snapshot(conversation_id, ctx)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=snap,
    )


@mcp.tool(
    name="app_save_llm_key",
    title="Save BYOK Suggestion API Key",
    description="Internal app-only tool to persist a local BYOK API key in plugin_data/llm_config.json (0600).",
    meta=APP_ONLY_META,
)
def app_save_llm_key(
    api_key: str,
    provider: str = "gemini",
    base_url: str = "",
    model: str = "",
    conversation_id: str | None = None,
    ctx: Context | None = None,
) -> types.CallToolResult:
    habits.save_llm_config(api_key=api_key, provider=provider, base_url=base_url, model=model)
    with _SUGGEST_LOCK:
        _SUGGEST_STATE.clear()
    snap = _build_snapshot(conversation_id, ctx, force_suggest=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=snap,
    )


@mcp.resource(
    APP_URI,
    name="Context Card UI",
    title="Context Card",
    description="Interactive Claude Desktop-style context window monitor for the active window.",
    mime_type="text/html;profile=mcp-app",
    meta={"ui": {"prefersBorder": False}},
)
def context_card_html() -> str:
    html_path = Path(os.environ.get("CONTEXT_CARD_APP_HTML", str(DEFAULT_HTML_PATH)))
    if not html_path.is_file():
        return (
            "<!doctype html><html><body style='font-family:sans-serif;padding:16px'>"
            "<h3>Context Card UI not built</h3>"
            "<p>Run <code>npm run build</code> inside the plugin's <code>ui/</code> folder.</p>"
            "</body></html>"
        )
    return html_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    mcp.run(transport="stdio")
