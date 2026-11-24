# Antigravity Context Card

[English](#english) | [中文说明](#中文说明)

---

<a id="english"></a>

## English

A live, Claude Desktop-inspired **Context Window Monitor & Smart Reply** side-pane plugin (MCP App) for **Google Antigravity** (Desktop 2.0, IDE, and CLI).

Unlike generic context meters that lump everything into `Messages`, **Context Card** inspects Antigravity's local conversation trajectory in real time and separates **pure dialogue (`Messages`)**, **model reasoning (`Thinking`)**, and **file/command payloads (`Tool calls & outputs`)**—so you always know *what* is consuming your context window and *when* to `/compact` or delegate to subagents.

### Features

- **Granular Token Breakdown**: Live progress bar and per-category token & percentage metrics:
  - `Messages` (user prompts & visible assistant replies)
  - `Thinking` (chain-of-thought / reasoning tokens retained in context)
  - `Tool calls & outputs` (file reads, terminal outputs, search results)
  - `System tools`, `Memory files` (`GEMINI.md` / `AGENTS.md` / `CLAUDE.md`), `Skills`, `System prompt`, `Checkpoint summary`, and `Free space`
- **Prompt Cache Telemetry**: Real-time prompt cache hit ratio (`Cache %`) and active step / checkpoint counters.
- **Reliable Multi-Window Binding**:
  - Auto-opens and locks to each window's exact `conversation_id` via a lightweight rule + `PreInvocation` lifecycle hook (even on resumed historical chats or large workspaces).
  - Subagent-safe: ignores child/subagent runs so background workers never hijack your main window's panel.
  - Active-pane exclusion when opened manually from `+ -> Context`, plus a one-click session switcher.
- **Quick Actions & File Preview**:
  - Two-click `/compact` trigger directly from the panel.
  - Clickable **Memory files**, **Loaded skills**, and conversation **Outputs** (artifacts) that open directly in Antigravity's side-pane file viewer.
- **One-Click Suggested Replies (Gemini Subscription + BYOK)**:
  - Works **100% offline** for all context monitoring features—zero network calls until you opt in.
  - **One-Click Gemini Subscription Auth**: Click **Use Subscription (agy)** once in the panel to reuse your local Antigravity CLI (`agy`) Google subscription OAuth—no API key required!
  - **Optional BYOK (~1s ultra-low latency)**: Also supports your own **Gemini API Key** (Google AI Studio free tier works great) or any **OpenAI-compatible endpoint** (Ollama, OpenRouter, DeepSeek, OpenAI), with one-click switching or clearing inside the `Habits` drawer.

### Quick Install

1. **Ensure `python3` (3.10+) is on your `PATH` and has the `mcp` SDK installed** (on Windows, ensure `python3` resolves via App Execution Aliases or `PATH`):
   ```bash
   pip install "mcp>=1.2.0"
   ```
2. **Clone directly into Antigravity's plugins directory** (`dist/app.html` is pre-built—no Node.js build step required):
   ```bash
   mkdir -p ~/.gemini/config/plugins
   git clone https://github.com/Shawn1565/antigravity-context-card.git ~/.gemini/config/plugins/context-card
   ```
3. Antigravity discovers plugins automatically. Start a new chat (or click `+ -> Context` in the right side panel) and Context Card will appear!

### Suggested Replies Auth (Gemini Subscription or BYOK)

Context monitoring requires **no authentication or network**. For smart next-reply suggestions, **4 explicit opt-in modes** are supported (switchable anytime inside `Habits` in the UI):

- **Option A — Gemini Subscription via `agy` CLI (1-Click, Zero API Key)**:
  If you have the official Antigravity CLI (`agy`) installed and signed in (`agy` in terminal once), click **Use Subscription (agy)** once in the panel (or set `export CONTEXT_CARD_PROVIDER="agy"`). Context Card then uses your existing Gemini subscription (`gemini-3.8-flash-low`) inside a sandboxed headless call with **zero API keys**.
- **Option B — Paste a Gemini API Key in the UI (~1s fastest response)**:
  Open `Habits` -> `Set Key` (or paste in the setup card) and click **Save** (click **Clear** anytime to remove saved auth). Stored locally with `0600` permissions at `~/.gemini/antigravity/plugin_data/context-card/llm_config.json` (never inside the plugin folder).
- **Option C — Environment variable** (in the environment launching Antigravity):
  ```bash
  export GEMINI_API_KEY="AIza..."
  # Or explicitly select subscription mode via env:
  # export CONTEXT_CARD_PROVIDER="agy"
  ```
- **Option D — OpenAI-compatible / Local Ollama**:
  ```bash
  export CONTEXT_CARD_OPENAI_BASE_URL="http://localhost:11434/v1"
  export CONTEXT_CARD_SUGGEST_MODEL="qwen2.5:7b"
  # Set CONTEXT_CARD_OPENAI_API_KEY="sk-..." for cloud providers (OpenRouter, DeepSeek, OpenAI)
  ```

Don't want reply suggestions? Click the **`Auto` / `Off`** toggle in the `Suggested reply` header anytime.

### Building the UI from Source (Optional)

If you modify `ui/src/main.ts` or `ui/build.mjs`:

```bash
cd ~/.gemini/config/plugins/context-card/ui
npm install
npm run build
```

### Privacy & Security

- All conversation parsing and habit SQLite storage (`habits.db`, `runtime_state.db`) stay **100% local** on your machine under Antigravity's `${PLUGIN_DATA}` directory (`~/.gemini/antigravity/plugin_data/context-card/`).
- **Strict Opt-In Network Policy**: No telemetry or external LLM requests are made by default. Recent dialogue excerpts and style samples are sent to your chosen model **only after** you explicitly enable Suggested Replies (by clicking **Use Subscription (agy)**, saving an API key in the UI, or setting `CONTEXT_CARD_*` / `GEMINI_API_KEY` environment variables) and while `Auto` is enabled.

---

<a id="中文说明"></a>

## 中文说明

专为 **Google Antigravity**（支持 Desktop 2.0、IDE 与 CLI）打造的 Claude Desktop 风格**上下文窗口实时监控与智能回复插件（MCP App）**。

不同于把所有对话和工具输出全部混入 `Messages` 的笼统统计方式，**Context Card** 实时解析本地会话轨迹，将 **`Messages`（纯对话）**、**`Thinking`（模型推理链）** 与 **`Tool calls & outputs`（文件读取与终端输出）** 清晰拆分——让你一眼看清到底是大文件/长日志吃满了窗口，还是对话轮次过多需要 `/compact` 或派发子代理。

### 核心特性

- **精细化上下文分项统计**：
  - 独立展示 `Messages`、`Thinking`、`Tool calls & outputs`、`System tools`、`Memory files`（`GEMINI.md` / `AGENTS.md` / `CLAUDE.md`）、`Skills`、`System prompt`、`Checkpoint summary`（压缩摘要）与 `Free space`。
  - 实时显示 **Prompt Cache 命中率（`Cache %`）**、活跃步数（`Steps`）及 `/compact` 检查点次数。
- **可靠的多窗口自动绑定**：
  - 结合轻量规则与 `PreInvocation` 生命周期钩子，在新窗口及重启后续聊的历史窗口自动打开并精确绑定当前 `conversation_id`。
  - **子代理安全隔离**：自动识别并忽略 `self` / `research` 等子代理会话，防止后台子任务抢绑主窗口面板。
  - **手动打开排除法防错绑**：手动从右侧栏 `+ -> Context` 打开时自动排除其它已挂载活跃面板的窗口，支持顶部切换查看历史会话（跨会话查看时自动切换为只读复制模式，防止误发消息）。
- **快捷交互与文件直达**：
  - 面板内双击确认即可向当前绑定会话发送 `/compact` 压缩上下文。
  - 点击面板中的规则文件、已加载 Skill 或会话产物（Outputs），直接在侧边栏预览对应文件。
- **一键开启智能建议回复（支持 Gemini 订阅鉴权 + BYOK）**：
  - **上下文监控主功能 100% 离线运行**，未开启鉴权前零外部网络请求。
  - **一键复用 Gemini 订阅（免 API Key）**：若本机已安装并登录官方 `agy`（Antigravity CLI），只需在面板点击一次 **Use Subscription (agy)** 显式授权，即可复用你的 Gemini 订阅 OAuth 生成建议回复，零密钥即可使用！
  - **支持一键切换或清除 API Key / 本地模型**：也可在面板 `Habits` 抽屉内一键切换为自己的 **Gemini API Key**（直连响应约 1 秒）或任意 **OpenAI 兼容接口**（本地 Ollama、OpenRouter、DeepSeek、OpenAI），并在本地自动学习你的表达习惯。

### 快速安装

1. **确保本机 `python3`（3.10+）已在 `PATH` 中且安装了 `mcp` SDK**（Windows 环境请确保 `python3` 在 `PATH` 或系统应用执行别名中可用）：
   ```bash
   pip install "mcp>=1.2.0"
   ```
2. **直接克隆到 Antigravity 插件目录**（仓库已内置预编译好的 `dist/app.html`，无需安装 Node.js 编译，开箱即用）：
   ```bash
   mkdir -p ~/.gemini/config/plugins
   git clone https://github.com/Shawn1565/antigravity-context-card.git ~/.gemini/config/plugins/context-card
   ```
3. Antigravity 会自动发现并加载插件。新建对话（或点击右侧边栏 `+ -> Context`）即可使用！

### 智能建议回复鉴权配置（Gemini 订阅或自带密钥）

上下文监控无需任何网络或鉴权。底部的 `Suggested reply` 支持以下 **4 种显式接入方式**（可在面板 `Habits` 抽屉内随时一键切换 `Subscription (agy)`、`API Key` 或点击 `Clear` 清除保存的鉴权）：

- **方式一：一键复用 Gemini 订阅（免 API Key）**
  只要本机已安装并登录 Antigravity 官方 CLI（终端执行过一次 `agy` 登录），在面板首次点击 **Use Subscription (agy)**（或配置环境变量 `CONTEXT_CARD_PROVIDER="agy"`），插件即会在受限沙箱下通过 `agy` 订阅通道调用 `gemini-3.8-flash-low`，**无需申请任何 API Key**。
- **方式二：在面板内填写 Gemini API Key（约 1 秒极速直连）**
  展开 `Habits` 点击 **Set Key**（或在首次提示框粘贴）并保存。密钥以 `0600` 权限保存在仓库外的 `~/.gemini/antigravity/plugin_data/context-card/llm_config.json`，绝不混入代码目录。
- **方式三：环境变量配置**（在启动 Antigravity 的进程环境中设置）
  ```bash
  export GEMINI_API_KEY="AIza..."
  # 或通过环境变量显式指定走订阅通道：
  # export CONTEXT_CARD_PROVIDER="agy"
  ```
- **方式四：接入本地 Ollama 或任意 OpenAI 兼容接口**
  ```bash
  export CONTEXT_CARD_OPENAI_BASE_URL="http://localhost:11434/v1"
  export CONTEXT_CARD_SUGGEST_MODEL="qwen2.5:7b"
  # 若使用云端兼容服务（OpenRouter / DeepSeek / OpenAI），额外设置：
  # export CONTEXT_CARD_OPENAI_API_KEY="sk-..."
  ```

如果不需要建议回复功能，随时点击 `Suggested reply` 右上角的 **`Auto` / `Off`** 按钮即可关闭。

### 前端源码二次构建（可选）

如果你修改了 `ui/src/main.ts` 或 `ui/build.mjs`：

```bash
cd ~/.gemini/config/plugins/context-card/ui
npm install
npm run build
```

### 隐私说明

- 所有上下文统计、会话状态与习惯学习数据库均 **100% 本地存储**在 `${PLUGIN_DATA}` 目录（`~/.gemini/antigravity/plugin_data/context-card/`），与插件代码仓完全隔离。
- **严格显式授权联网**：默认状态下插件不发起任何遥测或外部请求。仅当你主动点击启用 `Use Subscription (agy)`、保存了 API Key 或显式注入相关环境变量且开启 `Auto` 时，才会将近期对话摘要与风格样本发送至你所选的模型端点用于生成回复建议与习惯提炼。

## License

[MIT](./LICENSE)
