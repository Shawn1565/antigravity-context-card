import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';
import { z } from 'zod';

interface CategoryItem {
  key: string;
  label: string;
  tokens: number;
  pct: number;
  color: string;
  hint?: string;
  is_free?: boolean;
}

interface FileEntry {
  name: string;
  path: string;
  uri: string;
  bytes?: number;
  tokens?: number;
  ext?: string;
  updated_at?: string;
}

interface SuggestionData {
  status: 'idle' | 'working' | 'generating' | 'ready' | 'disabled' | 'needs_key' | 'error';
  turn_key: string;
  primary: string;
  alternatives: string[];
  auto_suggest: boolean;
  model?: string;
  error?: string | null;
  latency_ms?: number | null;
}

interface HabitFeedbackItem {
  outcome: 'accepted' | 'edited' | 'overridden';
  predicted: string;
  actual: string;
  created_at: string;
}

interface HabitsData {
  total_samples: number;
  history_samples: number;
  accepted_count: number;
  edited_count: number;
  overridden_count: number;
  accept_rate_pct: number | null;
  distilling: boolean;
  auth_mode?: 'agy' | 'gemini' | 'openai' | 'none';
  active_model?: string;
  agy_available?: boolean;
  has_saved_api_key?: boolean;
  profile_summary: string;
  frequent_phrases: string[];
  profile_updated_at: string;
  recent_feedback: HabitFeedbackItem[];
}

interface ContextSnapshot {
  empty: boolean;
  conversation_id?: string;
  bind_mode?: 'explicit' | 'auto';
  title?: string;
  environment?: {
    host: string;
    workspace_name: string;
    workspace_path: string | null;
    workspace_display: string;
    branch: string | null;
  };
  model_id?: string | null;
  model_display?: string;
  used_tokens?: number;
  active_input_tokens?: number;
  limit_tokens?: number;
  native_limit_tokens?: number;
  used_pct?: number;
  free_tokens?: number;
  cache_read_tokens?: number;
  cache_hit_pct?: number;
  checkpoint_count?: number;
  step_count?: number;
  active_step_count?: number;
  categories?: CategoryItem[];
  memory_files?: FileEntry[];
  loaded_skills?: FileEntry[];
  total_skills_count?: number;
  outputs?: FileEntry[];
  last_event_at?: string | null;
  refreshed_at?: string;
  suggestion?: SuggestionData;
  habits?: HabitsData;
}

interface ConversationOption {
  conversation_id: string;
  short_id: string;
  title: string;
  updated_at: string;
}

const ToggleAuxPaneResultSchema = z
  .object({ isError: z.boolean().optional() })
  .passthrough();

const app = new App({ name: 'Context Card', version: '1.1.0' }, {});
const clientId = `${Math.random().toString(36).slice(2, 10)}-${Date.now().toString(36)}`;

let currentSnapshot: ContextSnapshot | null = null;
// Authoritative conversation ID belonging to the host window (when known)
let hostConversationId: string | null = null;
// Initial conversation ID matched in Auto mode before any picker switch
let initialAutoConversationId: string | null = null;
// Strictly locked per-window conversation ID (or user-selected session in picker)
let lockedConversationId: string | null = null;
let lockedBindMode: 'explicit' | 'auto' = 'explicit';
let switchedViaPicker = false;
let autoRefresh = true;
let pollInFlight = false;
let pollTimer: number | null = null;
let fastPollTimer: number | null = null;
let memoryExpanded = false;
let skillsExpanded = false;
let outputsExpanded = false;
let sessionPickerOpen = false;
let recentConversations: ConversationOption[] = [];
let canSendMessage = false;
let toastMessage: string | null = null;
let toastTimer: number | null = null;

// Suggested reply + Habits UI state (isolated from 3s context re-renders)
let draftText = '';
let draftTurnKey = '';
let draftDirty = false;
let draftPrimarySource = '';
let sendingSuggestion = false;
let sentTurnKey = '';
let habitsExpanded = false;
let showDrawerKeyInput = false;
let resetHabitsConfirmUntil = 0;
let lastSuggestRenderSig = '';

