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
- **Optional BYOK Suggested Replies + Local Style Learning**:
  - Works **100% offline with zero API keys** for all context monitoring features.
  - Optionally connect your own **Gemini API Key** (Google AI Studio free tier works great) or any **OpenAI-compatible endpoint** (Ollama, OpenRouter, DeepSeek, OpenAI) to get one-click next-reply drafts that locally learn your phrasing and brevity over time.

### Quick Install

1. **Ensure Python 3.10+ has the `mcp` SDK installed**:
   ```bash
   pip install "mcp>=1.2.0"
   ```
2. **Clone directly into Antigravity's plugins directory** (`dist/app.html` is pre-built—no Node.js build step required):
   ```bash
   mkdir -p ~/.gemini/config/plugins
   git clone https://github.com/Shawn1565/antigravity-context-card.git ~/.gemini/config/plugins/context-card
   ```
3. Antigravity discovers plugins automatically. Start a new chat (or click `+ -> Context` in the right side panel) and Context Card will appear!

### Optional: Configure Suggested Replies (BYOK)

Context monitoring requires **no API key**. To enable smart next-reply suggestions, choose any one of the following:

- **Option A — Paste directly in the UI**: Click the password box in the `Suggested reply` card and hit **Save**. Your key is stored locally with `0600` permissions at `~/.gemini/antigravity/plugin_data/context-card/llm_config.json` (never inside the plugin folder).
- **Option B — Environment variable** (in `~/.zshrc` / `~/.bashrc`):
  ```bash
  export GEMINI_API_KEY="AIza..."
  # Optional custom model (default: gemini-2.5-flash)
  export CONTEXT_CARD_SUGGEST_MODEL="gemini-2.5-flash"
  ```
- **Option C — OpenAI-compatible / Local Ollama**:
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
- No telemetry or external network requests are made unless you explicitly opt into Suggested Replies with your own LLM endpoint/key.

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
  - **手动打开排除法防错绑**：手动从右侧栏 `+ -> Context` 打开时自动排除其它已挂载活跃面板的窗口，并支持顶部一键切换会话。
- **快捷交互与文件直达**：
  - 面板内双击确认即可发送 `/compact` 压缩上下文。
  - 点击面板中的规则文件、已加载 Skill 或会话产物（Outputs），直接在侧边栏预览对应文件。
- **可选的 BYOK 智能建议回复 + 本地习惯学习**：
  - **上下文监控主功能 100% 离线运行，无需任何 API Key**。
  - 可选接入你自己的 **Gemini API Key**（Google AI Studio 免费额度即可流畅使用）或任意 **OpenAI 兼容接口**（本地 Ollama、OpenRouter、DeepSeek、OpenAI），根据你的历史回复自动学习你的表达风格与简短习惯，提供一键发送/改写的下一句建议。

### 快速安装

1. **确保本机 Python 3.10+ 已安装 `mcp` SDK**：
   ```bash
   pip install "mcp>=1.2.0"
   ```
2. **直接克隆到 Antigravity 插件目录**（仓库已内置预编译好的 `dist/app.html`，无需安装 Node.js 编译，开箱即用）：
   ```bash
   mkdir -p ~/.gemini/config/plugins
   git clone https://github.com/Shawn1565/antigravity-context-card.git ~/.gemini/config/plugins/context-card
   ```
3. Antigravity 会自动发现并加载插件。新建对话（或点击右侧边栏 `+ -> Context`）即可使用！

### 可选配置：智能建议回复（BYOK 自带密钥）

如果不配置 Key，所有上下文监控功能完全正常可用。如需开启底部的 `Suggested reply`，任选以下一种方式即可：

- **方式一：在面板内直接填写（最简单）**
  在面板底部 `Suggested reply` 区域的输入框粘贴你的 Gemini API Key 并点击 **Save**。密钥会以 `0600` 仅本人可读权限保存在插件目录之外的 `~/.gemini/antigravity/plugin_data/context-card/llm_config.json`，绝不会混入代码目录。
- **方式二：环境变量配置**（写入 `~/.zshrc` 或 `~/.bashrc`）
  ```bash
  export GEMINI_API_KEY="AIza..."
  # 可选：自定义模型（默认 gemini-2.5-flash）
  export CONTEXT_CARD_SUGGEST_MODEL="gemini-2.5-flash"
  ```
- **方式三：接入本地 Ollama 或任意 OpenAI 兼容接口**
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
- 除非你主动配置了自己的 API Key 开启“建议回复”，否则插件不会发起任何外部网络请求。

## License

[MIT](./LICENSE)
