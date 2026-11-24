import esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.resolve(__dirname, '../dist');
fs.mkdirSync(outDir, { recursive: true });

const built = await esbuild.build({
  entryPoints: [path.resolve(__dirname, 'src/main.ts')],
  bundle: true,
  format: 'iife',
  target: ['es2022'],
  minify: true,
  write: false,
});

const jsCode = built.outputFiles[0].text.replace(/<\/script>/gi, '<\\/script>');

const css = `
:root {
  --bg: var(--color-background-primary, #19191b);
  --bg-elev: var(--color-background-secondary, #232327);
  --bg-hover: var(--color-background-tertiary, rgba(255, 255, 255, 0.06));
  --fg: var(--color-text-primary, #ececee);
  --fg-muted: var(--color-text-secondary, #a1a1aa);
  --fg-dim: var(--color-text-tertiary, #71717a);
  --border: var(--color-border-primary, rgba(255, 255, 255, 0.09));
  --ctx-free: #323238;
  --font: var(--font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif);
}

:root[data-theme="light"] {
  --bg: var(--color-background-primary, #ffffff);
  --bg-elev: var(--color-background-secondary, #f4f4f5);
  --bg-hover: var(--color-background-tertiary, rgba(0, 0, 0, 0.05));
  --fg: var(--color-text-primary, #18181b);
  --fg-muted: var(--color-text-secondary, #52525b);
  --fg-dim: var(--color-text-tertiary, #71717a);
  --border: var(--color-border-primary, rgba(0, 0, 0, 0.09));
  --ctx-free: #e4e4e7;
}

* {
  box-sizing: border-box;
}

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font);
  font-size: 13px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}

.panel {
  padding: 12px 14px 20px;
  max-width: 520px;
  margin: 0 auto;
  position: relative;
}

.empty-state {
  padding: 28px 16px;
  color: var(--fg-muted);
  text-align: center;
  font-size: 13px;
}

.ghost-btn {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: var(--fg-dim);
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}

.ghost-btn:hover {
  background: var(--bg-hover);
  color: var(--fg);
}

.ico-xs {
  width: 12px;
  height: 12px;
}

.sec {
  margin: 2px 0;
}

.sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.sec-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--fg-muted);
  margin-bottom: 10px;
  letter-spacing: 0.01em;
}

.sec-head .sec-title {
  margin-bottom: 0;
}

.live-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--fg-dim);
  font-variant-numeric: tabular-nums;
}

.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fg-dim);
}

.live-dot.on {
  background: #10b981;
  box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.22);
}

.divider {
  height: 1px;
  background: var(--border);
  margin: 14px 0;
}

.env-list {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.env-row {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.env-text {
  color: var(--fg);
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.env-sub {
  color: var(--fg-dim);
  font-size: 12px;
  flex-shrink: 0;
}

.ico {
  width: 15px;
  height: 15px;
  color: var(--fg-muted);
  flex-shrink: 0;
}

.ico-sm {
  width: 13px;
  height: 13px;
}

.ctx-summary {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 8px;
}

.ctx-pct {
  font-size: 14px;
  font-weight: 600;
  color: var(--fg);
  font-variant-numeric: tabular-nums;
}

.ctx-pct.pct-warn {
  color: #f59e0b;
}

.ctx-pct.pct-danger {
  color: #ef4444;
}

.ctx-ratio {
  font-size: 12.5px;
  color: var(--fg-muted);
  font-variant-numeric: tabular-nums;
}

.bar-track {
  display: flex;
  align-items: stretch;
  gap: 1.5px;
  height: 6px;
  border-radius: 999px;
  background: var(--ctx-free);
  overflow: hidden;
  margin-bottom: 13px;
}

.bar-seg {
  height: 100%;
  min-width: 2px;
  transition: width 0.25s ease;
}

.bar-seg:first-child {
  border-top-left-radius: 999px;
  border-bottom-left-radius: 999px;
}

.bar-seg:last-child {
  border-top-right-radius: 999px;
  border-bottom-right-radius: 999px;
}

.cat-list {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.cat-row {
  display: grid;
  grid-template-columns: 10px 1fr auto 46px;
  align-items: center;
  gap: 9px;
  font-size: 12.5px;
}

.cat-dot {
  width: 9px;
  height: 9px;
  border-radius: 2.5px;
  display: inline-block;
}

.cat-label {
  color: var(--fg-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cat-label.dotted {
  text-decoration: underline dotted var(--fg-dim);
  text-underline-offset: 3px;
  cursor: help;
}

.cat-val {
  color: var(--fg-muted);
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.cat-pct {
  color: var(--fg);
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.acc-wrap {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.acc-btn {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 5px 4px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--fg-muted);
  font-family: var(--font);
  font-size: 12.5px;
  cursor: pointer;
  text-align: left;
  transition: background 0.12s, color 0.12s;
}

.acc-btn:hover {
  background: var(--bg-hover);
  color: var(--fg);
}

.chev {
  width: 13px;
  height: 13px;
  color: var(--fg-dim);
  transition: transform 0.14s ease;
  flex-shrink: 0;
}

.acc-btn.open .chev {
  transform: rotate(90deg);
}

.acc-title {
  flex: 1;
}

.acc-count {
  color: var(--fg-dim);
  font-variant-numeric: tabular-nums;
}

.acc-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-left: 14px;
  margin-bottom: 4px;
}

.acc-empty {
  color: var(--fg-dim);
  font-size: 12px;
  padding: 3px 6px;
}

.file-row, .out-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 5px 6px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--fg);
  font-family: var(--font);
  font-size: 12.5px;
  cursor: pointer;
  text-align: left;
  transition: background 0.12s;
}

.file-row:hover, .out-row:hover {
  background: var(--bg-hover);
}

.file-name, .out-name {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-meta {
  color: var(--fg-dim);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}

.out-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.muted-note {
  color: var(--fg-dim);
  font-size: 12px;
  padding: 2px 0;
}

.meta-bar {
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.meta-stats {
  font-size: 11.5px;
  color: var(--fg-dim);
  font-variant-numeric: tabular-nums;
}

.meta-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.chip-btn {
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--fg-muted);
  font-family: var(--font);
  font-size: 11px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}

.chip-btn:hover {
  background: var(--bg-hover);
  color: var(--fg);
}

.chip-btn.active {
  color: #10b981;
  border-color: rgba(16, 185, 129, 0.35);
}

.chip-btn.danger {
  color: #f59e0b;
  border-color: rgba(245, 158, 11, 0.5);
}

.toast {
  position: fixed;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  padding: 5px 12px;
  border-radius: 999px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  color: var(--fg);
  font-size: 11.5px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
  pointer-events: none;
  z-index: 20;
}

/* Suggested reply section */
.suggest-head-left {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.suggest-head-right {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.suggest-sparkle {
  color: #a855f7;
  width: 13px;
  height: 13px;
  flex-shrink: 0;
}

.suggest-meta {
  font-size: 10.5px;
  color: var(--fg-dim);
  font-variant-numeric: tabular-nums;
}

.suggest-box {
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-elev);
  padding: 8px 9px 7px;
  transition: border-color 0.14s ease, opacity 0.14s ease;
}

.suggest-box:focus-within {
  border-color: rgba(168, 85, 247, 0.55);
}

.suggest-box.dimmed {
  opacity: 0.65;
}

.suggest-input {
  width: 100%;
  min-height: 44px;
  max-height: 140px;
  border: none;
  outline: none;
  resize: none;
  background: transparent;
  color: var(--fg);
  font-family: var(--font);
  font-size: 12.5px;
  line-height: 1.45;
  padding: 0;
  margin: 0;
  overflow-y: auto;
}

.suggest-input::placeholder {
  color: var(--fg-dim);
}

.suggest-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
}

.suggest-hint {
  font-size: 10.5px;
  color: var(--fg-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.suggest-btns {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
}

.send-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 10px;
  border-radius: 6px;
  border: none;
  background: #7c3aed;
  color: #ffffff;
  font-family: var(--font);
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.12s, opacity 0.12s;
}

.send-btn:hover:not(:disabled) {
  background: #6d28d9;
}

.send-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.copy-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 24px;
  padding: 0 7px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--fg-muted);
  font-family: var(--font);
  font-size: 11px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}

.copy-btn:hover {
  background: var(--bg-hover);
  color: var(--fg);
}

.alt-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 8px;
}

.alt-row {
  display: flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: transparent;
  transition: background 0.12s, border-color 0.12s;
}

.alt-row:hover {
  background: var(--bg-hover);
  border-color: rgba(168, 85, 247, 0.35);
}

.alt-pick {
  flex: 1;
  min-width: 0;
  padding: 6px 8px;
  border: none;
  background: transparent;
  color: var(--fg-muted);
  font-family: var(--font);
  font-size: 12px;
  line-height: 1.35;
  text-align: left;
  cursor: pointer;
  word-break: break-word;
}

.alt-row:hover .alt-pick {
  color: var(--fg);
}

.alt-send {
  width: 26px;
  height: 26px;
  margin-right: 3px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--fg-dim);
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.12s, color 0.12s;
}

.alt-send:hover {
  background: rgba(124, 58, 237, 0.18);
  color: #a855f7;
}

.suggest-status-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px dashed var(--border);
  color: var(--fg-dim);
  font-size: 12px;
}

.byok-box {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px dashed var(--border);
  background: var(--bg-elev);
}

.byok-note {
  color: var(--fg-dim);
  font-size: 11.5px;
  line-height: 1.4;
}

.byok-note code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px;
  color: var(--fg-muted);
}

.byok-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.byok-input {
  flex: 1;
  min-width: 0;
  height: 24px;
  padding: 0 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--fg);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px;
  outline: none;
}

.byok-input:focus {
  border-color: rgba(168, 85, 247, 0.55);
}

.suggest-skel {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
}

.skel-line {
  height: 10px;
  border-radius: 999px;
  background: var(--bg-hover);
  animation: ctxPulse 1.3s ease-in-out infinite;
}

.skel-line.w85 { width: 85%; }
.skel-line.w55 { width: 55%; }

@keyframes ctxPulse {
  0%, 100% { opacity: 0.45; }
  50% { opacity: 0.95; }
}

@keyframes ctxSpin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.spinning {
  animation: ctxSpin 0.9s linear infinite;
}

.session-row {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  margin-top: 2px;
  padding: 4px 6px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--fg-muted);
  font-family: var(--font);
  font-size: 11.5px;
  cursor: pointer;
  text-align: left;
  transition: background 0.12s, color 0.12s;
}

.session-row:hover {
  background: var(--bg-hover);
  color: var(--fg);
}

.session-row.open .chev {
  transform: rotate(90deg);
}

.session-title, .session-opt-title {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px;
  color: var(--fg-dim);
  flex-shrink: 0;
}

.session-auto-badge {
  padding: 0 5px;
  height: 16px;
  line-height: 16px;
  border-radius: 999px;
  border: 1px solid rgba(245, 158, 11, 0.4);
  background: rgba(245, 158, 11, 0.12);
  color: #f59e0b;
  font-size: 10px;
  font-weight: 500;
  flex-shrink: 0;
}

.session-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 4px;
  padding: 4px;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  max-height: 170px;
  overflow-y: auto;
}

.session-opt {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 5px 6px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--fg-muted);
  font-family: var(--font);
  font-size: 11.5px;
  cursor: pointer;
  text-align: left;
  transition: background 0.12s, color 0.12s;
}

.session-opt:hover {
  background: var(--bg-hover);
  color: var(--fg);
}

.session-opt.active {
  background: rgba(124, 58, 237, 0.16);
  color: var(--fg);
}

.chip-btn.habit-on {
  color: #c084fc;
  border-color: rgba(168, 85, 247, 0.45);
}

.habits-drawer {
  margin-top: 9px;
  padding: 9px 10px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.habits-stats {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
  color: var(--fg-dim);
  font-variant-numeric: tabular-nums;
}

.habits-model {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px;
  color: var(--fg-muted);
}

.habits-profile {
  font-size: 12px;
  line-height: 1.5;
  color: var(--fg);
  white-space: pre-wrap;
}

.habit-phrases {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.habit-pill {
  padding: 2px 7px;
  border-radius: 999px;
  border: 1px solid rgba(168, 85, 247, 0.3);
  background: rgba(168, 85, 247, 0.08);
  color: var(--fg);
  font-size: 11px;
}

.habit-fb-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
}

.habit-fb-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  min-width: 0;
}

.fb-tag {
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 10px;
  flex-shrink: 0;
}

.fb-tag.fb-edit {
  background: rgba(168, 85, 247, 0.18);
  color: #c084fc;
}

.fb-tag.fb-ok {
  background: rgba(16, 185, 129, 0.16);
  color: #34d399;
}

.fb-tag.fb-ov {
  background: rgba(245, 158, 11, 0.16);
  color: #fbbf24;
}

.fb-text {
  color: var(--fg-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.habits-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  padding-top: 4px;
}
`;

const html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Context Card</title>
  <style>${css}</style>
</head>
<body>
  <div id="app"></div>
  <script>${jsCode}</script>
</body>
</html>
`;

const outFile = path.join(outDir, 'app.html');
fs.writeFileSync(outFile, html, 'utf8');
console.log(`Built ${outFile} (${(Buffer.byteLength(html, 'utf8') / 1024).toFixed(1)} KB)`);
