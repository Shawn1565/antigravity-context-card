#!/usr/bin/env python3
"""Regression tests for Context Card v1.2.0 CLI statusLine, tmux fill, auth fallback, and hooks."""

from __future__ import annotations

import json
import os
import py_compile
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import cli_setup  # noqa: E402
import cli_statusline  # noqa: E402
import core  # noqa: E402
import habits  # noqa: E402
import tmux_fill  # noqa: E402


class _HabitsStateGuardMixin:
    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self._saved_habits = (
            habits.AGY_HOME,
            habits.DATA_DIR,
            habits.HABITS_DB_PATH,
            habits.LLM_CONFIG_PATH,
            habits.BRAIN_DIR,
            habits.SUMMARIES_DB,
            habits._LLM_CFG_CACHE,
            habits._LLM_CFG_SIG,
            habits._DB_INITIALIZED,
            habits._BOOTSTRAPPED,
        )

    def tearDown(self) -> None:
        (
            habits.AGY_HOME,
            habits.DATA_DIR,
            habits.HABITS_DB_PATH,
            habits.LLM_CONFIG_PATH,
            habits.BRAIN_DIR,
            habits.SUMMARIES_DB,
            habits._LLM_CFG_CACHE,
            habits._LLM_CFG_SIG,
            habits._DB_INITIALIZED,
            habits._BOOTSTRAPPED,
        ) = self._saved_habits
        super().tearDown()  # type: ignore[misc]


class TestSyntaxCompile(unittest.TestCase):
    def test_all_python_files_compile(self) -> None:
        py_files = [
            p
            for p in PLUGIN_ROOT.rglob("*.py")
            if "node_modules" not in p.parts and "__pycache__" not in p.parts
        ]
        self.assertGreaterEqual(len(py_files), 8)
        for pyf in py_files:
            py_compile.compile(str(pyf), doraise=True)


class TestCrossSurfaceAuth(_HabitsStateGuardMixin, unittest.TestCase):
    def test_clear_tombstone_no_secret_copy_and_cache_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            desk_dir = home / ".gemini" / "antigravity" / "plugin_data" / "context-card"
            cli_dir = home / ".gemini" / "antigravity-cli" / "plugin_data" / "context-card"
            desk_dir.mkdir(parents=True)
            desk_cfg = desk_dir / "llm_config.json"
            desk_cfg.write_text(
                json.dumps({"provider": "gemini", "api_key": "AIzaDesktopSecret", "_saved_gemini_key": "AIzaDesktopSecret"}),
                encoding="utf-8",
            )

            env_clean = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "GEMINI_API_KEY",
                    "GOOGLE_API_KEY",
                    "CONTEXT_CARD_GEMINI_API_KEY",
                    "CONTEXT_CARD_PROVIDER",
                    "CONTEXT_CARD_OPENAI_BASE_URL",
                    "CONTEXT_CARD_OPENAI_API_KEY",
                )
            }
            with mock.patch.dict(os.environ, env_clean, clear=True), mock.patch.object(Path, "home", return_value=home):
                # 1. CLI inherits Desktop config when CLI has no file yet
                habits.use_cli_context(cli_dir)
                cfg = habits.resolve_llm_config()
                self.assertEqual(cfg["provider"], "gemini")
                self.assertEqual(cfg["api_key"], "AIzaDesktopSecret")
                self.assertEqual(habits.get_active_llm_config_path(), desk_cfg)

                # 2. Saving CLI auth (`agy`) must NOT copy Desktop's secret key into CLI file
                with mock.patch.object(habits, "find_agy_bin", return_value="/usr/bin/agy"):
                    habits.save_llm_config(provider="agy")
                cli_raw = json.loads((cli_dir / "llm_config.json").read_text(encoding="utf-8"))
                self.assertEqual(cli_raw.get("provider"), "agy")
                self.assertNotIn("_saved_gemini_key", cli_raw)
                self.assertNotIn("api_key", cli_raw)

                # 3. CLI config takes precedence over Desktop config
                with mock.patch.object(habits, "find_agy_bin", return_value="/usr/bin/agy"):
                    cfg2 = habits.resolve_llm_config()
                    self.assertEqual(cfg2["provider"], "agy")
                    self.assertEqual(habits.get_active_llm_config_path(), cli_dir / "llm_config.json")

                # 4. Clearing CLI while Desktop still exists writes `provider=off` tombstone so CLI stops
                still = habits.save_llm_config(clear_key=True)
                self.assertFalse(still)
                self.assertFalse(habits.has_llm_configured())
                self.assertFalse(habits.has_saved_gemini_key())
                cli_tomb = json.loads((cli_dir / "llm_config.json").read_text(encoding="utf-8"))
                self.assertEqual(cli_tomb, {"provider": "off"})

                # 5. Sibling tombstone on CLI must NOT leak onto Desktop or block Desktop GEMINI_API_KEY
                desk_cfg.unlink()
                habits.DATA_DIR = desk_dir
                habits.LLM_CONFIG_PATH = desk_cfg
                with mock.patch.dict(os.environ, {**env_clean, "GEMINI_API_KEY": "AIzaEnvKey"}, clear=True):
                    desk_res = habits.resolve_llm_config()
                    self.assertEqual(desk_res["provider"], "gemini")
                    self.assertEqual(desk_res["api_key"], "AIzaEnvKey")

                # 6. Long-lived Desktop cache auto-invalidates when CLI edits/clears sibling config
                habits.use_cli_context(cli_dir)
                habits.save_llm_config(provider="gemini", api_key="AIzaCliShared")
                habits.DATA_DIR = desk_dir
                habits.LLM_CONFIG_PATH = desk_cfg
                self.assertEqual(habits.resolve_llm_config()["api_key"], "AIzaCliShared")
                # External process deletes CLI file -> Desktop cache signature must detect change immediately
                (cli_dir / "llm_config.json").unlink()
                self.assertFalse(habits.has_llm_configured())


