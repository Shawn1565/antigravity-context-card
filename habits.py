#!/usr/bin/env python3
"""Local habit-learning & feedback store for Context Card suggested replies.

Learns the user's personal phrasing, workflow shorthand, and correction patterns
entirely locally in SQLite (habits.db), using Google AI Studio for periodic
style-profile distillation and lightweight lexical retrieval for dynamic few-shot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent


def resolve_agy_home() -> Path:
    """Locate the active Antigravity app data directory across Desktop, IDE, and CLI."""
    env_dir = (os.environ.get("ANTIGRAVITY_APP_DATA_DIR") or "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir)
    gemini_root = Path.home() / ".gemini"
    candidates = [
        gemini_root / "antigravity",
        gemini_root / "antigravity-ide",
        gemini_root / "antigravity-cli",
    ]
    best: Path | None = None
    best_mtime = -1.0
    for cand in candidates:
        brain = cand / "brain"
        if brain.is_dir():
            try:
                mt = brain.stat().st_mtime
                if mt > best_mtime:
                    best_mtime = mt
                    best = cand
            except OSError:
                continue
    if best is not None:
        return best
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


def resolve_data_dir() -> Path:
    """Resolve persistent per-user plugin data directory outside the plugin repo."""
    raw = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--data-dir="):
            raw = arg.split("=", 1)[1].strip()
            break
    if not raw or raw.startswith("${"):
        raw = (os.environ.get("CONTEXT_CARD_DATA_DIR") or "").strip()
    if not raw or raw.startswith("${"):
        raw = (os.environ.get("PLUGIN_DATA") or "").strip()
    if raw and not raw.startswith("${"):
        target = Path(raw).expanduser()
    else:
        target = resolve_agy_home() / "plugin_data" / "context-card"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


import sys  # noqa: E402

AGY_HOME = resolve_agy_home()
DATA_DIR = resolve_data_dir()
HABITS_DB_PATH = DATA_DIR / "habits.db"
LLM_CONFIG_PATH = DATA_DIR / "llm_config.json"
BRAIN_DIR = AGY_HOME / "brain"
SUMMARIES_DB = AGY_HOME / "conversation_summaries.db"

DEFAULT_GEMINI_MODEL = os.environ.get("CONTEXT_CARD_SUGGEST_MODEL", "gemini-2.5-flash")
FALLBACK_GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
SHELL_GEMINI_KEY_RE = re.compile(
    r"^\s*(?:export\s+)?(?:CONTEXT_CARD_GEMINI_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*(['\"]?)([^'\"\s#]+)\1"
)
ENV_ANY_KEY_RE = re.compile(
    r"^\s*(?:export\s+)?(?:CONTEXT_CARD_GEMINI_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY|CONTEXT_CARD_OPENAI_API_KEY|OPENAI_API_KEY|API_KEY)\s*=\s*(['\"]?)([^'\"\s#]+)\1"
)
ASCII_WORD_RE = re.compile(r"[a-zA-Z0-9_\-]{2,}")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

_DB_LOCK = threading.Lock()
_DISTILL_LOCK = threading.Lock()
_DISTILL_IN_PROGRESS = False
_LLM_CFG_CACHE: dict[str, str] | None = None
_BOOTSTRAPPED = False


def _read_key_from_env_file(path_str: str) -> str:
    if not path_str:
        return ""
    p = Path(path_str).expanduser()
    if not p.is_file():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    for line in lines:
        m = ENV_ANY_KEY_RE.match(line)
        if m and m.group(2):
            return m.group(2).strip()
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" not in s and len(s) >= 16:
            return s
    return ""


def _is_local_base_url(url: str) -> bool:
    low = (url or "").lower()
    return low.startswith("http://localhost") or low.startswith("http://127.0.0.1") or low.startswith("http://[::1]")


def resolve_llm_config() -> dict[str, str]:
    """Resolve BYOK LLM credentials from env vars, plugin_data/llm_config.json, or shell rc."""
    global _LLM_CFG_CACHE

    # 1. Explicit OpenAI-compatible env vars (OpenRouter, DeepSeek, OpenAI, local Ollama/vLLM)
    oa_base = (os.environ.get("CONTEXT_CARD_OPENAI_BASE_URL") or "").strip()
    oa_key = (os.environ.get("CONTEXT_CARD_OPENAI_API_KEY") or "").strip()
    provider_env = (os.environ.get("CONTEXT_CARD_PROVIDER") or "").strip().lower()
    model_env = (os.environ.get("CONTEXT_CARD_SUGGEST_MODEL") or "").strip()

    if oa_base or provider_env == "openai":
        base = (oa_base or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        key = oa_key or (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key and _is_local_base_url(base):
            key = "local"
        if key:
            return {
                "provider": "openai",
                "api_key": key,
                "base_url": base,
                "model": model_env or "gpt-4o-mini",
            }

    # 2. Standard Gemini env vars
    for env_name in ("CONTEXT_CARD_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return {
                "provider": "gemini",
                "api_key": val,
                "base_url": GEMINI_API_BASE,
                "model": model_env or DEFAULT_GEMINI_MODEL,
            }

    if _LLM_CFG_CACHE is not None:
        return dict(_LLM_CFG_CACHE)

    # 3. Local plugin_data/llm_config.json (set via UI or user file, mode 0600)
    try:
        if LLM_CONFIG_PATH.is_file():
            raw = json.loads(LLM_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                prov = str(raw.get("provider") or "gemini").strip().lower()
                key = str(raw.get("api_key") or "").strip()
                if not key and raw.get("api_key_env_file"):
                    key = _read_key_from_env_file(str(raw.get("api_key_env_file")))
                base = str(raw.get("base_url") or "").strip().rstrip("/")
                mdl = model_env or str(raw.get("model") or "").strip()
                if prov == "openai":
                    eff_base = base or "https://api.openai.com/v1"
                    if not key and _is_local_base_url(eff_base):
                        key = "local"
                    if key:
                        _LLM_CFG_CACHE = {
                            "provider": "openai",
                            "api_key": key,
                            "base_url": eff_base,
                            "model": mdl or "gpt-4o-mini",
                        }
                        return dict(_LLM_CFG_CACHE)
                elif key:
                    _LLM_CFG_CACHE = {
                        "provider": "gemini",
                        "api_key": key,
                        "base_url": base or GEMINI_API_BASE,
                        "model": mdl or DEFAULT_GEMINI_MODEL,
                    }
                    return dict(_LLM_CFG_CACHE)
    except Exception:
        pass

    # 4. Fallback to ~/.profile / ~/.bashrc / ~/.zshrc
    for rc_name in (".profile", ".bashrc", ".zshrc"):
        rc_path = Path.home() / rc_name
        if not rc_path.is_file():
            continue
        try:
            for line in rc_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = SHELL_GEMINI_KEY_RE.match(line)
                if m and m.group(2):
                    _LLM_CFG_CACHE = {
                        "provider": "gemini",
                        "api_key": m.group(2).strip(),
                        "base_url": GEMINI_API_BASE,
                        "model": model_env or DEFAULT_GEMINI_MODEL,
                    }
                    return dict(_LLM_CFG_CACHE)
        except OSError:
            continue

    _LLM_CFG_CACHE = {"provider": "gemini", "api_key": "", "base_url": GEMINI_API_BASE, "model": DEFAULT_GEMINI_MODEL}
    return dict(_LLM_CFG_CACHE)


def has_llm_configured() -> bool:
    return bool(resolve_llm_config().get("api_key"))


def get_gemini_api_key() -> str:
    """Backward-compatible helper returning the active API key (if any)."""
    return resolve_llm_config().get("api_key") or ""


def save_llm_config(
    api_key: str,
    provider: str = "gemini",
    base_url: str = "",
    model: str = "",
) -> bool:
    """Persist BYOK credentials locally in plugin_data/llm_config.json (0600)."""
    global _LLM_CFG_CACHE
    prov = "openai" if (provider or "").strip().lower() == "openai" else "gemini"
    clean_key = (api_key or "").strip()
    clean_base = (base_url or "").strip().rstrip("/")
    clean_model = (model or "").strip()

    if not clean_key and not (prov == "openai" and _is_local_base_url(clean_base)):
        if LLM_CONFIG_PATH.is_file():
            try:
                LLM_CONFIG_PATH.unlink()
            except OSError:
                pass
        _LLM_CFG_CACHE = None
        return False

    data: dict[str, str] = {"provider": prov, "api_key": clean_key}
    if clean_base:
        data["base_url"] = clean_base
    if clean_model:
        data["model"] = clean_model

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = LLM_CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(str(tmp_path), str(LLM_CONFIG_PATH))
    _LLM_CFG_CACHE = None
    return has_llm_configured()


def call_gemini_json(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.35,
    max_output_tokens: int = 280,
    model: str | None = None,
    timeout: float = 15.0,
) -> tuple[str, str, int]:
    """Call configured BYOK model (Gemini native or OpenAI-compatible) and return (text, model_used, latency_ms)."""
    cfg = resolve_llm_config()
    api_key = cfg.get("api_key") or ""
    if not api_key:
        raise RuntimeError("API key not configured")

    if cfg.get("provider") == "openai":
        base_url = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        target_model = (model or cfg.get("model") or "gpt-4o-mini").strip()
        url = f"{base_url}/chat/completions"
        payload_oa: dict[str, Any] = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if api_key and api_key != "local":
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload_oa).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="ignore"))
            latency_ms = int(round((time.monotonic() - t0) * 1000))
            choices = body.get("choices") or []
            if not choices:
                raise ValueError("No choices returned from OpenAI-compatible endpoint")
            text = str(((choices[0].get("message") or {}).get("content")) or "").strip()
            if not text:
                raise ValueError("Empty content returned")
            return (text, target_model, latency_ms)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err_body = json.loads(e.read().decode("utf-8", errors="ignore"))
                detail = ((err_body.get("error") or {}).get("message") if isinstance(err_body.get("error"), dict) else str(err_body.get("error") or "")) or ""
            except Exception:
                pass
            raise RuntimeError(f"LLM HTTP {e.code}: {detail[:100]}" if detail else f"LLM HTTP {e.code}") from e

    base_gemini = (cfg.get("base_url") or GEMINI_API_BASE).rstrip("/")
    target = (model or cfg.get("model") or DEFAULT_GEMINI_MODEL).strip()
    candidates = [target]
    if FALLBACK_GEMINI_MODEL not in candidates:
        candidates.append(FALLBACK_GEMINI_MODEL)

    last_err: Exception | None = None
    for m in candidates:
        url = f"{base_gemini}/{m}:generateContent"
        gen_cfg: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        }
        if m.startswith("gemini-2.5-flash") and "lite" not in m:
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}

        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": gen_cfg,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="ignore"))
            latency_ms = int(round((time.monotonic() - t0) * 1000))
            cands = body.get("candidates") or []
            if not cands:
                raise ValueError("No candidates returned from Gemini")
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(str(p.get("text") or "") for p in parts if not p.get("thought")).strip()
            if not text:
                raise ValueError("Empty text returned from Gemini")
            return (text, m, latency_ms)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and m != candidates[-1]:
                continue
            detail = ""
            try:
                err_body = json.loads(e.read().decode("utf-8", errors="ignore"))
                detail = (err_body.get("error") or {}).get("message") or ""
            except Exception:
                pass
            raise RuntimeError(f"Gemini HTTP {e.code}: {detail[:100]}" if detail else f"Gemini HTTP {e.code}") from e
        except Exception as e:
            last_err = e
            if m != candidates[-1]:
                continue
            raise

    raise last_err or RuntimeError("Gemini call failed")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(HABITS_DB_PATH), timeout=2.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            summary TEXT NOT NULL DEFAULT '',
            frequent_phrases TEXT NOT NULL DEFAULT '[]',
            samples_analyzed INTEGER NOT NULL DEFAULT 0,
            live_feedback_at_distill INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_key TEXT UNIQUE NOT NULL,
            conversation_id TEXT NOT NULL,
            workspace TEXT NOT NULL DEFAULT '',
            assistant_tail TEXT NOT NULL,
            predicted_primary TEXT NOT NULL DEFAULT '',
            actual_reply TEXT NOT NULL,
            outcome TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_examples_outcome ON examples(outcome)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_examples_created ON examples(created_at DESC)")
    conn.commit()
    return conn


def extract_keywords(text: str) -> set[str]:
    """Extract lightweight lexical tokens (ASCII words + Chinese bigrams) for retrieval."""
    tokens: set[str] = set()
    low = (text or "").lower()
    for w in ASCII_WORD_RE.findall(low):
        if len(w) <= 32:
            tokens.add(w)
    for run in CJK_RUN_RE.findall(text or ""):
        if len(run) == 1:
            tokens.add(run)
        else:
            for i in range(len(run) - 1):
                tokens.add(run[i : i + 2])
    return tokens


def _clean_user_text(raw: str) -> str:
    m = USER_REQUEST_RE.search(raw or "")
    text = m.group(1) if m else (raw or "")
    text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
    text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
    return text.strip()


def _is_learnable_reply(text: str) -> bool:
    s = (text or "").strip()
    if len(s) < 2 or len(s) > 220:
        return False
    if s.startswith("/") and len(s) < 24:
        # Allow slash commands only if meaningful; skip raw /compact noise
        if s.lower() == "/compact":
            return False
    if s.count("\n") > 4:
        return False
    if "```" in s or "<" in s[:10]:
        return False
    return True


def _extract_assistant_tail(text: str, max_len: int = 360) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    if len(s) <= max_len:
        return s
    return "…" + s[-(max_len - 1) :]


def record_example(
    *,
    turn_key: str,
    conversation_id: str,
    workspace: str,
    assistant_tail: str,
    predicted_primary: str,
    actual_reply: str,
    outcome: str,
    created_at: str | None = None,
) -> bool:
    """Persist a single feedback or history example. Returns True if newly inserted/updated."""
    actual = (actual_reply or "").strip()
    if not _is_learnable_reply(actual) or not turn_key:
        return False

    tail = _extract_assistant_tail(assistant_tail, 360)
    pred = (predicted_primary or "").strip()
    kw = " ".join(sorted(extract_keywords(tail + " " + actual + " " + workspace))[:80])
    ts = created_at or datetime.now(tz=timezone.utc).isoformat()

    # Priority order when same turn_key already exists: edited > accepted > overridden > history
    rank = {"history": 0, "overridden": 1, "accepted": 2, "edited": 3}

    with _DB_LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT outcome FROM examples WHERE turn_key = ?", (turn_key,)
            ).fetchone()
            if row:
                old_rank = rank.get(row[0], 0)
                new_rank = rank.get(outcome, 0)
                if new_rank <= old_rank:
                    return False
                conn.execute(
                    """
                    UPDATE examples
                    SET predicted_primary = ?, actual_reply = ?, outcome = ?, keywords = ?, created_at = ?
                    WHERE turn_key = ?
                    """,
                    (pred, actual, outcome, kw, ts, turn_key),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO examples (
                        turn_key, conversation_id, workspace, assistant_tail,
                        predicted_primary, actual_reply, outcome, keywords, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (turn_key, conversation_id, workspace or "", tail, pred, actual, outcome, kw, ts),
                )
            conn.commit()
        finally:
            conn.close()

    if outcome in ("accepted", "edited", "overridden"):
        maybe_schedule_distill(force=False)
    return True


def _iter_interactive_transcripts(limit: int = 24) -> list[tuple[str, Path]]:
    """Return recent interactive user transcripts (excluding headless/subagent runs)."""
    cids: list[str] = []
    if SUMMARIES_DB.is_file():
        try:
            conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=0.5)
            try:
                rows = conn.execute(
                    """
                    SELECT conversation_id FROM conversation_summaries
                    WHERE title != '' AND (parent_conversation_id IS NULL OR parent_conversation_id = '')
                    ORDER BY last_user_input_time DESC, last_modified_time DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                cids = [str(r[0]) for r in rows if r and r[0]]
            finally:
                conn.close()
        except Exception:
            cids = []

    out: list[tuple[str, Path]] = []
    for cid in cids:
        tp = BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if tp.is_file():
            out.append((cid, tp))
    return out


