// Wakes every 30 minutes and asks the offscreen page (which can parse HTML) to run a sync.
const PERIOD_MIN = 30;

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['DOM_PARSER'],
    justification: 'Fetch the Amazon and Walmart order pages Boxloft asks for',
  });
}

// Settings live in chrome.storage, which the offscreen page can't reach, so they're read here
// and sent along with each sync request.
async function settings() {
  let s = await chrome.storage.local.get(['server', 'token', 'device', 'backfillDone']);
  if (!s.token) {
    // First run: settings Claude left in local-config.json (kept out of git), so nothing needs pasting.
    try {
      const local = await fetch(chrome.runtime.getURL('local-config.json')).then((r) => r.json());
      if (local.token) {
        await chrome.storage.local.set({ server: local.server, token: local.token, device: local.device || 'Chrome' });
        s = { ...s, ...local };
      }
    } catch {
      // no local file: the popup is the way in
    }
  }
  return { server: (s.server || 'http://localhost:5101').replace(/\/+$/, ''), token: s.token || '', device: s.device || 'Chrome', backfillDone: !!s.backfillDone };
}

// The offscreen page answers "started" at once, then sends sync-progress and sync-done messages.
// A run flag older than 3 hours is treated as dead (Chrome closed mid-run).
async function runSync(reason) {
  const { running, runningSince } = await chrome.storage.session.get(['running', 'runningSince']);
  if (running && Date.now() - (runningSince || 0) < 3 * 3600 * 1000) return { ok: true, in_progress: true, step: 'already running' };
  await chrome.storage.session.set({ running: true, runningSince: Date.now() });
  try {
    const cfg = await settings();
    if (!cfg.token) throw new Error('Add your Boxloft sync token in the extension popup');
    await ensureOffscreen();
    await chrome.runtime.sendMessage({ type: 'run-sync', reason, cfg });
    const status = { ok: true, in_progress: true, step: 'starting', at: new Date().toISOString() };
    await chrome.storage.local.set({ lastResult: status });
    return status;
  } catch (e) {
    const result = { ok: false, error: String(e && e.message || e), at: new Date().toISOString() };
    await chrome.storage.local.set({ lastResult: result });
    await chrome.storage.session.set({ running: false });
    return result;
  }
}

async function finished(result) {
  await chrome.storage.local.set({ lastResult: { ...result, at: new Date().toISOString() } });
  if (result && result.ok && result.backfill_done) await chrome.storage.local.set({ backfillDone: true });
  await chrome.storage.session.set({ running: false });
}

// First sync 1 minute after install, then every 30 minutes.
chrome.runtime.onInstalled.addListener(() => chrome.alarms.create('sync', { periodInMinutes: PERIOD_MIN, delayInMinutes: 1 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms.create('sync', { periodInMinutes: PERIOD_MIN, delayInMinutes: 1 }));
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'sync') runSync('timer'); });
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type === 'sync-now') { runSync('button').then(reply); return true; }
  if (msg.type === 'sync-progress') { chrome.storage.local.set({ lastResult: { ...msg, ok: true, in_progress: true, at: new Date().toISOString() } }); return false; }
  if (msg.type === 'sync-done') { finished(msg.result || { ok: false, error: 'no result' }); return false; }
  return false;
});