function esc(s: unknown): string {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function getReturnHostConversationId(): string | null {
  return hostConversationId || initialAutoConversationId;
}

function canActOnCurrentSession(): boolean {
  if (!canSendMessage) return false;
  if (hostConversationId) {
    return Boolean(lockedConversationId && lockedConversationId === hostConversationId);
  }
  return !switchedViaPicker;
}

function fmtTokens(n: number | undefined): string {
  const v = Math.max(0, Math.round(n ?? 0));
  if (v >= 1_000_000) {
    const m = (v / 1_000_000).toFixed(1).replace(/\.0$/, '');
    return `${m}M`;
  }
  if (v >= 1_000) {
    const k = (v / 1_000).toFixed(1).replace(/\.0$/, '');
    return `${k}K`;
  }
  return String(v);
}

function fmtPct(pct: number | undefined): string {
  const v = pct ?? 0;
  return `${v.toFixed(1)}%`;
}

function fmtBytes(b: number | undefined): string {
  const v = b ?? 0;
  if (v >= 1024 * 1024) return `${(v / (1024 * 1024)).toFixed(1)} MB`;
  if (v >= 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${v} B`;
}

function renderToast() {
  const el = document.getElementById('toast-root');
  if (!el) return;
  el.innerHTML = toastMessage ? `<div class="toast">${esc(toastMessage)}</div>` : '';
}

function showToast(msg: string) {
  toastMessage = msg;
  renderToast();
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toastMessage = null;
    renderToast();
  }, 2400);
}

function extractHostConversationId(ctx: unknown): string | null {
  if (!ctx || typeof ctx !== 'object') return null;
  const obj = ctx as Record<string, unknown>;
  for (const key of [
    'conversationId',
    'conversation_id',
    'trajectoryId',
    'trajectory_id',
    'cascadeId',
    'sessionId',
  ]) {
    const val = obj[key];
    if (typeof val === 'string' && val.trim()) return val.trim();
  }
  return null;
}

function iconSvg(kind: string, extraCls = ''): string {
  const cls = (base: string) => (extraCls ? `${base} ${extraCls}` : base);
  switch (kind) {
    case 'monitor':
      return `<svg class="${cls('ico')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><rect x="1.75" y="2.5" width="12.5" height="8.5" rx="1.5"/><path d="M5.5 13.5h5M8 11v2.5"/></svg>`;
    case 'folder':
      return `<svg class="${cls('ico')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><path d="M1.75 4.25A1.25 1.25 0 0 1 3 3h3l1.4 1.5H13A1.25 1.25 0 0 1 14.25 5.75v6A1.25 1.25 0 0 1 13 13H3a1.25 1.25 0 0 1-1.25-1.25v-7.5Z"/></svg>`;
    case 'branch':
      return `<svg class="${cls('ico')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><circle cx="4.5" cy="3.5" r="1.5"/><circle cx="4.5" cy="12.5" r="1.5"/><circle cx="11.5" cy="6" r="1.5"/><path d="M4.5 5v6M11.5 7.5c0 2-1.5 3-7 3"/></svg>`;
    case 'model':
      return `<svg class="${cls('ico')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.75 9.8 6.2 14.25 8 9.8 9.8 8 14.25 6.2 9.8 1.75 8 6.2 6.2 8 1.75Z"/></svg>`;
    case 'sparkle':
      return `<svg class="${cls('suggest-sparkle')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.45" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.75 9.55 6.05 13.85 7.6 9.55 9.15 8 13.45 6.45 9.15 2.15 7.6 6.45 6.05 8 1.75Z"/></svg>`;
    case 'send':
      return `<svg class="${cls('ico-xs')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 8h10.5M8.75 3.75 13 8l-4.25 4.25"/></svg>`;
    case 'chevron':
      return `<svg class="${cls('chev')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l4 4-4 4"/></svg>`;
    case 'globe':
      return `<svg class="${cls('ico')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2a9.5 9.5 0 0 1 0 12M8 2a9.5 9.5 0 0 0 0 12"/></svg>`;
    case 'doc':
      return `<svg class="${cls('ico')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 1.75h5.25L12.5 5v9.25H4a1 1 0 0 1-1-1V2.75a1 1 0 0 1 1-1Z"/><path d="M9.25 1.75V5h3.25M5.5 8h5M5.5 10.5h3.5"/></svg>`;
    case 'image':
      return `<svg class="${cls('ico')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2.5" width="12" height="11" rx="1.5"/><circle cx="5.75" cy="6" r="1"/><path d="m2.5 11.5 3.25-3 2.5 2.25 2-1.75 3.25 2.5"/></svg>`;
    case 'refresh':
      return `<svg class="${cls('ico-xs')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 8a5.5 5.5 0 1 1-1.35-3.6"/><path d="M13.5 2.75v2.75h-2.75"/></svg>`;
    case 'settings':
      return `<svg class="${cls('ico-sm')}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>`;
    default:
      return `<svg class="${cls('ico')}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 1.75h5.25L12.5 5v9.25H4a1 1 0 0 1-1-1V2.75a1 1 0 0 1 1-1Z"/><path d="M9.25 1.75V5h3.25"/></svg>`;
  }
}

function outputIconKind(ext: string | undefined): string {
  const e = (ext || '').toLowerCase();
  if (e === 'html' || e === 'htm') return 'globe';
  if (['png', 'jpg', 'jpeg', 'webp', 'svg', 'gif'].includes(e)) return 'image';
  return 'doc';
}

async function openFileInSidePane(uri: string, name: string) {
  if (!uri) return;
  try {
    const res = await app.request(
      {
        method: 'com.google.antigravity/toggle-aux-pane',
        params: { tabType: 'fileView', uri },
      },
      ToggleAuxPaneResultSchema
    );
    if (res?.isError) {
      showToast(`Unable to open ${name} in this view`);
    }
  } catch {
    showToast(`File preview unavailable here`);
  }
}

async function copyText(text: string) {
  const clean = text.trim();
  if (!clean) return;
  try {
    await navigator.clipboard.writeText(clean);
    showToast('Copied');
    return;
  } catch {
    // Fallback inside sandboxed iframe
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = clean;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    showToast('Copied');
  } catch {
    showToast('Copy unavailable');
  }
}

async function sendSuggestedReply(rawText: string, _fromAlt = false) {
  const text = rawText.trim();
  if (!text || !canActOnCurrentSession() || sendingSuggestion) return;
  sendingSuggestion = true;
  const activeTurnKey = currentSnapshot?.suggestion?.turn_key || draftTurnKey;
  const predictedPrimary = currentSnapshot?.suggestion?.primary || draftPrimarySource || '';
  const wasEdited = Boolean(predictedPrimary) && text !== predictedPrimary.trim();
  lastSuggestRenderSig = '';
  renderSuggestSection();
  try {
    await app.sendMessage({
      role: 'user',
      content: [{ type: 'text', text }],
    });
    sentTurnKey = activeTurnKey;
    draftDirty = false;
    showToast(wasEdited ? 'Sent & learned edit' : 'Sent reply');
    void app
      .callServerTool({
        name: 'app_record_suggestion_feedback',
        arguments: {
          conversation_id: lockedConversationId || undefined,
          turn_key: activeTurnKey,
          predicted_primary: predictedPrimary,
          sent_text: text,
          edited: wasEdited,
        },
      })
      .then((res) => {
        const sc = (res as { structuredContent?: { habits?: HabitsData } })?.structuredContent;
        if (sc?.habits && currentSnapshot) {
          currentSnapshot.habits = sc.habits;
          lastSuggestRenderSig = '';
          renderSuggestSection();
        }
      })
      .catch(() => {});
    scheduleFastPoll(900);
  } catch {
    showToast('Could not send reply');
  } finally {
    sendingSuggestion = false;
    lastSuggestRenderSig = '';
    renderSuggestSection();
  }
}

async function relearnHabits() {
  try {
    const args: Record<string, string> = {};
    if (lockedConversationId) args.conversation_id = lockedConversationId;
    const result = await app.callServerTool({
      name: 'app_relearn_habits',
      arguments: args,
    });
    const sc = (result as { structuredContent?: ContextSnapshot })?.structuredContent;
    if (sc) {
      applySnapshot(sc);
      scheduleFastPoll(900);
      showToast('Re-learning style from recent chats…');
    }
  } catch {
    showToast('Re-learn failed');
  }
}

async function resetHabits() {
  const now = Date.now();
  if (now > resetHabitsConfirmUntil) {
    resetHabitsConfirmUntil = now + 4500;
    lastSuggestRenderSig = '';
    renderSuggestSection();
    window.setTimeout(() => {
      if (Date.now() >= resetHabitsConfirmUntil) {
        lastSuggestRenderSig = '';
        renderSuggestSection();
      }
    }, 4600);
    return;
  }
  resetHabitsConfirmUntil = 0;
  try {
    const args: Record<string, string> = {};
    if (lockedConversationId) args.conversation_id = lockedConversationId;
    const result = await app.callServerTool({
      name: 'app_reset_habits',
      arguments: args,
    });
    const sc = (result as { structuredContent?: ContextSnapshot })?.structuredContent;
    if (sc) {
      applySnapshot(sc);
      showToast('Cleared learned habits');
    }
  } catch {
    showToast('Reset failed');
  }
}

async function saveByokKey(rawKey: string, provider = 'gemini', clearKey = false) {
  const key = rawKey.trim();
  if (!clearKey && provider === 'gemini' && !key && !currentSnapshot?.habits?.has_saved_api_key) {
    showDrawerKeyInput = true;
    lastSuggestRenderSig = '';
    renderSuggestSection();
    return;
  }
  try {
    const args: Record<string, unknown> = { api_key: key, provider, clear_key: clearKey };
    if (lockedConversationId) args.conversation_id = lockedConversationId;
    const result = await app.callServerTool({
      name: 'app_save_llm_key',
      arguments: args,
    });
    const sc = (result as { structuredContent?: ContextSnapshot })?.structuredContent;
    if (sc) {
      showDrawerKeyInput = false;
      applySnapshot(sc);
      const effMode = sc.habits?.auth_mode || 'none';
      if (effMode !== 'none') {
        scheduleFastPoll(800);
      }
      if (clearKey) {
        showToast(effMode === 'none' ? 'Cleared saved auth config' : `Cleared saved config (env ${effMode} active)`);
      } else if (effMode === 'agy') {
        showToast('Using Gemini Subscription (agy)');
      } else if (effMode === 'gemini') {
        showToast(key ? 'API key saved locally' : 'Using Gemini API key');
      } else if (effMode === 'openai') {
        showToast('Using OpenAI-compatible endpoint');
      } else {
        showToast('No active credential found');
      }
    }
  } catch {
    showToast('Failed to update auth config');
  }
}

async function regenerateSuggestion() {
  try {
    draftDirty = false;
    sentTurnKey = '';
    const args: Record<string, string> = {};
    if (lockedConversationId) {
      args.conversation_id = lockedConversationId;
    }
    const result = await app.callServerTool({
      name: 'app_regenerate_suggestion',
      arguments: args,
    });
    const sc = (result as { structuredContent?: ContextSnapshot })
      ?.structuredContent;
    if (sc) {
      applySnapshot(sc);
    }
  } catch {
    showToast('Regenerate failed');
  }
}

async function toggleAutoSuggest() {
  const currentEnabled = currentSnapshot?.suggestion?.auto_suggest ?? true;
  const nextEnabled = !currentEnabled;
  try {
    const args: Record<string, unknown> = { enabled: nextEnabled };
    if (lockedConversationId) {
      args.conversation_id = lockedConversationId;
    }
    const result = await app.callServerTool({
      name: 'app_set_suggest_enabled',
      arguments: args,
    });
    const sc = (result as { structuredContent?: ContextSnapshot })
      ?.structuredContent;
    if (sc) {
      applySnapshot(sc);
      showToast(nextEnabled ? 'Auto-suggest on' : 'Auto-suggest paused');
    }
  } catch {
    showToast('Failed to update auto-suggest');
  }
}

function scheduleFastPoll(delayMs = 700) {
  if (fastPollTimer !== null) {
    window.clearTimeout(fastPollTimer);
    fastPollTimer = null;
  }
  fastPollTimer = window.setTimeout(() => {
    fastPollTimer = null;
    if (!document.hidden) {
      void refreshSnapshot(false);
    }
  }, delayMs);
}

function applySnapshot(sc: ContextSnapshot, forceBind = false) {
  const prevCid = lockedConversationId;
  if (sc.conversation_id && (forceBind || !lockedConversationId)) {
    const firstLock = !lockedConversationId;
    lockedConversationId = sc.conversation_id;
    if (forceBind || firstLock) {
      lockedBindMode = sc.bind_mode === 'auto' ? 'auto' : 'explicit';
      if (!hostConversationId && !initialAutoConversationId && lockedBindMode === 'auto') {
        initialAutoConversationId = sc.conversation_id;
      }
    }
  } else if (sc.bind_mode === 'explicit' && sc.conversation_id === lockedConversationId) {
    lockedBindMode = 'explicit';
  }
  if (prevCid && lockedConversationId && prevCid !== lockedConversationId) {
    draftText = '';
    draftTurnKey = '';
    draftPrimarySource = '';
    draftDirty = false;
    sentTurnKey = '';
    lastSuggestRenderSig = '';
  }
  currentSnapshot = sc;

  const s = sc.suggestion;
  if (s) {
    if (s.turn_key && s.turn_key !== sentTurnKey && s.status === 'ready') {
      // Clear sent marker once a brand new assistant turn finishes
      sentTurnKey = '';
    }
    if (s.status === 'ready' && s.primary) {
      const isNewTurn = s.turn_key !== draftTurnKey;
      const isNewPrimary = s.primary !== draftPrimarySource;
      if (isNewTurn || (!draftDirty && isNewPrimary)) {
        draftTurnKey = s.turn_key;
        draftPrimarySource = s.primary;
        draftText = s.primary;
        draftDirty = false;
      }
    } else if (s.status === 'working') {
      draftDirty = false;
    }

    if (s.status === 'generating') {
      scheduleFastPoll(700);
    }
  }
  if (sc.habits?.distilling) {
    scheduleFastPoll(900);
  }

  render();
}

async function toggleSessionPicker() {
  sessionPickerOpen = !sessionPickerOpen;
  render();
  if (!sessionPickerOpen) return;
  try {
    const result = await app.callServerTool({
      name: 'app_list_conversations',
      arguments: { limit: 10 },
    });
    const sc = (result as { structuredContent?: { conversations?: ConversationOption[] } })
      ?.structuredContent;
    if (sc?.conversations) {
      recentConversations = sc.conversations;
      render();
    }
  } catch {
    showToast('Unable to load sessions');
  }
}

async function bindToConversation(cid: string) {
  if (!cid) return;
  sessionPickerOpen = false;
  if (!hostConversationId) {
    if (!initialAutoConversationId && lockedConversationId) {
      initialAutoConversationId = lockedConversationId;
    }
    switchedViaPicker = Boolean(initialAutoConversationId && cid !== initialAutoConversationId);
  } else {
    switchedViaPicker = cid !== hostConversationId;
  }
  lockedConversationId = cid;
  lockedBindMode = 'explicit';
  draftText = '';
  draftTurnKey = '';
  draftPrimarySource = '';
  draftDirty = false;
  sentTurnKey = '';
  lastSuggestRenderSig = '';
  await refreshSnapshot(false);
  showToast(currentSnapshot?.title ? `Switched: ${currentSnapshot.title}` : 'Switched session');
}

async function refreshSnapshot(manual = false) {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const args: Record<string, string> = {
      client_id: clientId,
      bind_mode: lockedBindMode,
    };
    if (lockedConversationId) {
      args.conversation_id = lockedConversationId;
    }
    const result = await app.callServerTool({
      name: 'app_get_context_snapshot',
      arguments: args,
    });
    const sc = (result as { structuredContent?: ContextSnapshot })
      ?.structuredContent;
    if (sc) {
      applySnapshot(sc);
      if (manual) showToast('Updated');
    }
  } catch {
    // Ignore transient abort on view close/reload
  } finally {
    pollInFlight = false;
  }
}