def bootstrap_from_history(max_conversations: int = 20, max_examples: int = 120) -> int:
    """Scan recent interactive transcripts and populate initial real user reply pairs."""
    added = 0
    for cid, tpath in _iter_interactive_transcripts(limit=max_conversations):
        if added >= max_examples:
            break
        try:
            lines = tpath.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue

        last_assistant: tuple[int, str] | None = None
        for idx, line in enumerate(lines):
            s = line.strip()
            if not s:
                continue
            try:
                r = json.loads(s)
            except json.JSONDecodeError:
                continue
            rtype = r.get("type")
            if (
                rtype == "PLANNER_RESPONSE"
                and r.get("status") == "DONE"
                and (r.get("content") or "").strip()
                and not (r.get("tool_calls") or [])
            ):
                last_assistant = (int(r.get("step_index", idx)), (r.get("content") or "").strip())
            elif rtype == "USER_INPUT" and last_assistant is not None:
                reply = _clean_user_text(r.get("content") or "")
                step_idx, asst_text = last_assistant
                last_assistant = None
                if not _is_learnable_reply(reply):
                    continue
                digest = hashlib.sha1(asst_text.encode("utf-8", errors="ignore")).hexdigest()[:10]
                tkey = f"{cid}:{step_idx}:{digest}"
                if record_example(
                    turn_key=tkey,
                    conversation_id=cid,
                    workspace="",
                    assistant_tail=asst_text,
                    predicted_primary="",
                    actual_reply=reply,
                    outcome="history",
                    created_at=r.get("created_at") or datetime.now(tz=timezone.utc).isoformat(),
                ):
                    added += 1
                    if added >= max_examples:
                        break
    return added