class TestPaneIsolationAndWorkerLock(_HabitsStateGuardMixin, unittest.TestCase):
    def test_pane_stale_and_no_cross_pane_leak(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ddir = Path(td)
            core.write_json_atomic(
                ddir / "cli_cache" / "suggest_cid-A.json",
                {
                    "cid": "cid-A",
                    "turn_key": "cid-A:1:aaa",
                    "status": "ready",
                    "primary": "Reply for A",
                    "alternatives": ["Alt A2"],
                    "updated_at": time.time(),
                },
                mode=0o600,
            )
            _p_path, p_data = tmux_fill._load_pane_data(ddir, "%99")
            self.assertEqual(p_data, {})

            stale_pane = {
                "pane_id": "%10",
                "cid": "cid-A",
                "turn_key": "cid-A:1:aaa",
                "primary": "",
                "alternatives": [],
                "alt_index": 0,
                "stale": True,
                "updated_at": time.time(),
            }
            refreshed = tmux_fill._refresh_from_suggest_cache(ddir, stale_pane, core)
            self.assertTrue(refreshed.get("stale"))
            self.assertEqual(refreshed.get("primary"), "")

            idle_pane = {**stale_pane, "stale": False}
            refreshed2 = tmux_fill._refresh_from_suggest_cache(ddir, idle_pane, core)
            self.assertEqual(refreshed2.get("primary"), "Reply for A")

    def test_late_worker_does_not_unmark_stale_pane(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ddir = Path(td)
            # Pane starts working on new turn -> marked stale
            cli_statusline._bind_pane_working(ddir, "%7", "cid-1", "tk-old")
            # Late worker from previous turn finishes with only_if_matching=True -> must NOT clear stale
            res = cli_statusline.sync_pane_state(
                ddir,
                "%7",
                "cid-1",
                "tk-old",
                "Old stale reply",
                [],
                stale=False,
                only_if_matching=True,
            )
            self.assertTrue(res["stale"])
            self.assertEqual(res["primary"], "")

    def test_sync_pane_state_skips_redundant_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ddir = Path(td)
            cli_statusline.sync_pane_state(ddir, "%5", "cid-1", "tk-1", "hi", ["alt1"], stale=False)
            pfile = ddir / "panes" / "p_5.json"
            data = json.loads(pfile.read_text(encoding="utf-8"))
            data["alt_index"] = 1
            core.write_json_atomic(pfile, data, mode=0o600)
            mt1 = pfile.stat().st_mtime_ns
            time.sleep(0.01)
            res = cli_statusline.sync_pane_state(ddir, "%5", "cid-1", "tk-1", "hi", ["alt1"], stale=False)
            self.assertEqual(pfile.stat().st_mtime_ns, mt1)
            self.assertEqual(res["alt_index"], 1)

    def test_atomic_lock_prevents_concurrent_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "suggest_cid1.lock"
            n1 = cli_statusline._try_acquire_worker_lock(lock_path, "cid1", "tk1")
            self.assertIsNotNone(n1)
            n2 = cli_statusline._try_acquire_worker_lock(lock_path, "cid1", "tk1")
            self.assertIsNone(n2)
            cli_statusline._release_worker_lock(lock_path, n1)
            self.assertFalse(lock_path.exists())


class TestCliSetupAndTmuxQuoting(_HabitsStateGuardMixin, unittest.TestCase):
    def test_install_preserves_mode_symlinks_and_restores_previous_statusline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            settings_path = home / ".gemini" / "antigravity-cli" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            orig_sl = {"type": "command", "command": "~/my-bar.sh", "enabled": True}
            fd = os.open(str(settings_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"model": "Gemini 4 Neon (High)", "statusLine": orig_sl}, f)

            # Dotfiles symlink for ~/.tmux.conf must stay a symlink after install & uninstall
            dotfiles_tmux = home / "dotfiles" / "tmux.conf"
            dotfiles_tmux.parent.mkdir(parents=True)
            dotfiles_tmux.write_text("set -g mouse on\n", encoding="utf-8")
            os.chmod(str(dotfiles_tmux), 0o640)
            tmux_conf = home / ".tmux.conf"
            tmux_conf.symlink_to(dotfiles_tmux)

            env_no_tmux = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
            with (
                mock.patch.dict(os.environ, env_no_tmux, clear=True),
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cli_setup, "CLI_SETTINGS_PATH", settings_path),
                mock.patch.object(cli_setup, "TMUX_CONF_PATH", tmux_conf),
                mock.patch.object(shutil, "which", return_value="/usr/bin/tmux"),
                mock.patch.object(cli_setup, "_reload_tmux_if_active", return_value=False),
            ):
                ns_inst = mock.MagicMock(
                    replace=False,
                    no_suggest=False,
                    no_tmux=False,
                    no_prefix_key=False,
                    force_prefix_y=False,
                    key="M-y",
                    alt_key="M-u",
                )
                rc = cli_setup.cmd_install(ns_inst)
                self.assertEqual(rc, 0)
                self.assertEqual(stat.S_IMODE(settings_path.stat().st_mode), 0o600)
                self.assertTrue(tmux_conf.is_symlink())
                self.assertEqual(stat.S_IMODE(dotfiles_tmux.stat().st_mode), 0o640)

                installed, sug = core.is_cli_statusline_installed(settings_path)
                self.assertTrue(installed and sug)

                conf_txt = dotfiles_tmux.read_text(encoding="utf-8")
                self.assertIn("if-shell -F", conf_txt)
                self.assertIn("--pane #{pane_id}", conf_txt)

                rc2 = cli_setup.cmd_uninstall(mock.MagicMock())
                self.assertEqual(rc2, 0)
                self.assertTrue(tmux_conf.is_symlink())
                restored = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertEqual(restored["statusLine"], orig_sl)
                self.assertEqual(stat.S_IMODE(settings_path.stat().st_mode), 0o600)
                installed2, _ = core.is_cli_statusline_installed(settings_path)
                self.assertFalse(installed2)

    def test_custom_clone_dir_name_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            custom_root = Path(td) / "ctxcard-custom"
            custom_root.mkdir()
            sl = {"type": "command", "command": f"python3 {custom_root}/cli_statusline.py"}
            self.assertTrue(core.is_our_statusline_config(sl, plugin_root=custom_root, data_dir=Path(td)))

    @unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
    def test_tmux_block_with_spaces_single_quote_and_hash_parses_in_real_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tricky_dir = Path(td) / "dir with space" / "user's #1 plugin"
            tricky_dir.mkdir(parents=True)
            (tricky_dir / "tmux_fill.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            with mock.patch.object(cli_setup, "PLUGIN_ROOT", tricky_dir):
                block = cli_setup._build_tmux_block("M-y", "M-u", include_prefix_y=True)

            conf = Path(td) / "test.tmux.conf"
            conf.write_text(block, encoding="utf-8")
            sock = f"cc-quote-{os.getpid()}"
            try:
                res = subprocess.run(
                    ["tmux", "-L", sock, "-f", str(conf), "new-session", "-d", "-s", "t", "sleep", "5"],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                self.assertEqual(res.returncode, 0, f"tmux failed to parse config:\n{block}\nstderr={res.stderr}")
                keys = subprocess.check_output(["tmux", "-L", sock, "list-keys", "-T", "root", "M-y"], text=True)
                self.assertIn("if-shell -F", keys)
                self.assertIn("--pane #{pane_id}", keys)
                self.assertIn("tmux_fill.py", keys)
            finally:
                subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)


class TestStopHookOptIn(unittest.TestCase):
    def test_stop_hook_skips_when_cli_statusline_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            tpath = home / ".gemini" / "antigravity-cli" / "brain" / "abc123" / ".system_generated" / "logs" / "transcript.jsonl"
            tpath.parent.mkdir(parents=True)
            tpath.write_text(
                json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "Done."}) + "\n",
                encoding="utf-8",
            )
            payload = {
                "conversationId": "abc123",
                "transcriptPath": str(tpath),
                "cwd": str(home),
                "terminationReason": "NO_TOOL_CALL",
            }
            env = dict(os.environ)
            env["HOME"] = str(home)
            res = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "hooks" / "stop_hook.py")],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            self.assertEqual(res.stdout.strip(), "{}")
            cache_dir = home / ".gemini" / "antigravity-cli" / "plugin_data" / "context-card" / "cli_cache"
            self.assertFalse(cache_dir.exists())