function startPolling() {
  if (pollTimer !== null) window.clearInterval(pollTimer);
  pollTimer = window.setInterval(() => {
    if (!autoRefresh || document.hidden) return;
    void refreshSnapshot(false);
  }, 3000);
}

function autoResizeTextarea(ta: HTMLTextAreaElement) {
  ta.style.height = 'auto';
  const nextH = Math.min(140, Math.max(44, ta.scrollHeight));
  ta.style.height = `${nextH}px`;
}

function renderSuggestSection() {
  const host = document.getElementById('suggest-root');
  if (!host || !currentSnapshot || currentSnapshot.empty) return;

  const s: SuggestionData = currentSnapshot.suggestion ?? {
    status: 'idle',
    turn_key: '',
    primary: '',
    alternatives: [],
    auto_suggest: true,
  };

  const h: HabitsData = currentSnapshot.habits ?? {
    total_samples: 0,
    history_samples: 0,
    accepted_count: 0,
    edited_count: 0,
    overridden_count: 0,
    accept_rate_pct: null,
    distilling: false,
    profile_summary: '',
    frequent_phrases: [],
    profile_updated_at: '',
    recent_feedback: [],
  };

  const canAct = canActOnCurrentSession();
  const isForeignSession = canSendMessage && !canAct;
  const returnHostCid = isForeignSession ? getReturnHostConversationId() : null;
  const isSent = Boolean(sentTurnKey && s.turn_key && sentTurnKey === s.turn_key);
  const isConfirmingReset = Date.now() < resetHabitsConfirmUntil;
  const sig = JSON.stringify({
    st: s.status,
    tk: s.turn_key,
    pr: s.primary,
    al: s.alternatives,
    au: s.auto_suggest,
    er: s.error || '',
    lm: s.latency_ms || 0,
    md: s.model || '',
    se: sendingSuggestion,
    sn: isSent,
    cs: canAct,
    fs: isForeignSession,
    rc: returnHostCid || '',
    dr: draftDirty ? '__DIRTY__' : draftText,
    he: habitsExpanded,
    sk: showDrawerKeyInput,
    am: h.auth_mode || '',
    ag: Boolean(h.agy_available),
    hk: Boolean(h.has_saved_api_key),
    hr: isConfirmingReset,
    hs: h.total_samples,
    hd: h.distilling,
    hp: h.profile_updated_at,
    ha: h.accepted_count,
    hed: h.edited_count,
    ho: h.overridden_count,
  });

  if (sig === lastSuggestRenderSig) return;
  lastSuggestRenderSig = sig;

  const isGen = s.status === 'generating';
  const latencyLabel =
    s.status === 'ready' && s.latency_ms
      ? `${(s.latency_ms / 1000).toFixed(1)}s`
      : isGen
        ? 'drafting…'
        : '';

  let bodyHtml = '';

  if (isSent) {
    bodyHtml = `
      <div class="suggest-status-box">
        <span>Sent · waiting for reply…</span>
      </div>
    `;
  } else if (s.status === 'working') {
    bodyHtml = `
      <div class="suggest-status-box">
        <span>Waiting for assistant turn to finish…</span>
      </div>
    `;
  } else if (s.status === 'idle') {
    bodyHtml = `
      <div class="suggest-status-box">
        <span>Next-reply suggestions appear on your turn</span>
      </div>
    `;
  } else if (s.status === 'disabled') {
    bodyHtml = `
      <div class="suggest-status-box">
        <span>Auto-suggest paused</span>
        <button class="chip-btn" data-suggest-action="regen" type="button">Generate</button>
      </div>
    `;
  } else if (s.status === 'needs_key') {
    const agyNote = s.error
      ? esc(s.error)
      : 'Use your Gemini subscription (via Antigravity CLI) or paste a Gemini API key:';
    const agyBtn = h.agy_available
      ? `<button class="chip-btn active" data-suggest-action="use-agy" type="button" title="Use local Antigravity CLI (agy) Google subscription OAuth">Use Subscription (agy)</button>`
      : '';
    bodyHtml = `
      <div class="byok-box">
        <div class="byok-note">${agyNote}</div>
        ${agyBtn ? `<div class="byok-row">${agyBtn}</div>` : ''}
        <div class="byok-row">
          <input id="byok-key-input" class="byok-input" type="password" autocomplete="off" placeholder="AIza… Gemini API Key (0600)" />
          <button class="chip-btn" data-suggest-action="save-key" type="button">Save</button>
        </div>
      </div>
    `;
  } else if (s.status === 'error') {
    bodyHtml = `
      <div class="suggest-status-box">
        <span title="${esc(s.error || 'Error')}">${esc(s.error || 'Suggestion failed')}</span>
        <button class="chip-btn" data-suggest-action="regen" type="button">Retry</button>
      </div>
    `;
  } else if (isGen && !draftText) {
    bodyHtml = `
      <div class="suggest-skel" aria-busy="true">
        <div class="skel-line w85"></div>
        <div class="skel-line w55"></div>
      </div>
    `;
  } else {
    const alts = s.alternatives || [];
    const altHtml =
      alts.length > 0
        ? `<div class="alt-list">
            ${alts
              .map(
                (alt, idx) => `
              <div class="alt-row">
                <button class="alt-pick" data-alt-pick="${idx}" type="button" title="Click to load into box">${esc(alt)}</button>
                ${
                  canAct
                    ? `<button class="alt-send" data-alt-send="${idx}" type="button" title="Send immediately">${iconSvg('send')}</button>`
                    : ''
                }
              </div>
            `
              )
              .join('')}
          </div>`
        : '';

    const hintLabel = canAct
      ? 'Enter to send · Shift+Enter newline'
      : isForeignSession
        ? 'Viewing another session · Copy only'
        : 'Detached pane · Copy only (open via chat to Send)';

    bodyHtml = `
      <div class="suggest-box ${isGen ? 'dimmed' : ''}">
        <textarea
          id="suggest-textarea"
          class="suggest-input"
          rows="2"
          placeholder="Suggested next reply…"
        >${esc(draftText)}</textarea>
        <div class="suggest-footer">
          <span class="suggest-hint">${hintLabel}</span>
          <div class="suggest-btns">
            ${
              returnHostCid
                ? `<button class="chip-btn" data-suggest-action="return-host" type="button" title="Switch back to this window's chat to re-enable Send">Back to chat</button>`
                : ''
            }
            <button class="copy-btn" data-suggest-action="copy" type="button" title="Copy suggestion">Copy</button>
            ${
              canAct
                ? `<button class="send-btn" data-suggest-action="send" type="button" ${sendingSuggestion ? 'disabled' : ''}>${
                    sendingSuggestion ? 'Sending…' : `Send ${iconSvg('send')}`
                  }</button>`
                : ''
            }
          </div>
        </div>
      </div>
      ${altHtml}
    `;
  }

  const habitsDrawerHtml = habitsExpanded
    ? (() => {
        const authMode = h.auth_mode || 'none';
        const authRowHtml = `
          <div class="habits-auth-row">
            <span class="habits-auth-label">Auth</span>
            <div class="habits-auth-pills">
              ${
                h.agy_available
                  ? `<button class="chip-btn ${authMode === 'agy' ? 'active' : ''}" data-suggest-action="use-agy" type="button" title="Use Antigravity CLI (agy) Gemini subscription OAuth — no API key needed">Subscription (agy)</button>`
                  : ''
              }
              <button class="chip-btn ${authMode === 'gemini' ? 'active' : ''}" data-suggest-action="switch-apikey" type="button" title="Use Gemini API Key (faster ~1s direct HTTP)">API Key</button>
              <button class="chip-btn" data-suggest-action="toggle-key-input" type="button" title="Set or update Gemini API key">${showDrawerKeyInput ? 'Cancel' : 'Set Key'}</button>
              ${
                h.has_saved_api_key || authMode !== 'none'
                  ? `<button class="chip-btn" data-suggest-action="clear-key" type="button" title="Clear saved auth config">Clear</button>`
                  : ''
              }
            </div>
          </div>
          ${
            showDrawerKeyInput
              ? `<div class="byok-row">
                   <input id="drawer-key-input" class="byok-input" type="password" autocomplete="off" placeholder="AIza… (stored locally)" />
                   <button class="chip-btn" data-suggest-action="save-drawer-key" type="button">Save</button>
                 </div>`
              : ''
          }
        `;
        return `
          <div class="habits-drawer">
            ${authRowHtml}
            <div class="habits-auth-row">
              <span class="habits-auth-label">Style</span>
              <div class="habits-auth-pills">
                <button class="chip-btn" data-suggest-action="relearn-habits" type="button" ${h.distilling ? 'disabled' : ''}>
                  ${h.distilling ? 'Learning…' : 'Re-learn'}
                </button>
                <button class="chip-btn ${isConfirmingReset ? 'danger' : ''}" data-suggest-action="reset-habits" type="button">
                  ${isConfirmingReset ? 'Confirm reset?' : 'Reset'}
                </button>
              </div>
            </div>
          </div>
        `;
      })()
    : '';

  host.innerHTML = `
    <section class="sec">
      <div class="sec-head">
        <span class="suggest-head-left">
          ${iconSvg('sparkle')}
          <span class="sec-title">Suggested reply</span>
          ${latencyLabel ? `<span class="suggest-meta">${esc(latencyLabel)}</span>` : ''}
        </span>
        <span class="suggest-head-right">
          <button
            class="chip-btn ${s.auto_suggest ? 'active' : ''}"
            data-suggest-action="toggle-auto-suggest"
            type="button"
            title="Auto-generate suggested replies when assistant finishes a turn"
          >${s.auto_suggest ? 'Auto' : 'Off'}</button>
          <button
            class="ghost-btn"
            data-suggest-action="regen"
            type="button"
            title="Regenerate suggestion"
          >${iconSvg('refresh', isGen ? 'spinning' : '')}</button>
          <button
            class="ghost-btn ${habitsExpanded ? 'active' : ''}"
            data-suggest-action="toggle-habits"
            type="button"
            title="Suggested reply settings"
          >${iconSvg('settings')}</button>
        </span>
      </div>
      ${bodyHtml}
      ${habitsDrawerHtml}
    </section>
  `;

  const ta = host.querySelector<HTMLTextAreaElement>('#suggest-textarea');
  if (ta) {
    autoResizeTextarea(ta);
    ta.addEventListener('input', () => {
      draftText = ta.value;
      draftDirty = true;
      autoResizeTextarea(ta);
    });
    ta.addEventListener('keydown', (e) => {
      if (canActOnCurrentSession() && e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        void sendSuggestedReply(ta.value, false);
      }
    });
  }

  host.querySelectorAll<HTMLElement>('[data-suggest-action]').forEach((el) => {
    el.onclick = () => {
      const action = el.dataset.suggestAction;
      if (action === 'toggle-auto-suggest') {
        void toggleAutoSuggest();
      } else if (action === 'toggle-habits') {
        habitsExpanded = !habitsExpanded;
        lastSuggestRenderSig = '';
        renderSuggestSection();
      } else if (action === 'relearn-habits') {
        void relearnHabits();
      } else if (action === 'reset-habits') {
        void resetHabits();
      } else if (action === 'regen') {
        void regenerateSuggestion();
      } else if (action === 'copy') {
        const cur = host.querySelector<HTMLTextAreaElement>('#suggest-textarea')?.value ?? draftText;
        void copyText(cur);
      } else if (action === 'return-host') {
        const target = getReturnHostConversationId();
        if (target) void bindToConversation(target);
      } else if (action === 'send') {
        const cur = host.querySelector<HTMLTextAreaElement>('#suggest-textarea')?.value ?? draftText;
        void sendSuggestedReply(cur, false);
      } else if (action === 'save-key') {
        const inp = host.querySelector<HTMLInputElement>('#byok-key-input');
        void saveByokKey(inp?.value ?? '', 'gemini');
      } else if (action === 'save-drawer-key') {
        const inp = host.querySelector<HTMLInputElement>('#drawer-key-input');
        void saveByokKey(inp?.value ?? '', 'gemini');
      } else if (action === 'use-agy') {
        void saveByokKey('', 'agy');
      } else if (action === 'switch-apikey') {
        void saveByokKey('', 'gemini');
      } else if (action === 'clear-key') {
        void saveByokKey('', 'gemini', true);
      } else if (action === 'toggle-key-input') {
        showDrawerKeyInput = !showDrawerKeyInput;
        lastSuggestRenderSig = '';
        renderSuggestSection();
      }
    };
  });

  const keyInp = host.querySelector<HTMLInputElement>('#byok-key-input');
  if (keyInp) {
    keyInp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        void saveByokKey(keyInp.value, 'gemini');
      }
    });
  }
  const drawerKeyInp = host.querySelector<HTMLInputElement>('#drawer-key-input');
  if (drawerKeyInp) {
    drawerKeyInp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        void saveByokKey(drawerKeyInp.value, 'gemini');
      }
    });
  }

  const alts = s.alternatives || [];
  host.querySelectorAll<HTMLElement>('[data-alt-pick]').forEach((el) => {
    el.onclick = () => {
      const idx = Number(el.dataset.altPick);
      const picked = alts[idx];
      if (!picked) return;
      draftText = picked;
      draftDirty = true;
      const box = host.querySelector<HTMLTextAreaElement>('#suggest-textarea');
      if (box) {
        box.value = picked;
        autoResizeTextarea(box);
        box.focus();
        box.setSelectionRange(picked.length, picked.length);
      }
    };
  });

  host.querySelectorAll<HTMLElement>('[data-alt-send]').forEach((el) => {
    el.onclick = () => {
      const idx = Number(el.dataset.altSend);
      const picked = alts[idx];
      if (!picked) return;
      void sendSuggestedReply(picked, true);
    };
  });
}

