#!/usr/bin/env python3
"""Opt-in CLI & tmux setup utility for Antigravity Context Card.

Commands:
  install   Configure `~/.gemini/antigravity-cli/settings.json` statusLine (Plan A)
            and optional `~/.tmux.conf` keybindings (Plan B) with automatic backup
            and restoration of any prior custom statusLine on uninstall.
  uninstall Cleanly remove Context Card statusLine (restoring any saved previous
            statusLine) and unbind/remove tmux bindings.
  status    Show current CLI statusLine, tmux binding, and suggestion auth status.
  auth      Configure or clear Suggested Reply LLM auth (shared across CLI, Desktop,
            and IDE unless explicitly disabled per surface).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import core  # noqa: E402
import habits  # noqa: E402

CLI_SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
TMUX_CONF_PATH = Path.home() / ".tmux.conf"
TMUX_BEGIN = "# >>> antigravity-context-card >>>"
TMUX_END = "# <<< antigravity-context-card <<<"
TMUX_BLOCK_RE = re.compile(
    rf"\n?{re.escape(TMUX_BEGIN)}.*?{re.escape(TMUX_END)}\n?",
    re.DOTALL,
)
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_+^@-]{1,16}$")
SAFE_UNQUOTED_TILDE_RE = re.compile(r"^~/[A-Za-z0-9._/-]+$")
PREFIX_Y_RE = re.compile(r"^\s*bind(?:-key)?\s+(?:-[A-Za-z]+\s+)*y\b", re.MULTILINE)
MAX_BACKUPS = 3


def _prev_statusline_path(data_dir: Path) -> Path:
    return data_dir / "previous_statusline.json"


def _cli_config_path(data_dir: Path) -> Path:
    return data_dir / "cli_config.json"


def _load_cli_config(data_dir: Path) -> dict[str, Any]:
    p = _cli_config_path(data_dir)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _shell_script_token(name: str, *, allow_tilde: bool = True) -> str:
    """Return a shell-safe script path token (uses ~/... when clean, else shlex.quote(abs))."""
    abs_path = (PLUGIN_ROOT / name).resolve()
    if allow_tilde:
        home = Path.home().resolve()
        try:
            rel = abs_path.relative_to(home)
            cand = f"~/{rel.as_posix()}"
            if SAFE_UNQUOTED_TILDE_RE.match(cand):
                return cand
        except ValueError:
            pass
    return shlex.quote(str(abs_path))


def _tmux_dq(s: str, *, escape_hash: bool = True) -> str:
    """Escape a string for inclusion inside tmux double quotes (optionally escaping `#`)."""
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    if escape_hash:
        out = out.replace("#", "##")
    return out


def _load_settings() -> dict[str, Any]:
    if not CLI_SETTINGS_PATH.is_file():
        return {}
    try:
        raw = json.loads(CLI_SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {CLI_SETTINGS_PATH} (line {exc.lineno}, col {exc.colno}). "
            "Please fix syntax/comments in settings.json before running install/uninstall."
        ) from exc
    except OSError as exc:
        raise ValueError(f"Unable to read {CLI_SETTINGS_PATH}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{CLI_SETTINGS_PATH} must contain a JSON object.")
    return raw


def _prune_backups(path: Path, keep: int = MAX_BACKUPS) -> None:
    try:
        baks = sorted(path.parent.glob(f"{path.name}.bak.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in baks[keep:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _backup_file(path: Path) -> Path | None:
    if not path.is_file():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak.{ts}")
    idx = 1
    while bak.exists():
        bak = path.with_name(f"{path.name}.bak.{ts}-{idx}")
        idx += 1
    shutil.copy2(str(path), str(bak))
    _prune_backups(path, keep=MAX_BACKUPS)
    return bak


def _write_text_atomic_preserve_mode(path: Path, content: str, *, default_mode: int) -> None:
    """Atomically write `content` while preserving existing mode and following symlinks (dotfiles-safe)."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = default_mode
    try:
        if target.is_file():
            mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        pass
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(str(tmp), mode)
        os.replace(str(tmp), str(target))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_settings(data: dict[str, Any]) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _write_text_atomic_preserve_mode(CLI_SETTINGS_PATH, text, default_mode=0o600)