class TestModelLimits(unittest.TestCase):
    def test_known_and_unknown_model_limits(self) -> None:
        self.assertEqual(core.format_model("gemini-4-neon-high"), ("Gemini 4 Neon (High)", 512_000))
        self.assertEqual(core.format_model_limits("gemini-4-neon-high"), ("Gemini 4 Neon (High)", 512_000, 1_048_576))
        self.assertEqual(core.format_model("gemini-pro-orchestrator-neon"), ("Orchestrator (Neon)", 512_000))
        self.assertEqual(
            core.format_model_limits("gemini-pro-orchestrator-neon"),
            ("Orchestrator (Neon)", 512_000, 1_048_576),
        )
        self.assertEqual(core.format_model("claude-opus-5-5-high"), ("Claude Opus 5.5 (High)", 1_000_000))
        self.assertEqual(core.format_model("claude-sonnet-5-5-medium"), ("Claude Sonnet 5.5 (Medium)", 1_000_000))
        self.assertEqual(core.format_model("gpt-oss-120b-medium"), ("GPT-OSS 120B (Medium)", 131_072))
        self.assertEqual(core.format_model("some-future-model"), ("Some Future Model", 512_000))
        self.assertEqual(core.format_model(None), ("Unknown model", 512_000))

    def test_adaptive_limit_lift_and_env_override(self) -> None:
        env_clean = {k: v for k, v in os.environ.items() if k != "CONTEXT_CARD_CONTEXT_LIMIT"}
        with mock.patch.dict(os.environ, env_clean, clear=True):
            # Normal usage stays on 512K effective limit with 1M native
            self.assertEqual(
                core.resolve_effective_limit(512_000, 1_048_576, 250_000, 240_000),
                (512_000, 1_048_576),
            )
            # Slight pre-compaction overshoot right before CHECKPOINT must NOT jump down to 49%
            self.assertEqual(
                core.resolve_effective_limit(512_000, 1_048_576, 520_000, 514_000),
                (512_000, 1_048_576),
            )
            # Genuine overflow beyond buffer lifts automatically to native 1M
            self.assertEqual(
                core.resolve_effective_limit(512_000, 1_048_576, 565_000, 560_000),
                (1_048_576, 1_048_576),
            )
        with mock.patch.dict(os.environ, {**env_clean, "CONTEXT_CARD_CONTEXT_LIMIT": "768000"}, clear=True):
            self.assertEqual(
                core.resolve_effective_limit(512_000, 1_048_576, 100_000, 100_000),
                (768_000, 1_048_576),
            )

    def test_cli_statusline_uses_512k_effective_and_lifts_on_overflow(self) -> None:
        import re

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        with tempfile.TemporaryDirectory() as td:
            args = mock.MagicMock(data_dir=td, replace=False, no_suggest=True)
            base_payload = {
                "terminal_width": 120,
                "model": {"id": "gemini-4-neon-high", "display_name": "Gemini 4 Neon (High)"},
                "context_window": {
                    "context_window_size": 1_048_576,
                    "used_percentage": 24.4,
                    "current_usage": {
                        "input_tokens": 56_000,
                        "cache_read_input_tokens": 200_000,
                        "output_tokens": 0,
                    },
                },
            }
            out_normal = ansi_re.sub("", cli_statusline.render_statusline(base_payload, args))
            self.assertIn("50.0%", out_normal)
            self.assertIn("(256K/512K)", out_normal)

            # Pre-compaction slight overshoot caps percentage at 100.0% while showing real token count
            overshoot_payload = {
                **base_payload,
                "context_window": {
                    "context_window_size": 1_048_576,
                    "used_percentage": 49.3,
                    "current_usage": {
                        "input_tokens": 7_100,
                        "cache_read_input_tokens": 500_000,
                        "output_tokens": 10_000,
                    },
                },
            }
            out_cap = ansi_re.sub("", cli_statusline.render_statusline(overshoot_payload, args))
            self.assertIn("100.0%", out_cap)
            self.assertNotIn("101.", out_cap)
            self.assertIn("(517.1K/512K)", out_cap)

            overflow_payload = {
                **base_payload,
                "context_window": {
                    "context_window_size": 1_048_576,
                    "used_percentage": 57.2,
                    "current_usage": {
                        "input_tokens": 100_000,
                        "cache_read_input_tokens": 500_000,
                        "output_tokens": 0,
                    },
                },
            }
            out_lift = ansi_re.sub("", cli_statusline.render_statusline(overflow_payload, args))
            self.assertIn("(600K/1M)", out_lift)