function ensureShell(): boolean {
  const root = document.getElementById('app');
  if (!root) return false;
  if (!document.getElementById('ctx-body')) {
    root.innerHTML = `
      <div class="panel">
        <div id="ctx-body"></div>
        <div class="divider"></div>
        <div id="suggest-root"></div>
        <div id="toast-root"></div>
      </div>
    `;
    lastSuggestRenderSig = '';
  }
  return true;
}

function render() {
  const root = document.getElementById('app');
  if (!root) return;

  if (!currentSnapshot) {
    lastSuggestRenderSig = '';
    root.innerHTML = `<div class="empty-state">Loading context…</div>`;
    return;
  }

  if (currentSnapshot.empty) {
    lastSuggestRenderSig = '';
    root.innerHTML = `<div class="empty-state">Waiting for conversation context…</div>`;
    return;
  }

  if (!ensureShell()) return;
  const ctxBody = document.getElementById('ctx-body');
  if (!ctxBody) return;

  const snap = currentSnapshot;
  const env = snap.environment ?? {
    host: '-',
    workspace_name: '-',
    workspace_path: null,
    workspace_display: '-',
    branch: null,
  };
  const usedPct = snap.used_pct ?? 0;
  const usedTokens = snap.used_tokens ?? 0;
  const limitTokens = snap.limit_tokens ?? 512000;
  const nativeLimitTokens = snap.native_limit_tokens ?? limitTokens;
  const hasCompactCap = nativeLimitTokens > limitTokens;
  const ratioTitle = hasCompactCap
    ? `Antigravity auto-compact limit: ${fmtTokens(limitTokens)} (model native window: ${fmtTokens(nativeLimitTokens)}; auto-lifts if exceeded)`
    : `Model context window: ${fmtTokens(limitTokens)}`;
  const categories = snap.categories ?? [];
  const memoryFiles = snap.memory_files ?? [];
  const loadedSkills = snap.loaded_skills ?? [];
  const outputs = snap.outputs ?? [];

  const pctClass =
    usedPct >= 88 ? 'pct-danger' : usedPct >= 70 ? 'pct-warn' : '';

  const barSegments = categories
    .filter((c) => !c.is_free && c.tokens > 0)
    .map((c) => {
      const w = Math.max(0.6, Math.min(100, (c.tokens / limitTokens) * 100));
      return `<div class="bar-seg" style="width:${w.toFixed(2)}%;background:${c.color}" title="${esc(c.label)}: ${fmtTokens(c.tokens)} (${fmtPct(c.pct)})"></div>`;
    })
    .join('');

  const dottedKeys = new Set([
    'memory_files',
    'skills',
    'system_tools',
    'system_prompt',
  ]);

  const catRows = categories
    .map((c) => {
      const labelCls = dottedKeys.has(c.key) ? 'cat-label dotted' : 'cat-label';
      const dotStyle = c.is_free
        ? `background:var(--ctx-free)`
        : `background:${c.color}`;
      return `
        <div class="cat-row" title="${esc(c.hint || c.label)}">
          <span class="cat-dot" style="${dotStyle}"></span>
          <span class="${labelCls}">${esc(c.label)}</span>
          <span class="cat-val">${fmtTokens(c.tokens)}</span>
          <span class="cat-pct">${fmtPct(c.pct)}</span>
        </div>
      `;
    })
    .join('');

  const memAccordion = `
    <button class="acc-btn ${memoryExpanded ? 'open' : ''}" data-action="toggle-memory" type="button">
      ${iconSvg('chevron')}
      <span class="acc-title">Memory files</span>
      <span class="acc-count">${memoryFiles.length}</span>
    </button>
    ${
      memoryExpanded
        ? `<div class="acc-list">
            ${
              memoryFiles.length === 0
                ? `<div class="acc-empty">No GEMINI.md or AGENTS.md detected</div>`
                : memoryFiles
                    .map(
                      (m) => `
                  <button class="file-row" data-uri="${esc(m.uri)}" data-name="${esc(m.name)}" title="${esc(m.path)}" type="button">
                    ${iconSvg('doc')}
                    <span class="file-name">${esc(m.name)}</span>
                    <span class="file-meta">${fmtTokens(m.tokens)}</span>
                  </button>
                `
                    )
                    .join('')
            }
          </div>`
        : ''
    }
  `;

  const skillsAccordion =
    loadedSkills.length > 0
      ? `
    <button class="acc-btn ${skillsExpanded ? 'open' : ''}" data-action="toggle-skills" type="button">
      ${iconSvg('chevron')}
      <span class="acc-title">Loaded skills</span>
      <span class="acc-count">${loadedSkills.length}</span>
    </button>
    ${
      skillsExpanded
        ? `<div class="acc-list">
            ${loadedSkills
              .map(
                (s) => `
              <button class="file-row" data-uri="${esc(s.uri)}" data-name="${esc(s.name)}" title="${esc(s.path)}" type="button">
                ${iconSvg('doc')}
                <span class="file-name">${esc(s.name)}</span>
                <span class="file-meta">${fmtTokens(s.tokens)}</span>
              </button>
            `
              )
              .join('')}
          </div>`
        : ''
    }
  `
      : '';

  const outputsHtml =
    outputs.length > 0
      ? outputs
          .map(
            (o) => `
          <button class="out-row" data-uri="${esc(o.uri)}" data-name="${esc(o.name)}" title="${esc(o.path)} (${fmtBytes(o.bytes)})" type="button">
            ${iconSvg(outputIconKind(o.ext))}
            <span class="out-name">${esc(o.name)}</span>
          </button>
        `
          )
          .join('')
      : `<div class="acc-empty">No outputs yet</div>`;

  ctxBody.innerHTML = `
      <section class="sec">
        <div class="sec-head">
          <span class="sec-title">Environment</span>
          <button class="ghost-btn" data-action="refresh" title="Refresh now" type="button">
            ${iconSvg('refresh')}
          </button>
        </div>
        <div class="env-list">
          <div class="env-row" title="Host machine">
            ${iconSvg('monitor')}
            <span class="env-text">${esc(env.host)}</span>
          </div>
          <div class="env-row" title="${esc(env.workspace_display)}">
            ${iconSvg('folder')}
            <span class="env-text">${esc(env.workspace_name)}</span>
            ${
              env.workspace_path
                ? `<span class="env-sub">· ~</span>`
                : `<span class="env-sub">· no workspace</span>`
            }
          </div>
          ${
            env.branch
              ? `<div class="env-row" title="Git branch">
                  ${iconSvg('branch')}
                  <span class="env-text">${esc(env.branch)}</span>
                </div>`
              : ''
          }
          <div class="env-row" title="${esc(snap.model_id || '')}">
            ${iconSvg('model')}
            <span class="env-text">${esc(snap.model_display || 'Unknown')}</span>
          </div>
          <button
            class="session-row ${sessionPickerOpen ? 'open' : ''}"
            data-action="toggle-session-picker"
            type="button"
            title="${
              lockedBindMode === 'auto'
                ? `Auto-matched session (${esc(snap.conversation_id || '')}) — click to switch, or send a message to lock`
                : `Bound session (${esc(snap.conversation_id || '')}) — click to switch`
            }"
          >
            ${iconSvg('chevron')}
            <span class="session-title">${esc(snap.title || (snap.conversation_id || '').slice(0, 8))}</span>
            ${lockedBindMode === 'auto' ? `<span class="session-auto-badge">Auto</span>` : ''}
          </button>
          ${
            sessionPickerOpen
              ? `<div class="session-list">
                  ${
                    recentConversations.length === 0
                      ? `<div class="acc-empty">Loading sessions…</div>`
                      : recentConversations
                          .map(
                            (c) => `
                        <button
                          class="session-opt ${c.conversation_id === snap.conversation_id ? 'active' : ''}"
                          data-bind-cid="${esc(c.conversation_id)}"
                          type="button"
                          title="${esc(c.title)} (${esc(c.short_id)})"
                        >
                          <span class="session-opt-title">${esc(c.title)}</span>
                        </button>
                      `
                          )
                          .join('')
                  }
                </div>`
              : ''
          }
        </div>
      </section>

      <div class="divider"></div>

      <section class="sec">
        <div class="sec-head">
          <span class="sec-title">Context</span>
          <span class="live-badge" title="Prompt cache hit ratio on the latest model call">
            <span class="live-dot ${autoRefresh ? 'on' : ''}"></span>
            Cache ${Math.round(snap.cache_hit_pct ?? 0)}%
          </span>
        </div>

        <div class="ctx-summary">
          <span class="ctx-pct ${pctClass}">${Math.min(100, Math.round(usedPct))}% used</span>
          <span class="ctx-ratio" title="${esc(ratioTitle)}">${fmtTokens(usedTokens)} / ${fmtTokens(limitTokens)}${hasCompactCap ? `<span class="ctx-native"> · ${fmtTokens(nativeLimitTokens)} native</span>` : ''}</span>
        </div>

        <div class="bar-track" role="progressbar" aria-valuenow="${Math.min(100, Math.round(usedPct))}" aria-valuemin="0" aria-valuemax="100">
          ${barSegments}
        </div>

        <div class="cat-list">
          ${catRows}
        </div>

        <div class="acc-wrap">
          ${memAccordion}
          ${skillsAccordion}
        </div>

        <div class="meta-bar">
          <span class="meta-stats">
            Steps ${snap.active_step_count ?? 0}${
              (snap.checkpoint_count ?? 0) > 0
                ? ` · Checkpoints ${snap.checkpoint_count}`
                : ''
            }
          </span>
          <div class="meta-actions">
            <button class="chip-btn ${autoRefresh ? 'active' : ''}" data-action="toggle-auto" type="button" title="Toggle 3s auto-refresh">
              ${autoRefresh ? 'Live 3s' : 'Paused'}
            </button>
          </div>
        </div>
      </section>

      <div class="divider"></div>

      <section class="sec">
        <button class="acc-btn sec-acc-btn ${outputsExpanded ? 'open' : ''}" data-action="toggle-outputs" type="button">
          ${iconSvg('chevron')}
          <span class="acc-title">Outputs</span>
          <span class="acc-count">${outputs.length}</span>
        </button>
        ${
          outputsExpanded
            ? `<div class="out-list acc-out-list">
                ${outputsHtml}
              </div>`
            : ''
        }
      </section>
  `;

  ctxBody.querySelectorAll<HTMLElement>('[data-action]').forEach((el) => {
    el.onclick = () => {
      const action = el.dataset.action;
      if (action === 'toggle-memory') {
        memoryExpanded = !memoryExpanded;
        render();
      } else if (action === 'toggle-skills') {
        skillsExpanded = !skillsExpanded;
        render();
      } else if (action === 'toggle-outputs') {
        outputsExpanded = !outputsExpanded;
        render();
      } else if (action === 'refresh') {
        void refreshSnapshot(true);
      } else if (action === 'toggle-auto') {
        autoRefresh = !autoRefresh;
        render();
      } else if (action === 'toggle-session-picker') {
        void toggleSessionPicker();
      }
    };
  });

  ctxBody.querySelectorAll<HTMLElement>('[data-bind-cid]').forEach((el) => {
    el.onclick = () => {
      const cid = el.dataset.bindCid || '';
      void bindToConversation(cid);
    };
  });

  ctxBody.querySelectorAll<HTMLElement>('[data-uri]').forEach((el) => {
    el.onclick = () => {
      const uri = el.dataset.uri || '';
      const name = el.dataset.name || 'file';
      void openFileInSidePane(uri, name);
    };
  });

  renderSuggestSection();
  renderToast();
}