def capture_implicit_feedback(
    cid: str,
    rows: list[dict[str, Any]],
    workspace: str,
    recent_predictions: dict[str, str],
) -> None:
    """Detect when the user replied directly in chat after a predicted turn and record it."""
    if not rows or not recent_predictions:
        return

    for i in range(len(rows) - 1):
        r = rows[i]
        if (
            r.get("type") == "PLANNER_RESPONSE"
            and r.get("status") == "DONE"
            and (r.get("content") or "").strip()
            and not (r.get("tool_calls") or [])
        ):
            content = (r.get("content") or "").strip()
            step_idx = r.get("step_index", i)
            digest = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()[:10]
            tkey = f"{cid}:{step_idx}:{digest}"
            pred = recent_predictions.get(tkey)
            if not pred:
                continue
            # Look ahead for the next USER_INPUT
            for j in range(i + 1, min(len(rows), i + 5)):
                nxt = rows[j]
                if nxt.get("type") == "USER_INPUT":
                    actual = _clean_user_text(nxt.get("content") or "")
                    if actual:
                        outcome = "accepted" if actual.strip() == pred.strip() else "overridden"
                        record_example(
                            turn_key=tkey,
                            conversation_id=cid,
                            workspace=workspace,
                            assistant_tail=content,
                            predicted_primary=pred,
                            actual_reply=actual,
                            outcome=outcome,
                            created_at=nxt.get("created_at"),
                        )
                    break