class TestServerLifeAndPreInvocation(unittest.TestCase):
    def test_import_server_does_not_clobber_server_life_and_hook_handles_stale_pid(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            data_dir = home / ".gemini" / "antigravity" / "plugin_data" / "context-card"
            data_dir.mkdir(parents=True)
            db_path = data_dir / "runtime_state.db"
            live_pid = os.getpid()
            dead_pid = 999_999_999
            cid = "11111111-2222-3333-4444-555555555555"

            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE server_life (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    pid INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    unconfirmed_auto_ts REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE session_bindings (
                    cid TEXT PRIMARY KEY,
                    workspace_path TEXT NOT NULL DEFAULT '',
                    hook_seen_ts REAL NOT NULL DEFAULT 0,
                    explicit_server_pid INTEGER NOT NULL DEFAULT 0,
                    explicit_bound_ts REAL NOT NULL DEFAULT 0,
                    hook_reminded_ts REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE pane_clients (
                    client_id TEXT PRIMARY KEY,
                    cid TEXT NOT NULL,
                    bind_mode TEXT NOT NULL DEFAULT 'explicit',
                    updated_ts REAL NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO server_life (id, pid, started_at, unconfirmed_auto_ts) VALUES (1, ?, ?, 0)",
                (dead_pid, time.time() - 600),
            )
            conn.execute(
                "INSERT INTO session_bindings (cid, explicit_server_pid, explicit_bound_ts) VALUES (?, ?, ?)",
                (cid, live_pid, time.time() - 300),
            )
            conn.commit()
            conn.close()

            import site

            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("ANTIGRAVITY_APP_DATA_DIR", "CONTEXT_CARD_DATA_DIR", "PLUGIN_DATA")
            }
            env["HOME"] = str(home)
            env["ANTIGRAVITY_APP_DATA_DIR"] = str(home / ".gemini" / "antigravity")
            user_site = site.getusersitepackages()
            env["PYTHONPATH"] = f"{user_site}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else user_site
            # 1. Importing server must NOT overwrite server_life.pid
            subprocess.run(
                [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(PLUGIN_ROOT)!r}); import server"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("SELECT pid FROM server_life WHERE id = 1").fetchone()
            conn.close()
            self.assertEqual(row[0], dead_pid)

            # 2. pre_invocation must NOT re-inject when explicit_server_pid is still alive even if server_life.pid died
            payload = {
                "conversationId": cid,
                "invocationNum": 1,
                "initialNumSteps": 10,
                "cwd": str(home),
                "workspacePaths": [str(home)],
            }
            res = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "hooks" / "pre_invocation.py")],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            self.assertEqual(res.stdout.strip(), "{}")

            # 3. Active server handling an explicit open self-heals a dead server_life.pid
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"import os, sys; sys.path.insert(0, {str(PLUGIN_ROOT)!r}); import server; server._record_explicit_open({cid!r}); print(os.getpid())",
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            healed_pid = int(proc.stdout.strip())
            conn = sqlite3.connect(str(db_path))
            row2 = conn.execute("SELECT pid FROM server_life WHERE id = 1").fetchone()
            conn.close()
            self.assertEqual(row2[0], healed_pid)

    def test_dist_app_html_has_back_to_chat_and_detached_hint(self) -> None:
        html = (PLUGIN_ROOT / "dist" / "app.html").read_text(encoding="utf-8")
        self.assertIn("Back to chat", html)
        self.assertIn("Detached pane", html)
        self.assertIn("toggle-outputs", html)
        self.assertIn("Suggested reply settings", html)
        self.assertNotIn("Confirm /compact?", html)
        self.assertNotIn("Sent /compact to conversation", html)
        self.assertNotIn(">Habits</button>", html)
        self.assertNotIn(" samples", html)
        self.assertNotIn("habit-fb-list", html)