function applyHostContext() {
  const ctx = app.getHostContext();
  if (!ctx) return;
  const hostCid = extractHostConversationId(ctx);
  if (hostCid) {
    hostConversationId = hostCid;
    initialAutoConversationId = null;
    switchedViaPicker = false;
    lockedConversationId = hostCid;
    lockedBindMode = 'explicit';
  }
  if (ctx.theme) {
    applyDocumentTheme(ctx.theme);
    document.documentElement.dataset.theme = ctx.theme;
  }
  if (ctx.styles?.variables) {
    applyHostStyleVariables(ctx.styles.variables);
  }
  if (ctx.styles?.css?.fonts) {
    applyHostFonts(ctx.styles.css.fonts);
  }
}

// Register handlers BEFORE connect() per MCP Apps spec
app.ontoolinput = ({ arguments: args } = {} as { arguments?: Record<string, unknown> }) => {
  const cid = args && typeof (args as Record<string, unknown>).conversation_id === 'string'
    ? ((args as Record<string, unknown>).conversation_id as string).trim()
    : '';
  if (cid) {
    hostConversationId = cid;
    initialAutoConversationId = null;
    switchedViaPicker = false;
    lockedConversationId = cid;
    lockedBindMode = 'explicit';
  } else if (!lockedConversationId) {
    lockedBindMode = 'auto';
  }
  if (!currentSnapshot) render();
};

