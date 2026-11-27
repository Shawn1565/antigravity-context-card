#!/usr/bin/env python3
"""Antigravity Context Card MCP App server.

Reads Antigravity's documented conversation transcripts under
~/.gemini/antigravity/brain/<conversation-id>/.system_generated/logs/transcript.jsonl
to render a live Claude Desktop-style Context panel bound to each window's own conversation.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
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
import core  # noqa: E402
import habits  # noqa: E402

DEFAULT_HTML_PATH = PLUGIN_ROOT / "dist" / "app.html"
PREFS_PATH = habits.DATA_DIR / "prefs.json"
RUNTIME_DB_PATH = habits.DATA_DIR / "runtime_state.db"
BRAIN_DIR = habits.BRAIN_DIR
ANNOTATIONS_DIR = habits.AGY_HOME / "annotations"
SUMMARIES_DB = habits.SUMMARIES_DB
CONFIG_DIR = Path.home() / ".gemini" / "config"
BUILTIN_SKILLS_DIR = habits.AGY_HOME / "builtin" / "skills"

_VIEW_TIME_RE = re.compile(r"last_user_view_time\s*:\s*\{\s*seconds\s*:\s*(\d+)(?:\s+nanos\s*:\s*(\d+))?\s*\}")
_ARCHIVED_RE = re.compile(r"\barchived\s*:\s*true\b")

DEFAULT_SUGGEST_MODEL = habits.DEFAULT_GEMINI_MODEL
SERVER_PID = os.getpid()
SERVER_STARTED_AT = time.time()
ACTIVE_CLIENT_TTL_SEC = 5.5

SAFE_CID_RE = core.SAFE_CID_RE
MAX_CACHE_ENTRIES = core.MAX_CACHE_ENTRIES

# Caches keyed by (cid -> (mtime_ns, size, ws_hint, parsed_dict))
_TRANSCRIPT_CACHE: dict[str, tuple[int, int, str, dict[str, Any]]] = {}

_RUNTIME_DB_LOCK = threading.Lock()
_RUNTIME_DB_READY = False
_SUGGEST_LOCK = threading.Lock()
_SUGGEST_STATE: dict[str, dict[str, Any]] = {}
_RECENT_PREDICTIONS: dict[str, dict[str, str]] = {}
_PREFS_CACHE: dict[str, Any] | None = None

mcp = FastMCP("context-card-monitor")


def _sanitize_cid(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if not s or ".." in s or not SAFE_CID_RE.match(s):
        return None
    try:
        base_resolved = BRAIN_DIR.resolve()
        cand_resolved = (BRAIN_DIR / s).resolve()
        if cand_resolved.parent != base_resolved:
            return None
    except OSError:
        return None
    return s


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


def _ensure_server_life_row(conn: sqlite3.Connection, *, force: bool = False) -> None:
    """Register this running server's PID in server_life (or self-heal if stale/dead)."""
    if not force:
        row = conn.execute("SELECT pid FROM server_life WHERE id = 1").fetchone()
        if row:
            cur_pid = int(row[0] or 0)
            if cur_pid == SERVER_PID or _pid_alive(cur_pid):
                return
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


def _open_runtime_db() -> sqlite3.Connection:
    global _RUNTIME_DB_READY
    RUNTIME_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        conn.commit()
        _RUNTIME_DB_READY = True
    return conn


def _init_runtime_state() -> None:
    """Register active MCP server process (called only from __main__, never on import)."""
    try:
        with _RUNTIME_DB_LOCK:
            conn = _open_runtime_db()
            try:
                _ensure_server_life_row(conn, force=True)
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _record_explicit_open(cid: str) -> None:
    if not cid:
        return
    now = time.time()
    try:
        with _RUNTIME_DB_LOCK:
            conn = _open_runtime_db()
            try:
                _ensure_server_life_row(conn, force=False)
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
                _ensure_server_life_row(conn, force=False)
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
                _ensure_server_life_row(conn, force=False)
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
    cur = _sanitize_cid(cid) or ""
    if not cur or not SUMMARIES_DB.is_file():
        return _sanitize_cid(cur)
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
    return _sanitize_cid(cur)


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