class TestManualOpenWindowDetection(unittest.TestCase):
    def test_manual_open_prefers_last_user_view_time_over_busy_background_agent(self) -> None:
        import server

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = root / "brain"
            ann = root / "annotations"
            ann.mkdir(parents=True)
            now = time.time()

            def make_conv(cid: str, transcript_age: float, view_age: float, *, archived: bool = False) -> None:
                tpath = brain / cid / ".system_generated" / "logs" / "transcript.jsonl"
                tpath.parent.mkdir(parents=True, exist_ok=True)
                tpath.write_text('{"type":"USER_INPUT","content":"hi"}\n', encoding="utf-8")
                t_ts = now - transcript_age
                os.utime(tpath, (t_ts, t_ts))
                v_sec = int(now - view_age)
                arch_str = " archived:true" if archived else ""
                p = ann / f"{cid}.pbtxt"
                p.write_text(
                    f'title:"{cid}"{arch_str} last_user_view_time:{{seconds:{v_sec} nanos:0}}\n',
                    encoding="utf-8",
                )
                os.utime(p, (v_sec, v_sec))

            # Window A: visible & already has an active pane (viewed 1s ago)
            make_conv("win-a-bound", transcript_age=60.0, view_age=1.0)
            # Window B: idle for 10 min, user just switched here & clicked '+' (viewed 3s ago, NO pane)
            make_conv("win-b-target", transcript_age=600.0, view_age=3.0)
            # Window C: background agent actively writing transcript 0.2s ago, last viewed 45s ago
            make_conv("win-c-busy-bg", transcript_age=0.2, view_age=45.0)
            # Window D: archived conversation viewed 0.5s ago (must be ignored)
            make_conv("win-d-archived", transcript_age=1.0, view_age=0.5, archived=True)

            with (
                mock.patch.object(server, "BRAIN_DIR", brain),
                mock.patch.object(server, "ANNOTATIONS_DIR", ann),
                mock.patch.object(server, "SUMMARIES_DB", root / "missing.db"),
                mock.patch.object(server, "_get_runtime_maps", return_value=({"win-a-bound"}, {})),
            ):
                picked = server._find_latest_conversation_id(exclude_active_panes=True)
                self.assertEqual(picked, "win-b-target")

                # Quick close-and-reopen case: if only unbound window is stale (>5 min older than top),
                # stay on the most recently viewed window instead of jumping to an old tab.
                make_conv("win-b-target", transcript_age=600.0, view_age=600.0)
                make_conv("win-c-busy-bg", transcript_age=0.2, view_age=600.0)
                picked_reopen = server._find_latest_conversation_id(exclude_active_panes=True)
                self.assertEqual(picked_reopen, "win-a-bound")


if __name__ == "__main__":
    unittest.main()