def _has_prefix_y_conflict(cleaned_tmux_conf: str) -> bool:
    if PREFIX_Y_RE.search(cleaned_tmux_conf):
        return True
    if os.environ.get("TMUX") and shutil.which("tmux"):
        try:
            res = subprocess.run(
                ["tmux", "list-keys", "-T", "prefix", "y"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                if "tmux_fill.py" not in res.stdout:
                    return True
        except Exception:
            pass
    return False


def _unbind_tmux_keys_if_active(old_cfg: dict[str, Any]) -> None:
    """Unbind previously installed keys in a live tmux server so uninstall/rekey leaves no stale bindings."""
    if not os.environ.get("TMUX") or not shutil.which("tmux"):
        return
    old_accept = str(old_cfg.get("accept_key") or "").strip()
    old_alt = str(old_cfg.get("alt_key") or "").strip()
    for k in (old_accept, old_alt):
        if k and SAFE_KEY_RE.match(k):
            subprocess.run(["tmux", "unbind-key", "-n", k], capture_output=True, timeout=2.0, check=False)
    if old_cfg.get("prefix_y_installed"):
        subprocess.run(["tmux", "unbind-key", "y"], capture_output=True, timeout=2.0, check=False)


def _build_tmux_block(accept_key: str, alt_key: str, *, include_prefix_y: bool = True) -> str:
    # Escape script path safely (including `#` -> `##`), then append `--pane #{pane_id}`
    # so tmux expands `#{pane_id}` synchronously at keypress time.
    # Non-`agy` panes route purely inside tmux via `if-shell -F` (zero Python startup).
    fill_tok = _shell_script_token("tmux_fill.py", allow_tilde=True)
    accept_sh_escaped = _tmux_dq(f"python3 {fill_tok} accept --pane ", escape_hash=True) + "#{pane_id}"
    cycle_sh_escaped = _tmux_dq(f"python3 {fill_tok} cycle --pane ", escape_hash=True) + "#{pane_id}"

    accept_run = f'run-shell -b "{accept_sh_escaped}"'
    cycle_run = f'run-shell -b "{cycle_sh_escaped}"'

    # Second-layer double-quote escaping for the `if-shell` command argument (preserve `#{pane_id}`)
    accept_if_arg = _tmux_dq(accept_run, escape_hash=False)
    cycle_if_arg = _tmux_dq(cycle_run, escape_hash=False)

    lines = [
        TMUX_BEGIN,
        "# Antigravity Context Card: fill suggested reply into agy prompt box (no Enter)",
        (
            f'bind-key -n {accept_key} if-shell -F "#{{==:#{{pane_current_command}},agy}}" '
            f'"{accept_if_arg}" "send-keys {accept_key}"'
        ),
        (
            f'bind-key -n {alt_key} if-shell -F "#{{==:#{{pane_current_command}},agy}}" '
            f'"{cycle_if_arg}" "send-keys {alt_key}"'
        ),
    ]
    if include_prefix_y:
        lines.append(f'bind-key y {accept_run}')
    lines.append(TMUX_END)
    return "\n".join(lines) + "\n"


def _reload_tmux_if_active() -> bool:
    if not os.environ.get("TMUX") or not shutil.which("tmux"):
        return False
    res = subprocess.run(
        ["tmux", "source-file", str(TMUX_CONF_PATH)],
        capture_output=True,
        text=True,
        timeout=3.0,
        check=False,
    )
    return res.returncode == 0


def _clear_cli_suggestion_caches(data_dir: Path) -> None:
    for pattern in ("cli_cache/suggest_*.json", "cli_cache/suggest_*.lock", "panes/*.json"):
        for p in data_dir.glob(pattern):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def cmd_install(args: argparse.Namespace) -> int:
    if not SAFE_KEY_RE.match(args.key) or not SAFE_KEY_RE.match(args.alt_key):
        print("Error: invalid tmux key syntax.", file=sys.stderr)
        return 1

    try:
        settings = _load_settings()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    data_dir = habits.use_cli_context()
    old_cli_cfg = _load_cli_config(data_dir)
    bak = _backup_file(CLI_SETTINGS_PATH)

    # Preserve any existing non-Context-Card statusLine so `uninstall` can restore it
    existing_sl = settings.get("statusLine")
    prev_path = _prev_statusline_path(data_dir)
    if existing_sl is not None and not core.is_our_statusline_config(existing_sl, PLUGIN_ROOT, data_dir):
        core.write_json_atomic(prev_path, {"statusLine": existing_sl}, mode=0o600)
        print(f"[INFO] Saved existing statusLine config to {prev_path} (will restore on uninstall).")

    script_tok = _shell_script_token("cli_statusline.py", allow_tilde=True)
    cmd_parts = ["python3", script_tok]
    if args.replace:
        cmd_parts.append("--replace")
    if args.no_suggest:
        cmd_parts.append("--no-suggest")
    command_str = " ".join(cmd_parts)

    settings["statusLine"] = {
        "type": "command",
        "command": command_str,
        "padding": 0,
        "enabled": True,
        "stack_with_default": not bool(args.replace),
    }
    _write_settings(settings)

    mode_desc = "replace built-in statusLine" if args.replace else "stack below built-in statusLine (default)"
    print(f"[OK] Configured CLI statusLine ({mode_desc})")
    print(f"     File: {CLI_SETTINGS_PATH}")
    if bak:
        print(f"     Backup: {bak}")

    include_prefix_y = False
    tmux_bin = shutil.which("tmux")
    if not args.no_tmux and tmux_bin:
        existing_conf = TMUX_CONF_PATH.read_text(encoding="utf-8", errors="surrogateescape") if TMUX_CONF_PATH.is_file() else ""
        cleaned = TMUX_BLOCK_RE.sub("\n", existing_conf).rstrip()
        include_prefix_y = not bool(args.no_prefix_key)
        prefix_skipped_conflict = False
        if include_prefix_y and not args.force_prefix_y and _has_prefix_y_conflict(cleaned):
            include_prefix_y = False
            prefix_skipped_conflict = True

        # Unbind any previously installed Context Card keys before reloading (handles rekeying)
        if old_cli_cfg:
            _unbind_tmux_keys_if_active(old_cli_cfg)

        block = _build_tmux_block(args.key, args.alt_key, include_prefix_y=include_prefix_y)
        new_conf = (cleaned + "\n\n" + block) if cleaned else block
        if TMUX_CONF_PATH.is_file():
            _backup_file(TMUX_CONF_PATH)
        _write_text_atomic_preserve_mode(TMUX_CONF_PATH, new_conf, default_mode=0o644)
        reloaded = _reload_tmux_if_active()
        print(f"[OK] Configured tmux keybindings in {TMUX_CONF_PATH}")
        accept_lbl = core.format_tmux_key_label(args.key)
        alt_lbl = core.format_tmux_key_label(args.alt_key)
        prefix_note = " or Prefix+y" if include_prefix_y else ""
        print(f"     Accept suggestion: {accept_lbl} (native tmux agy-only routing){prefix_note}")
        print(f"     Cycle alternative: {alt_lbl} (native tmux agy-only routing)")
        if prefix_skipped_conflict:
            print("     [NOTE] Skipped `Prefix+y` binding because `y` is already bound in your tmux prefix table (use --force-prefix-y to override).")
        if reloaded:
            print("     Reloaded active tmux config automatically.")
        else:
            print("     Run `tmux source-file ~/.tmux.conf` inside tmux to apply immediately.")
    elif args.no_tmux:
        print("[SKIP] Skipped tmux keybinding setup (--no-tmux).")
    else:
        print("[INFO] tmux not found on PATH; installed Plan A (native CLI statusLine) only.")

    # Persist configured key labels & exact statusLine command (for custom clone dirs & clean uninstall)
    core.write_json_atomic(
        _cli_config_path(data_dir),
        {
            "accept_key": args.key,
            "alt_key": args.alt_key,
            "prefix_y_installed": include_prefix_y,
            "statusline_command": command_str,
            "replace": bool(args.replace),
            "no_suggest": bool(args.no_suggest),
        },
        mode=0o600,
    )

    cfg = habits.resolve_llm_config()
    if cfg.get("api_key"):
        src = habits.get_active_llm_config_path()
        src_note = f" from {src}" if src else " from environment"
        print(f"[OK] Suggested reply auth ready: provider={cfg.get('provider')}, model={cfg.get('model')}{src_note}")
    else:
        setup_tok = _shell_script_token("cli_setup.py", allow_tilde=True)
        print("[INFO] Suggested reply is currently offline (strict opt-in).")
        print(f"       Enable zero-key Gemini subscription: python3 {setup_tok} auth agy")
        print(f"       Or enter a Gemini API key securely:  python3 {setup_tok} auth gemini")
    return 0


def cmd_uninstall(_args: argparse.Namespace) -> int:
    data_dir = habits.use_cli_context()
    prev_path = _prev_statusline_path(data_dir)
    cfg_path = _cli_config_path(data_dir)
    old_cli_cfg = _load_cli_config(data_dir)

    if CLI_SETTINGS_PATH.is_file():
        try:
            settings = _load_settings()
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if core.is_our_statusline_config(settings.get("statusLine"), PLUGIN_ROOT, data_dir):
            _backup_file(CLI_SETTINGS_PATH)
            restored = False
            if prev_path.is_file():
                try:
                    prev_obj = json.loads(prev_path.read_text(encoding="utf-8"))
                    if isinstance(prev_obj, dict) and "statusLine" in prev_obj:
                        settings["statusLine"] = prev_obj["statusLine"]
                        restored = True
                except Exception:
                    pass
                prev_path.unlink(missing_ok=True)
            if not restored:
                settings.pop("statusLine", None)
            _write_settings(settings)
            if restored:
                print(f"[OK] Removed Context Card and restored previous statusLine in {CLI_SETTINGS_PATH}")
            else:
                print(f"[OK] Removed Context Card statusLine from {CLI_SETTINGS_PATH}")
        else:
            print(f"[SKIP] {CLI_SETTINGS_PATH} statusLine is not managed by Context Card.")
    else:
        print(f"[SKIP] {CLI_SETTINGS_PATH} does not exist.")

    cfg_path.unlink(missing_ok=True)

    if TMUX_CONF_PATH.is_file():
        existing = TMUX_CONF_PATH.read_text(encoding="utf-8", errors="surrogateescape")
        if TMUX_BEGIN in existing:
            _backup_file(TMUX_CONF_PATH)
            updated = TMUX_BLOCK_RE.sub("\n", existing).strip()
            _write_text_atomic_preserve_mode(
                TMUX_CONF_PATH,
                (updated + "\n") if updated else "",
                default_mode=0o644,
            )
            _unbind_tmux_keys_if_active(old_cli_cfg)
            reloaded = _reload_tmux_if_active()
            print(f"[OK] Removed Context Card bindings from {TMUX_CONF_PATH}")
            if reloaded:
                print("     Unbound keys and reloaded active tmux config.")
        else:
            print(f"[SKIP] No Context Card block found in {TMUX_CONF_PATH}.")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    data_dir = habits.use_cli_context()
    sl_state = "not configured"
    if CLI_SETTINGS_PATH.is_file():
        try:
            sl = _load_settings().get("statusLine")
            if core.is_our_statusline_config(sl, PLUGIN_ROOT, data_dir):
                mode = "stack_with_default" if sl.get("stack_with_default") else "replace"
                sl_state = f"installed ({mode}, enabled={sl.get('enabled', True)})"
            elif sl:
                sl_state = "custom (other script)"
        except ValueError as exc:
            sl_state = f"error ({exc})"

    tmux_state = "not configured"
    if TMUX_CONF_PATH.is_file() and TMUX_BEGIN in TMUX_CONF_PATH.read_text(encoding="utf-8", errors="ignore"):
        accept_lbl, alt_lbl = core.load_cli_key_labels(data_dir)
        tmux_state = f"installed in ~/.tmux.conf ({accept_lbl} fill / {alt_lbl} alt)"

    cfg = habits.resolve_llm_config()
    cfg_src = habits.get_active_llm_config_path()
    if cfg.get("api_key"):
        src_desc = str(cfg_src) if cfg_src else "environment variable"
        auth_state = f"{cfg.get('provider')} ({cfg.get('model')}) [{src_desc}]"
    elif cfg.get("provider") == "off":
        auth_state = f"disabled on CLI ({cfg_src})"
    else:
        auth_state = "not configured (offline monitor only)"

    print("Antigravity Context Card — CLI Status")
    print(f"  CLI statusLine : {sl_state}")
    print(f"  tmux bindings  : {tmux_state}")
    print(f"  Suggest auth   : {auth_state}")
    print(f"  CLI data dir   : {data_dir}")
    if _prev_statusline_path(data_dir).is_file():
        print("  Prior bar saved: yes (will restore on uninstall)")
    return 0


def _read_secret_value(raw_val: str, prompt_label: str) -> str:
    val = (raw_val or "").strip()
    if val == "-":
        return sys.stdin.read().strip()
    if val:
        return val
    if sys.stdin.isatty():
        return getpass.getpass(f"{prompt_label}: ").strip()
    return sys.stdin.read().strip()


def cmd_auth(args: argparse.Namespace) -> int:
    data_dir = habits.use_cli_context()
    sub = args.provider

    if sub == "status":
        cfg = habits.resolve_llm_config()
        src = habits.get_active_llm_config_path()
        if cfg.get("api_key"):
            src_desc = str(src) if src else "environment variable"
            print(f"Active suggestion auth: provider={cfg.get('provider')}, model={cfg.get('model')} ({src_desc})")
        elif cfg.get("provider") == "off":
            print(f"Suggestion auth: explicitly disabled on CLI ({src}).")
        else:
            print("Suggestion auth: not configured (offline monitoring only).")
        return 0

    if sub == "clear":
        still_active = habits.save_llm_config(clear_key=True, clear_all=bool(args.all))
        _clear_cli_suggestion_caches(data_dir)
        if args.all:
            print("[OK] Cleared saved auth files across all Antigravity surfaces and purged CLI suggestion caches.")
        else:
            print(f"[OK] Disabled/cleared CLI suggestion auth ({habits.LLM_CONFIG_PATH}) and purged CLI caches.")
            if habits.has_sibling_llm_config():
                print("[INFO] Wrote local `provider=off` tombstone so CLI stays offline while leaving Desktop/IDE config intact.")
                print("       Pass `--all` to clear Desktop/IDE config files as well.")
        if still_active:
            print("[NOTE] Auth is still provided by process environment variables (e.g. GEMINI_API_KEY).")
        return 0

    if sub == "agy":
        if not habits.find_agy_bin():
            print("Error: `agy` binary not found on PATH.", file=sys.stderr)
            return 1
        ok = habits.save_llm_config(provider="agy", model=args.model or "")
        print(f"[OK] Enabled Gemini Subscription via `agy` CLI (active={ok}).")
        print("     Note: Antigravity Desktop/IDE will also share this auth (including habit learning) unless cleared there.")
        return 0

    if sub == "gemini":
        key = _read_secret_value(args.value, "Gemini API Key")
        if not key:
            print("Error: empty Gemini API key. Run `auth gemini` (interactive) or pipe via `auth gemini -`.", file=sys.stderr)
            return 1
        ok = habits.save_llm_config(provider="gemini", api_key=key, model=args.model or "")
        print(f"[OK] Saved Gemini API key to {habits.LLM_CONFIG_PATH} (0600, active={ok}).")
        print("     Note: Antigravity Desktop/IDE will also share this auth (including habit learning) unless cleared there.")
        return 0

    if sub == "openai":
        if not args.value:
            print("Error: provide base URL: auth openai <BASE_URL> [--api-key -] [--model <MODEL>]", file=sys.stderr)
            return 1
        api_key = _read_secret_value(args.api_key, "OpenAI-compatible API Key") if args.api_key == "-" else (args.api_key or "")
        ok = habits.save_llm_config(
            provider="openai",
            base_url=args.value,
            api_key=api_key,
            model=args.model or "",
        )
        print(f"[OK] Saved OpenAI-compatible config to {habits.LLM_CONFIG_PATH} (0600, active={ok}).")
        print("     Note: Antigravity Desktop/IDE will also share this auth (including habit learning) unless cleared there.")
        return 0

    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cli_setup.py",
        description="Configure Antigravity Context Card for Antigravity CLI (Plan A) and tmux (Plan B).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inst = sub.add_parser("install", help="Install CLI statusLine and optional tmux keybindings")
    p_inst.add_argument("--replace", action="store_true", help="Replace built-in statusLine instead of stacking below it")
    p_inst.add_argument("--no-suggest", action="store_true", help="Show only Line 1 context bar without Line 2 suggestions")
    p_inst.add_argument("--no-tmux", action="store_true", help="Skip adding tmux keybindings")
    p_inst.add_argument("--no-prefix-key", action="store_true", help="Skip adding `Prefix + y` fallback binding")
    p_inst.add_argument("--force-prefix-y", action="store_true", help="Bind `Prefix + y` even if already bound")
    p_inst.add_argument("--key", default="M-y", help="tmux key to fill suggestion (default: M-y / Alt+y)")
    p_inst.add_argument("--alt-key", default="M-u", help="tmux key to cycle alternatives (default: M-u / Alt+u)")
    p_inst.set_defaults(func=cmd_install)

    p_un = sub.add_parser("uninstall", help="Remove Context Card CLI statusLine and tmux bindings")
    p_un.set_defaults(func=cmd_uninstall)

    p_st = sub.add_parser("status", help="Show CLI & tmux installation status")
    p_st.set_defaults(func=cmd_status)

    p_auth = sub.add_parser(
        "auth",
        help="Manage Suggested Reply LLM auth (shared across CLI, Desktop, and IDE unless cleared per surface)",
    )
    p_auth.add_argument("provider", choices=("status", "agy", "gemini", "openai", "clear"))
    p_auth.add_argument(
        "value",
        nargs="?",
        default="",
        help="Optional value: base URL for openai, or '-' to read gemini key from stdin (omit for secure getpass prompt; avoid passing raw keys on argv)",
    )
    p_auth.add_argument("--api-key", default="", help="API key for cloud OpenAI endpoints (pass '-' to read from stdin)")
    p_auth.add_argument("--model", default="", help="Optional model override")
    p_auth.add_argument("--all", action="store_true", help="For `clear`: also remove saved auth on Desktop/IDE surfaces")
    p_auth.set_defaults(func=cmd_auth)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