def _get_headless_cli_cids() -> set[str]:
    """Return conversation IDs created by headless `agy -p` CLI invocations."""
    if not SUMMARIES_DB.is_file():
        return set()
    out: set[str] = set()
    try:
        conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=0.4)
        try:
            for (cid,) in conn.execute(
                "SELECT conversation_id FROM conversation_summaries WHERE source = 'CORTEX_TRAJECTORY_SOURCE_CLI'"
            ).fetchall():
                if cid:
                    out.add(str(cid).strip())
        finally:
            conn.close()
    except Exception:
        pass
    return out


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
                SELECT conversation_id, title, last_user_input_time, last_modified_time, workspace_uris, source
                FROM conversation_summaries
                WHERE (parent_conversation_id IS NULL OR parent_conversation_id = '')
                  AND (killed IS NULL OR killed = 0)
                  AND (source IS NULL OR source != 'CORTEX_TRAJECTORY_SOURCE_CLI')
                ORDER BY last_modified_time DESC
                LIMIT ?
                """,
                (max(40, int(limit) * 3),),
            ).fetchall()
        finally:
            conn.close()

        out: list[dict[str, Any]] = []
        for cid, title, u_ts, m_ts, ws_uris, _src in rows:
            if not cid:
                continue
            cid_str = str(cid).strip()
            if not cid_str or cid_str in child_ids:
                continue
            ws_local = _uri_to_local_path(str(ws_uris or "")) or ""
            if ws_local.startswith("/tmp/"):
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
                    "workspace_path": ws_local,
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


def _get_user_view_times(limit: int = 40) -> tuple[dict[str, float], set[str]]:
    """Read per-conversation last_user_view_time from ~/.gemini/antigravity/annotations/*.pbtxt.

    Antigravity updates each conversation's annotation file whenever the user views or
    focuses that window, making it a far more accurate signal for manual '+' opens than
    transcript mtime (which is skewed by background running agents).
    """
    view_map: dict[str, float] = {}
    archived: set[str] = set()
    if not ANNOTATIONS_DIR.is_dir():
        return (view_map, archived)
    try:
        entries: list[tuple[float, str, Path]] = []
        for entry in ANNOTATIONS_DIR.iterdir():
            if not entry.name.endswith(".pbtxt"):
                continue
            cid = _sanitize_cid(entry.name[:-6])
            if not cid:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, cid, entry))
        entries.sort(key=lambda x: x[0], reverse=True)
        for mtime, cid, path in entries[: max(1, int(limit))]:
            try:
                raw = path.read_text(encoding="utf-8", errors="ignore")[:1024]
            except OSError:
                continue
            if _ARCHIVED_RE.search(raw):
                archived.add(cid)
                continue
            m = _VIEW_TIME_RE.search(raw)
            if m:
                sec = int(m.group(1))
                nanos = int(m.group(2) or 0)
                view_map[cid] = sec + (nanos / 1e9)
            else:
                view_map[cid] = mtime
    except OSError:
        pass
    return (view_map, archived)


def _find_latest_conversation_id(exclude_active_panes: bool = False) -> str | None:
    """Return the best matching top-level interactive conversation ID.

    When exclude_active_panes=True (manual '+' pane open with empty args):
      1. Prioritize Antigravity's `annotations/<cid>.pbtxt` `last_user_view_time`
         so the window the user is actually looking at beats busy background agents.
      2. Deprioritize conversations already displayed in another actively polling
         Context Card pane, while avoiding jumping to stale windows (>5 min older
         than the most recently viewed window, e.g. on quick close-and-reopen).
      3. Fall back to transcript/summary activity timestamps when no view telemetry
         is available.
    """
    active_cids, hook_map = _get_runtime_maps()
    view_map, archived_cids = _get_user_view_times(limit=40)
    cli_cids = _get_headless_cli_cids()
    candidates: dict[str, float] = {}
    sidebar_cids: set[str] = set()

    for item in _query_sidebar_summaries(limit=30):
        cid = item["conversation_id"]
        if cid in archived_cids:
            continue
        sidebar_cids.add(cid)
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
        if root_cid in cli_cids or root_cid in archived_cids:
            continue
        tpath = BRAIN_DIR / root_cid / ".system_generated" / "logs" / "transcript.jsonl"
        mtime_ep = 0.0
        try:
            mtime_ep = tpath.stat().st_mtime
        except OSError:
            pass
        candidates[root_cid] = max(candidates.get(root_cid, 0.0), hook_ep, mtime_ep)

    if exclude_active_panes and view_map:
        view_candidates: dict[str, float] = {}
        for cid, v_ts in view_map.items():
            if v_ts <= 0 or cid in archived_cids:
                continue
            root_cid = _resolve_root_conversation_id(cid) or cid
            if root_cid in cli_cids or root_cid in archived_cids:
                continue
            tpath = BRAIN_DIR / root_cid / ".system_generated" / "logs" / "transcript.jsonl"
            if root_cid not in candidates and root_cid not in sidebar_cids and not tpath.is_file():
                continue
            view_candidates[root_cid] = max(view_candidates.get(root_cid, 0.0), v_ts)

        if view_candidates:
            v_ranked = sorted(view_candidates.items(), key=lambda kv: kv[1], reverse=True)
            top_v_cid, top_v_ts = v_ranked[0]
            if (time.time() - top_v_ts) <= 43_200.0:
                if active_cids:
                    for cid, ts in v_ranked:
                        if cid not in active_cids:
                            # Only prefer an unbound window if it was also viewed recently
                            # (within 5 min of the top viewed window), preventing jumps to stale tabs
                            # when the user closes & immediately reopens Context Card in the same window.
                            if (top_v_ts - ts) <= 300.0:
                                return cid
                            break
                return top_v_cid

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
                if root_cid in cli_cids or root_cid in archived_cids:
                    continue
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


_format_model = core.format_model
_est_tokens_from_text = core.est_tokens_from_text
_est_tokens_from_file = core.est_tokens_from_file
_extract_title = core.extract_title
_find_git_root = core.find_git_root
_get_git_branch = core.get_git_branch


def _detect_workspace(rows: list[dict[str, Any]], fallback_ws_path: str | None = None) -> dict[str, Any]:
    return core.detect_workspace(rows, fallback_ws_path)


def _collect_memory_files(workspace_path: str | None) -> list[dict[str, Any]]:
    return core.collect_memory_files(workspace_path, config_dir=CONFIG_DIR)


def _collect_skills_catalog() -> tuple[int, list[dict[str, Any]]]:
    return core.collect_skills_catalog(builtin_skills_dir=BUILTIN_SKILLS_DIR, config_dir=CONFIG_DIR)


def _collect_outputs(cid: str) -> list[dict[str, Any]]:
    return core.collect_outputs(cid, brain_dir=BRAIN_DIR)


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
        # Refresh LRU order
        _TRANSCRIPT_CACHE.pop(cid, None)
        _TRANSCRIPT_CACHE[cid] = cached
        return cached[3]
    data = _parse_transcript(cid, tpath, ws_hint)
    _TRANSCRIPT_CACHE.pop(cid, None)
    _TRANSCRIPT_CACHE[cid] = (mtime_ns, size, hint_key, data)
    while len(_TRANSCRIPT_CACHE) > MAX_CACHE_ENTRIES:
        oldest_key = next(iter(_TRANSCRIPT_CACHE))
        _TRANSCRIPT_CACHE.pop(oldest_key, None)
    return data


def _parse_transcript(cid: str, tpath: Path, ws_hint: str | None = None) -> dict[str, Any]:
    with _SUGGEST_LOCK:
        preds_copy = dict(_RECENT_PREDICTIONS.get(cid) or {})
    return core.parse_transcript(
        cid,
        tpath,
        ws_hint=ws_hint,
        brain_dir=BRAIN_DIR,
        builtin_skills_dir=BUILTIN_SKILLS_DIR,
        config_dir=CONFIG_DIR,
        fast_cli=False,
        recent_predictions=preds_copy or None,
    )


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
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return prefs


_clean_user_text = core.clean_user_text
_detect_turn_state = core.detect_turn_state
_truncate_middle = core._truncate_middle
_extract_recent_dialogue = core.extract_recent_dialogue
SUGGEST_SYSTEM_PROMPT = core.SUGGEST_SYSTEM_PROMPT
_strip_outer_quotes = core.strip_outer_quotes
_parse_suggest_json = core.parse_suggest_json
_call_suggest_llm = core.call_suggest_llm


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
    except habits.AgyAuthRequiredError as exc:
        with _SUGGEST_LOCK:
            cur = _SUGGEST_STATE.get(cid)
            if cur and cur.get("turn_key") == turn_key and cur.get("req_seq") == req_seq:
                _SUGGEST_STATE[cid] = {
                    "turn_key": turn_key,
                    "req_seq": req_seq,
                    "status": "needs_key",
                    "primary": "",
                    "alternatives": [],
                    "model": DEFAULT_SUGGEST_MODEL,
                    "error": str(exc)[:140],
                    "latency_ms": None,
                    "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                }
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
    if os.environ.get("CONTEXT_CARD_INTERNAL_SUBPROCESS") == "1":
        return {
            "status": "disabled",
            "turn_key": "",
            "primary": "",
            "alternatives": [],
            "auto_suggest": False,
            "model": DEFAULT_SUGGEST_MODEL,
            "error": None,
            "latency_ms": None,
        }
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
        if not force and cur and cur.get("turn_key") == turn_key and cur.get("status") in ("generating", "ready", "error", "needs_key"):
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
        _SUGGEST_STATE.pop(cid, None)
        _SUGGEST_STATE[cid] = pending
        while len(_SUGGEST_STATE) > MAX_CACHE_ENTRIES:
            oldest_cid = next(iter(_SUGGEST_STATE))
            _SUGGEST_STATE.pop(oldest_cid, None)
        while len(_RECENT_PREDICTIONS) > MAX_CACHE_ENTRIES:
            oldest_pred = next(iter(_RECENT_PREDICTIONS))
            _RECENT_PREDICTIONS.pop(oldest_pred, None)

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
    explicit_raw = _sanitize_cid(conversation_id) or _sanitize_cid(_extract_meta_conversation_id(ctx)) or ""
    if explicit_raw:
        target_id = _resolve_root_conversation_id(explicit_raw) or explicit_raw
        bind_mode = bind_mode_override if bind_mode_override in ("auto", "explicit") else "explicit"
    elif (conversation_id or "").strip():
        return {
            "empty": True,
            "refreshed_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    else:
        target_id = _find_latest_conversation_id(exclude_active_panes=exclude_active_panes)
        bind_mode = bind_mode_override if bind_mode_override in ("auto", "explicit") else "auto"

    target_id = _sanitize_cid(target_id)
    if not target_id:
        return {
            "empty": True,
            "refreshed_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    _active_cids, hook_map = _get_runtime_maps()
    ws_hint = hook_map.get(target_id, ("", 0.0))[0] or _get_summary_workspace_path(target_id)
    auto_suggest = bool(_load_prefs().get("auto_suggest", True))

    tpath = BRAIN_DIR / target_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not tpath.is_file():
        # Brand new conversation whose transcript file hasn't flushed yet
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
            "native_limit_tokens": 512_000,
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
            "habits": habits.get_habits_status(),
        }

    habits.ensure_initialized_async(auto_suggest_enabled=auto_suggest)
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


_fmt_k = core.fmt_k


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
        native_lim = int(snap.get("native_limit_tokens") or snap["limit_tokens"])
        native_note = f", native {_fmt_k(native_lim)}" if native_lim > snap["limit_tokens"] else ""
        text = (
            f"Context Card ({snap['conversation_id'][:8]}) — {snap['model_display']}: "
            f"{snap['used_pct']}% used ({_fmt_k(snap['used_tokens'])} / {_fmt_k(snap['limit_tokens'])}{native_note}), "
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
    view_map, archived_cids = _get_user_view_times(limit=40)
    sidebar_items = _query_sidebar_summaries(limit=max(limit * 2, 20))
    if sidebar_items:
        ranked_sidebar: list[tuple[float, dict[str, Any]]] = []
        for item in sidebar_items:
            cid = item["conversation_id"]
            if cid in archived_cids:
                continue
            tpath = BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
            if not tpath.is_file():
                continue
            title = item["title"] or _extract_title_fast(tpath, cid)
            sort_ts = max(float(item["user_ts"]), float(item["mod_ts"]), float(view_map.get(cid, 0.0)))
            ranked_sidebar.append(
                (
                    sort_ts,
                    {
                        "conversation_id": cid,
                        "short_id": cid[:8],
                        "title": title,
                        "updated_at": item["last_modified_time"] or item["last_user_input_time"],
                    },
                )
            )
        ranked_sidebar.sort(key=lambda x: x[0], reverse=True)
        out = [row for _ts, row in ranked_sidebar[:limit]]
        if out:
            return out

    if not BRAIN_DIR.is_dir():
        return []
    cli_cids = _get_headless_cli_cids()
    items: list[tuple[int, str, Path]] = []
    seen_roots: set[str] = set()
    try:
        for entry in BRAIN_DIR.iterdir():
            if not entry.is_dir() or entry.name == "tempmediaStorage":
                continue
            root_cid = _resolve_root_conversation_id(entry.name) or entry.name
            if root_cid != entry.name or root_cid in seen_roots or root_cid in cli_cids:
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
    target_id = _sanitize_cid(conversation_id) or _sanitize_cid(_extract_meta_conversation_id(ctx)) or _find_latest_conversation_id() or ""
    assistant_tail = ""
    workspace = ""
    if target_id:
        cached = _TRANSCRIPT_CACHE.get(target_id)
        if cached:
            assistant_tail = cached[3].get("_assistant_tail") or ""
            workspace = (cached[3].get("environment") or {}).get("workspace_name") or ""
    differs_from_primary = bool(predicted_primary.strip() and sent_text.strip() != predicted_primary.strip())
    outcome = "edited" if (edited or differs_from_primary) else "accepted"
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
    title="Save Suggestion LLM Auth Config",
    description="Internal app-only tool to switch between Gemini Subscription (`agy`) and BYOK API key in plugin_data/llm_config.json (0600).",
    meta=APP_ONLY_META,
)
def app_save_llm_key(
    api_key: str = "",
    provider: str = "gemini",
    base_url: str = "",
    model: str = "",
    clear_key: bool = False,
    conversation_id: str | None = None,
    ctx: Context | None = None,
) -> types.CallToolResult:
    habits.save_llm_config(
        api_key=api_key,
        provider=provider,
        base_url=base_url,
        model=model,
        clear_key=clear_key,
    )
    with _SUGGEST_LOCK:
        _SUGGEST_STATE.clear()
    snap = _build_snapshot(conversation_id, ctx, force_suggest=not clear_key)
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
    _init_runtime_state()
    mcp.run(transport="stdio")