app.ontoolresult = (result) => {
  const sc = (result as { structuredContent?: ContextSnapshot })
    ?.structuredContent;
  if (sc) {
    if (sc.conversation_id && sc.bind_mode === 'explicit') {
      hostConversationId = sc.conversation_id;
      initialAutoConversationId = null;
      switchedViaPicker = false;
    }
    applySnapshot(sc, true);
  }
};

app.ontoolcancelled = () => {
  // Keep last known snapshot visible
};

app.onhostcontextchanged = (ctx) => {
  const hostCid = extractHostConversationId(ctx);
  if (hostCid) {
    const changed = hostCid !== lockedConversationId;
    hostConversationId = hostCid;
    initialAutoConversationId = null;
    switchedViaPicker = false;
    lockedConversationId = hostCid;
    lockedBindMode = 'explicit';
    if (changed) {
      draftText = '';
      draftTurnKey = '';
      draftPrimarySource = '';
      draftDirty = false;
      sentTurnKey = '';
      lastSuggestRenderSig = '';
      void refreshSnapshot(false);
    } else {
      render();
    }
  }
  if (ctx.theme) {
    applyDocumentTheme(ctx.theme);
    document.documentElement.dataset.theme = ctx.theme;
  }
  if (ctx.styles?.variables) {
    applyHostStyleVariables(ctx.styles.variables);
  }
  if (ctx.styles?.css?.fonts) {
    applyHostFonts(ctx.styles.css.fonts);
  }
};

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && autoRefresh) {
    void refreshSnapshot(false);
  }
});

async function boot() {
  render();
  try {
    await app.connect();
    applyHostContext();
    const caps = app.getHostCapabilities();
    canSendMessage = Boolean(caps?.message);
    if (!currentSnapshot) {
      await refreshSnapshot(false);
    } else {
      render();
    }
    startPolling();
  } catch {
    const root = document.getElementById('app');
    if (root) {
      root.innerHTML = `<div class="empty-state">Unable to connect to Antigravity host. Reopen the Context pane to retry.</div>`;
    }
  }
}

void boot();