DISTILL_SYSTEM_PROMPT = """你是一个用户沟通风格分析师。根据用户过去与 AI 助手对话的真实回复样本（尤其是用户「修改建议」和「手打覆盖建议」的纠偏样本），提炼一份紧凑、可执行的个人风格画像，用于让下一轮“建议回复生成器”说出更像用户本人的话。

仅输出合法 JSON：
{
  "summary": "150~260字中文画像，分3点概括：1) 语言与长度习惯 2) 决策与工作流推进风格 3) 偏好与忌讳",
  "frequent_phrases": ["高频习惯用语或工作流短语，最多8个，仅摘自样本中用户实际说过的词"]
}

注意：summary 里只描述样本中观察到的用户习惯，不要复述本提示词的规则。
"""


def _distill_profile_sync() -> dict[str, Any]:
    global _DISTILL_IN_PROGRESS
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                live_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM examples WHERE outcome IN ('accepted', 'edited', 'overridden')"
                    ).fetchone()[0]
                )
                total_count = int(conn.execute("SELECT COUNT(*) FROM examples").fetchone()[0])
                corrections = conn.execute(
                    """
                    SELECT assistant_tail, predicted_primary, actual_reply, outcome
                    FROM examples
                    WHERE outcome IN ('edited', 'overridden')
                    ORDER BY created_at DESC LIMIT 20
                    """
                ).fetchall()
                accepted = conn.execute(
                    """
                    SELECT assistant_tail, actual_reply
                    FROM examples
                    WHERE outcome = 'accepted'
                    ORDER BY created_at DESC LIMIT 12
                    """
                ).fetchall()
                history = conn.execute(
                    """
                    SELECT assistant_tail, actual_reply
                    FROM examples
                    WHERE outcome = 'history'
                    ORDER BY created_at DESC LIMIT 45
                    """
                ).fetchall()
            finally:
                conn.close()

        if total_count == 0:
            return get_habits_status()

        lines: list[str] = []
        if corrections:
            lines.append("## 纠偏样本（高权重：原预测 vs 用户实际说的话）")
            for tail, pred, actual, outc in corrections:
                tag = "用户改写" if outc == "edited" else "用户手打覆盖"
                lines.append(f"- [{tag}] 助手刚说: {tail[-100:]} | 原预测: {pred} -> 用户实际说: {actual}")
        if accepted:
            lines.append("\n## 直接采纳的建议（说明风格命中）")
            for tail, actual in accepted:
                lines.append(f"- 助手刚说: {tail[-90:]} -> 采纳: {actual}")
        if history:
            lines.append("\n## 历史对话中用户的真实回复")
            for tail, actual in history:
                lines.append(f"- 助手刚说: {tail[-90:]} -> 用户回复: {actual}")

        raw_text, _model, _lat = call_gemini_json(
            DISTILL_SYSTEM_PROMPT,
            "\n".join(lines),
            temperature=0.25,
            max_output_tokens=600,
            timeout=18.0,
        )
        decoder = json.JSONDecoder()
        idx = raw_text.find("{")
        parsed = decoder.raw_decode(raw_text, idx)[0] if idx != -1 else {}
        summary = str(parsed.get("summary") or "").strip()
        raw_phrases = parsed.get("frequent_phrases") or []
        phrases = [str(p).strip() for p in raw_phrases if str(p).strip()][:8]

        if summary:
            now_iso = datetime.now(tz=timezone.utc).isoformat()
            with _DB_LOCK:
                conn = _connect()
                try:
                    conn.execute(
                        """
                        INSERT INTO profile (id, summary, frequent_phrases, samples_analyzed, live_feedback_at_distill, updated_at)
                        VALUES (1, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            summary = excluded.summary,
                            frequent_phrases = excluded.frequent_phrases,
                            samples_analyzed = excluded.samples_analyzed,
                            live_feedback_at_distill = excluded.live_feedback_at_distill,
                            updated_at = excluded.updated_at
                        """,
                        (summary, json.dumps(phrases, ensure_ascii=False), total_count, live_count, now_iso),
                    )
                    conn.commit()
                finally:
                    conn.close()
    except Exception:
        pass
    finally:
        with _DISTILL_LOCK:
            _DISTILL_IN_PROGRESS = False
    return get_habits_status()


