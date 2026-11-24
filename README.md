# Antigravity Context Card

[English](#english) | [中文说明](#中文说明)

---

<a id="english"></a>

## English

A live **Context Window Monitor & Smart Reply** plugin for **Google Antigravity** across **Desktop 2.0**, **IDE**, and **Antigravity CLI (`agy`)**:

- **Desktop 2.0 & IDE**: Full interactive Claude Desktop-style **MCP App** side panel with progress bar, clickable Memory / Skill / Output previews, one-click `/compact`, and Habits drawer.
- **Antigravity CLI (`agy`) — Native `statusLine` Bar**: A responsive, 1–2 line ANSI segmented progress bar docked right below the CLI prompt box showing live `Messages` / `Thinking` / `Tool I/O` / `Cache %` / `Checkpoints` breakdown plus asynchronous **Suggested Replies**.
- **Inside `tmux` — One-Key Prompt Insertion**: Automatically upgrades with pane-isolated suggestion sync so pressing **`Alt+y`** (smart `agy`-only passthrough, or **`Prefix + y`** on macOS/SSH) types the suggested reply straight into the `agy` prompt box without pressing Enter—ready to send or tweak. Press **`Alt+u`** to cycle alternatives.

Unlike generic context meters that lump everything into `Messages`, **Context Card** inspects Antigravity's local conversation trajectory in real time and separates **pure dialogue (`Messages`)**, **model reasoning (`Thinking`)**, and **file/command payloads (`Tool calls & outputs`)**—so you always know *what* is consuming your context window and *when* to `/compact` or delegate to subagents.

### Features

- **Granular Token Breakdown**: Live progress bar and per-category token & percentage metrics:
  - `Messages` (user prompts & visible assistant replies)
  - `Thinking` (chain-of-thought / reasoning tokens retained in context)
  - `Tool calls & outputs` (file reads, terminal outputs, search results)
  - `System tools`, `Memory files` (`GEMINI.md` / `AGENTS.md` / `CLAUDE.md`), `Skills`, `System prompt`, `Checkpoint summary`, and `Free space`
- **Prompt Cache Telemetry**: Real-time prompt cache hit ratio (`Cache %`) and active step / checkpoint counters.
- **Desktop / IDE Multi-Window Binding**:
  - Auto-opens and locks to each window's exact `conversation_id` via a lightweight rule + `PreInvocation` lifecycle hook (even on resumed historical chats or large workspaces).
  - Subagent-safe & CLI-safe: ignores child/subagent runs and skips opening the HTML pane in Antigravity CLI sessions.
  - Active-pane exclusion when opened manually from `+ -> Context`, plus a one-click session switcher.
- **Antigravity CLI (`agy`) & `tmux` Integration**:
  - Pure-stdlib transcript parser with disk `(mtime, size)` & skills-catalog caching (~70ms warm / <100ms typical per statusLine refresh including Python startup, zero `mcp` import overhead).
  - Stacked below `agy`'s built-in status bar by default (`stack_with_default: true`) so you never lose mode (`plan`/`auto`), sandbox, or task indicators—with `--replace` available for minimalist layouts, and automatic restoration of any prior custom `statusLine` on uninstall.
  - Strictly opt-in `Stop`-hook pre-warming + atomic-locked detached suggestion worker so `statusLine` never blocks and never sends network requests unless CLI integration & auth are explicitly enabled.
  - **Pane-isolated `tmux` fill**: each tmux pane tracks its own session under `panes/<pane_id>.json` (`0600`), never crossing wires between parallel `agy` windows. Uses native tmux `if-shell -F` routing so non-`agy` panes (vim, zsh, bash) never spawn Python and experience zero key delay.
- **One-Click Suggested Replies (Gemini Subscription + BYOK)**:
  - Works **100% offline** for all context monitoring features—zero network calls until you opt in.
  - **Cross-Surface Auth Sharing**: Auth configured in Desktop 2.0 (or via `cli_setup.py auth`) is automatically shared between Desktop, IDE, and CLI.
  - **One-Click Gemini Subscription Auth**: Reuse your local `agy` Google subscription OAuth—no API key required.
  - **Optional BYOK (~1s ultra-low latency)**: Supports your own **Gemini API Key** or any **OpenAI-compatible endpoint** (Ollama, OpenRouter, DeepSeek, OpenAI).

### Quick Install

#### 1. Desktop 2.0 & IDE (MCP App Side Panel)

1. **Ensure `python3` (3.10+) is on your `PATH` and has the `mcp` SDK installed**:
   ```bash
   pip install "mcp>=1.2.0"
   ```
2. **Clone directly into Antigravity's plugins directory** (`dist/app.html` is pre-built—no Node.js build step required):
   ```bash
   mkdir -p ~/.gemini/config/plugins
   git clone https://github.com/Shawn1565/antigravity-context-card.git ~/.gemini/config/plugins/context-card
   ```
3. Start a new chat (or click `+ -> Context` in the right side panel) and Context Card will appear.

#### 2. Antigravity CLI (`agy`) & `tmux` (Opt-In Setup)

After cloning the plugin above, run the opt-in CLI installer (backs up `~/.gemini/antigravity-cli/settings.json` and `~/.tmux.conf` automatically; pure stdlib—does not even require `pip install mcp` if you only use CLI):

```bash
python3 ~/.gemini/config/plugins/context-card/cli_setup.py install
```

What this configures:
- **In any terminal (Plan A)**: Stacks a 1–2 line color Context Card bar directly below `agy`'s built-in status line:
  ```text
  Ctx [█████▓▒░░░░░░░░░] 20.1% (211K/1M) │ Msg 18K · Think 62K · Tool 115K · Cache 84% · Ckpt 1
  💡 Run the unit tests and commit the changes to the working branch  [Alt+y fill · Alt+u alt]
  ```
- **Inside `tmux` (Plan B)**: Automatically adds smart keybindings to `~/.tmux.conf`:
  - **`Alt+y`** (or **`Prefix + y`** fallback for macOS terminals without Option-as-Meta): Inserts the suggested reply straight into `agy`'s prompt box **without pressing Enter**. Passes `Alt+y` through transparently in non-`agy` panes.
  - **`Alt+u`**: Cycles through alternative suggestions (previewed in tmux status bar; press `Alt+y` to insert).

Optional flags & management:
```bash
# Replace agy's default status line instead of stacking below it
python3 ~/.gemini/config/plugins/context-card/cli_setup.py install --replace

# Skip tmux bindings or customize keys
python3 ~/.gemini/config/plugins/context-card/cli_setup.py install --no-tmux
python3 ~/.gemini/config/plugins/context-card/cli_setup.py install --key M-y --alt-key M-u

# Check status or cleanly uninstall
python3 ~/.gemini/config/plugins/context-card/cli_setup.py status
python3 ~/.gemini/config/plugins/context-card/cli_setup.py uninstall
```

### Suggested Replies Auth (Gemini Subscription or BYOK)

Context monitoring requires **no authentication or network**. For smart next-reply suggestions, **4 explicit opt-in modes** are supported (shared automatically across Desktop, IDE, and CLI):

- **Option A — Gemini Subscription via `agy` CLI (Zero API Key)**:
  - **Desktop/IDE**: Click **Use Subscription (agy)** once in the panel.
  - **CLI**:
    ```bash
    python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth agy
    ```
- **Option B — Gemini API Key (~1s fastest response)**:
  - **Desktop/IDE**: Open `Habits` -> `Set Key` and click **Save** (toggle `Auto` / `Off` anytime in the panel header).
  - **CLI** (interactive prompt or stdin — avoids storing keys in shell history):
    ```bash
    python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth gemini
    # Or via stdin:
    # printf '%s' "$GEMINI_API_KEY" | python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth gemini -
    ```
  Stored locally with `0600` permissions in `plugin_data/context-card/llm_config.json` (never inside the plugin folder). Note that saved auth is shared across CLI, Desktop, and IDE (including Desktop habit learning) unless cleared per surface.
- **Option C — Environment variable**:
  ```bash
  export GEMINI_API_KEY="AIza..."
  # Or explicitly select subscription mode via env:
  # export CONTEXT_CARD_PROVIDER="agy"
  ```
- **Option D — OpenAI-compatible / Local Ollama**:
  ```bash
  python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth openai http://localhost:11434/v1 --model qwen2.5:7b
  # Or via env vars:
  export CONTEXT_CARD_OPENAI_BASE_URL="http://localhost:11434/v1"
  export CONTEXT_CARD_SUGGEST_MODEL="qwen2.5:7b"
  # Set CONTEXT_CARD_OPENAI_API_KEY="sk-..." for cloud providers (OpenRouter, DeepSeek, OpenAI)
  ```

Clear saved CLI auth anytime with `python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth clear`.

### Building the Desktop/IDE UI from Source (Optional)

If you modify `ui/src/main.ts` or `ui/build.mjs`:

```bash
cd ~/.gemini/config/plugins/context-card/ui
npm install
npm run build
```

### Privacy & Security

- All conversation parsing, CLI caches, tmux pane state (`panes/*.json`), and habit SQLite storage stay **100% local** under Antigravity's `${PLUGIN_DATA}` directory (`0600` permissions for auth and pane suggestion files).
- **Strict Opt-In Network Policy**: No telemetry or external LLM requests are made by default. Recent dialogue excerpts and style samples are sent to your chosen model **only after** you explicitly enable Suggested Replies.

---

<a id="中文说明"></a>

## 中文说明

专为 **Google Antigravity** 打造的**全端上下文窗口实时监控与智能回复插件**，完整支持 **Desktop 2.0、IDE 与 Antigravity CLI (`agy`)**：

- **Desktop 2.0 / IDE**：Claude Desktop 风格的交互式 **MCP App** 侧边栏，支持分段进度条、规则/Skill/产物直达预览、一键 `/compact` 与习惯学习抽屉。
- **Antigravity CLI (`agy`) — 原生 `statusLine` 磁吸横条（方案 A）**：利用官方 `statusLine` 在 CLI 输入框下方磁吸 1–2 行 ANSI 彩色进度条，实时拆分展示 `Messages` / `Thinking` / `Tool I/O` / `Cache %` / `Checkpoints`，并在空闲时异步展示 **智能建议回复**。
- **`tmux` 环境增强 — 快捷键一键填入输入框（方案 B）**：自动识别 `$TMUX_PANE` 并按窗格隔离状态，在 `agy` 窗格按 **`Alt+y`**（或 macOS/SSH 通用兜底 **`Prefix + y`**）即可将建议回复直接填入输入框（**不自动回车**，可修改后再发）；按 **`Alt+u`** 轮换备选建议；在非 `agy` 窗格自动原样透传按键，零干扰。

不同于把所有对话和工具输出全部混入 `Messages` 的笼统统计方式，**Context Card** 实时解析本地会话轨迹，将 **`Messages`（纯对话）**、**`Thinking`（模型推理链）** 与 **`Tool calls & outputs`（文件读取与终端输出）** 清晰拆分——让你一眼看清到底是大文件/长日志吃满了窗口，还是对话轮次过多需要 `/compact` 或派发子代理。

### 核心特性

- **精细化上下文分项统计**：
  - 独立统计 `Messages`、`Thinking`、`Tool calls & outputs`、`System tools`、`Memory files`（`GEMINI.md` / `AGENTS.md` / `CLAUDE.md`）、`Skills`、`System prompt`、`Checkpoint summary` 与 `Free space`。
  - 实时显示 **Prompt Cache 命中率（`Cache %`）**、活跃步数及 `/compact` 检查点次数；超过 80% 自动标红提醒 `/compact`。
- **Desktop / IDE 可靠多窗口绑定**：
  - 结合轻量规则与 `PreInvocation` 生命周期钩子自动打开并精确绑定当前 `conversation_id`。
  - 自动跳过子代理会话与 Antigravity CLI 会话，避免在纯终端中触发无意义的侧边栏打开调用。
- **CLI 原生 `statusLine` + `tmux` 一键上屏**：
  - 核心解析逻辑抽离为纯标准库模块 `core.py`，配合 `(mtime, size)` 与 skills 目录磁盘缓存，单次状态栏刷新约 **~70ms（含 Python 进程启动）**，不拖慢 TUI。
  - 默认以 `stack_with_default: true` 叠加在官方底栏下方，完整保留 `plan`/`auto` 模式、沙箱与后台任务提示（支持 `--replace` 替换模式，卸载时自动恢复用户原有自定义 `statusLine`）。
  - 仅在用户显式安装 CLI 集成并开启鉴权后，才通过 `Stop` 钩子 + 原子锁脱钩后台 worker 异步抢跑生成建议回复，绝不阻塞交互或静默外发。
  - 按 `$TMUX_PANE` 独立隔离缓存（`panes/<pane_id>.json`，`0600` 权限），多窗格并行跑 `agy` 绝不串话；通过 tmux 原生 `if-shell -F` 分流，非 `agy` 窗格零子进程开销、零按键延迟。
- **跨端共享智能建议回复鉴权（Gemini 订阅 + BYOK）**：
  - **上下文监控主功能 100% 离线运行**，未显式配置鉴权前零外部网络请求。
  - 在 Desktop 2.0 面板中配置好的订阅通道或 API Key，CLI 会自动回退共享，无需重复填写。

### 快速安装

#### 1. Desktop 2.0 & IDE（右侧边栏 MCP App）

1. **确保本机 `python3`（3.10+）已在 `PATH` 中且安装了 `mcp` SDK**：
   ```bash
   pip install "mcp>=1.2.0"
   ```
2. **直接克隆到 Antigravity 插件目录**（仓库已内置预编译好的 `dist/app.html`，无需 Node.js 编译）：
   ```bash
   mkdir -p ~/.gemini/config/plugins
   git clone https://github.com/Shawn1565/antigravity-context-card.git ~/.gemini/config/plugins/context-card
   ```
3. 新建对话（或点击右侧边栏 `+ -> Context`）即可使用。

#### 2. Antigravity CLI (`agy`) 与 `tmux`（显式一键配置）

克隆插件后，执行安装脚本（自动备份 `~/.gemini/antigravity-cli/settings.json` 与 `~/.tmux.conf`；纯 CLI 使用甚至无需安装 `mcp` 依赖）：

```bash
python3 ~/.gemini/config/plugins/context-card/cli_setup.py install
```

安装后效果：
- **任意终端（方案 A）**：在 `agy` 输入框下方磁吸展示 1–2 行自适应宽度的彩色状态条：
  ```text
  Ctx [█████▓▒░░░░░░░░░] 20.1% (211K/1M) │ Msg 18K · Think 62K · Tool 115K · Cache 84% · Ckpt 1
  💡 继续跑一下单测并把改动提交到工作分支  [Alt+y fill · Alt+u alt]
  ```
- **在 `tmux` 内（方案 B）**：自动写入受管配置块到 `~/.tmux.conf`：
  - **`Alt+y`**（或 **`Prefix + y`** 兜底，兼容未开启 Meta 键的 macOS 终端）：把建议回复直接填入当前 `agy` 输入框（**不自动发送**，可继续编辑）。在非 `agy` 窗格原样透传按键。
  - **`Alt+u`**：轮换备选建议（在 tmux 底部状态栏预览，按 `Alt+y` 填入）。

常用管理命令：
```bash
# 使用替换模式（替代官方默认底栏，自动补全模型名与模式标识）
python3 ~/.gemini/config/plugins/context-card/cli_setup.py install --replace

# 仅安装 CLI 状态栏（不改 tmux 配置）或自定义快捷键
python3 ~/.gemini/config/plugins/context-card/cli_setup.py install --no-tmux
python3 ~/.gemini/config/plugins/context-card/cli_setup.py install --key M-y --alt-key M-u

# 查看当前状态 / 一键干净卸载
python3 ~/.gemini/config/plugins/context-card/cli_setup.py status
python3 ~/.gemini/config/plugins/context-card/cli_setup.py uninstall
```

### 智能建议回复鉴权配置（Gemini 订阅或自带密钥）

上下文监控无需任何网络或鉴权。建议回复支持以下 **4 种显式接入方式**（Desktop、IDE 与 CLI 自动共享配置）：

- **方式一：一键复用 Gemini 订阅（免 API Key）**
  - **桌面端**：首次点击面板中的 **Use Subscription (agy)**。
  - **CLI**：
    ```bash
    python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth agy
    ```
- **方式二：填写 Gemini API Key（约 1 秒极速直连）**
  - **桌面端**：展开 `Habits` 点击 **Set Key** 保存（可随时在面板右上角切换 `Auto` / `Off`）。
  - **CLI**（推荐交互输入或通过 stdin 传入，避免密钥落入 shell 历史记录）：
    ```bash
    python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth gemini
    # 或通过 stdin 传入：
    # printf '%s' "$GEMINI_API_KEY" | python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth gemini -
    ```
  密钥以 `0600` 权限保存在 `plugin_data/context-card/llm_config.json`，绝不写入代码目录。保存的鉴权会在 CLI、Desktop 与 IDE 间共享（包含桌面端的本地习惯学习，除非在对应端显式清除）。
- **方式三：环境变量配置**
  ```bash
  export GEMINI_API_KEY="AIza..."
  # 或显式指定走订阅通道：
  # export CONTEXT_CARD_PROVIDER="agy"
  ```
- **方式四：接入本地 Ollama 或任意 OpenAI 兼容接口**
  ```bash
  python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth openai http://localhost:11434/v1 --model qwen2.5:7b
  # 或通过环境变量：
  export CONTEXT_CARD_OPENAI_BASE_URL="http://localhost:11434/v1"
  export CONTEXT_CARD_SUGGEST_MODEL="qwen2.5:7b"
  # 云端兼容服务（OpenRouter / DeepSeek / OpenAI）可加 --api-key 或设置 CONTEXT_CARD_OPENAI_API_KEY
  ```

如需清除已保存的 CLI 鉴权，执行 `python3 ~/.gemini/config/plugins/context-card/cli_setup.py auth clear`。

### 前端源码二次构建（可选）

如果你修改了 `ui/src/main.ts` 或 `ui/build.mjs`：

```bash
cd ~/.gemini/config/plugins/context-card/ui
npm install
npm run build
```

### 隐私说明

- 所有上下文统计、CLI 缓存、tmux 窗格建议状态（`panes/*.json`）与习惯学习数据库均 **100% 本地存储**在 `${PLUGIN_DATA}` 目录，敏感文件均采用 `0600` 权限。
- **严格显式授权联网**：默认状态下插件不发起任何遥测或外部请求。仅当你主动配置启用建议回复鉴权且开启 `Auto` 时，才会将近期对话摘要发送至你所选的模型端点。

## License

[MIT](./LICENSE)