def maybe_schedule_distill(force: bool = False) -> bool:
    """Trigger background profile distillation if forced, missing, or >=6 new live feedbacks."""
    global _DISTILL_IN_PROGRESS
    if not get_gemini_api_key():
        return False

    should_run = force
    if not should_run:
        with _DB_LOCK:
            conn = _connect()
            try:
                total = int(conn.execute("SELECT COUNT(*) FROM examples").fetchone()[0])
                live = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM examples WHERE outcome IN ('accepted', 'edited', 'overridden')"
                    ).fetchone()[0]
                )
                row = conn.execute(
                    "SELECT summary, live_feedback_at_distill FROM profile WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()

        if total >= 5 and (not row or not (row[0] or "").strip()):
            should_run = True
        elif row and (live - int(row[1] or 0)) >= 6:
            should_run = True

    if not should_run:
        return False

    with _DISTILL_LOCK:
        if _DISTILL_IN_PROGRESS:
            return False
        _DISTILL_IN_PROGRESS = True

    threading.Thread(target=_distill_profile_sync, daemon=True).start()
    return True


def ensure_initialized_async() -> None:
    """Once per process, bootstrap historical user replies and schedule initial style distillation."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True

    def _init_worker() -> None:
        try:
            with _DB_LOCK:
                conn = _connect()
                try:
                    cnt = int(conn.execute("SELECT COUNT(*) FROM examples").fetchone()[0])
                finally:
                    conn.close()
            if cnt < 10:
                bootstrap_from_history(max_conversations=20, max_examples=120)
            maybe_schedule_distill(force=False)
        except Exception:
            pass

    threading.Thread(target=_init_worker, daemon=True).start()


def relearn_now_async() -> dict[str, Any]:
    """Scan latest transcripts and force-distill the style profile in the background."""
    def _worker() -> None:
        try:
            bootstrap_from_history(max_conversations=25, max_examples=150)
        except Exception:
            pass
        maybe_schedule_distill(force=True)

    threading.Thread(target=_worker, daemon=True).start()
    return get_habits_status()


def reset_habits() -> dict[str, Any]:
    """Clear all learned examples and profile."""
    with _DB_LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM examples")
            conn.execute("DELETE FROM profile")
            conn.commit()
        finally:
            conn.close()
    return get_habits_status()


def build_personalization_block(assistant_tail: str, workspace: str = "") -> str:
    """Return a compact personalization prompt block (style profile + dynamic few-shot examples)."""
    query_kw = extract_keywords(assistant_tail + " " + workspace)
    with _DB_LOCK:
        conn = _connect()
        try:
            prof = conn.execute(
                "SELECT summary, frequent_phrases FROM profile WHERE id = 1"
            ).fetchone()
            rows = conn.execute(
                """
                SELECT workspace, assistant_tail, predicted_primary, actual_reply, outcome, keywords
                FROM examples
                ORDER BY created_at DESC
                LIMIT 160
                """
            ).fetchall()
        finally:
            conn.close()

    sections: list[str] = []

    if prof and (prof[0] or "").strip():
        summary = str(prof[0]).strip()
        try:
            phrases = json.loads(prof[1] or "[]")
        except Exception:
            phrases = []
        block = f"## Learned User Style Profile\n{summary}"
        if phrases:
            block += f"\nUser's frequent phrases/shorthand: {', '.join(phrases[:8])}"
        sections.append(block)

    if rows:
        outcome_bonus = {"edited": 2.6, "overridden": 2.0, "accepted": 1.2, "history": 0.4}
        scored: list[tuple[float, tuple[Any, ...]]] = []
        for idx, r in enumerate(rows):
            r_ws, r_tail, r_pred, r_actual, r_out, r_kw_str = r
            r_kw = set((r_kw_str or "").split())
            overlap = len(query_kw & r_kw) if query_kw and r_kw else 0
            ws_bonus = 1.8 if (workspace and r_ws and workspace == r_ws) else 0.0
            recency = max(0.0, 1.0 - (idx / 160.0)) * 0.6
            score = overlap * 1.4 + outcome_bonus.get(r_out, 0.4) + ws_bonus + recency
            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        chosen: list[tuple[Any, ...]] = []
        seen_actual: set[str] = set()
        for _sc, r in scored:
            act = str(r[3]).strip()
            if act.lower() in seen_actual:
                continue
            seen_actual.add(act.lower())
            chosen.append(r)
            if len(chosen) >= 5:
                break

        if chosen:
            ex_lines = ["## Similar Past Replies by This User (mimic this exact brevity & tone)"]
            for _ws, r_tail, r_pred, r_actual, r_out, _kw in chosen:
                short_tail = r_tail[-95:] if len(r_tail) > 95 else r_tail
                if r_out in ("edited", "overridden") and r_pred:
                    ex_lines.append(
                        f"- Context: {short_tail} | (Avoid: {r_pred}) -> User actually said: {r_actual}"
                    )
                else:
                    ex_lines.append(f"- Context: {short_tail} -> User said: {r_actual}")
            sections.append("\n".join(ex_lines))

    return "\n\n".join(sections)


def get_habits_status() -> dict[str, Any]:
    """Return compact habit-learning summary for the Context Card UI."""
    with _DB_LOCK:
        conn = _connect()
        try:
            prof = conn.execute(
                "SELECT summary, frequent_phrases, samples_analyzed, updated_at FROM profile WHERE id = 1"
            ).fetchone()
            counts = dict(
                conn.execute("SELECT outcome, COUNT(*) FROM examples GROUP BY outcome").fetchall()
            )
            recent_fb = conn.execute(
                """
                SELECT outcome, predicted_primary, actual_reply, created_at
                FROM examples
                WHERE outcome IN ('accepted', 'edited', 'overridden')
                ORDER BY created_at DESC
                LIMIT 4
                """
            ).fetchall()
        finally:
            conn.close()

    total = sum(int(v) for v in counts.values())
    accepted = int(counts.get("accepted", 0))
    edited = int(counts.get("edited", 0))
    overridden = int(counts.get("overridden", 0))
    live_total = accepted + edited + overridden
    accept_rate = round(((accepted + edited) / live_total) * 100) if live_total > 0 else None

    phrases: list[str] = []
    if prof and prof[1]:
        try:
            phrases = json.loads(prof[1])
        except Exception:
            phrases = []

    return {
        "total_samples": total,
        "history_samples": int(counts.get("history", 0)),
        "accepted_count": accepted,
        "edited_count": edited,
        "overridden_count": overridden,
        "accept_rate_pct": accept_rate,
        "distilling": _DISTILL_IN_PROGRESS,
        "profile_summary": (prof[0] if prof else "") or "",
        "frequent_phrases": phrases[:8],
        "profile_updated_at": (prof[3] if prof else "") or "",
        "recent_feedback": [
            {
                "outcome": str(o),
                "predicted": str(p or ""),
                "actual": str(a or ""),
                "created_at": str(ts or ""),
            }
            for o, p, a, ts in recent_fb
        ],
    }
