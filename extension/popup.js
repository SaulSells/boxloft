const $ = (id) => document.getElementById(id);
const sellerLine = (r) => (!r.seller || (!r.seller.asked && !r.seller.error) ? '' : r.seller.error ? `\nSeller Central: ${r.seller.error}` : `\nSeller Central: ${r.seller.read} of ${r.seller.asked} shipped orders read, ${r.seller.sizes} box sizes.`);
const walmartLine = (r) => sellerLine(r) + (!r.walmart ? '' : r.walmart.error ? `\nWalmart: ${r.walmart.error}` : `\nWalmart: ${r.walmart.read} of ${r.walmart.asked} order pages read.`);
const show = (r) => {
  if (!r) { $('status').textContent = 'Not synced yet. It runs every 30 minutes.'; return; }
  const when = r.at ? new Date(r.at).toLocaleString() : '';
  if (r.in_progress) {
    $('status').textContent = `Syncing (${r.step || 'working'})...\n${r.found_new ?? 0} new orders, ${r.read ?? 0} read, ${r.sent ?? 0} sent to Boxloft so far.\nYou can close this; it keeps going.`;
  } else if (r.ok && r.stopped) {
    $('status').textContent = `Last sync ${when}: stopped early.\n${Object.entries(r.stopped).map(([k, v]) => `${k}: ${v}`).join('\n')}\n${r.found_new} new orders, ${r.checked} read.` + walmartLine(r);
  } else if (r.ok) {
    $('status').textContent = `Last sync ${when}: done.\n${r.found_new} new orders, ${r.checked} read, ${r.sent ?? 0} sent to Boxloft.` + walmartLine(r);
  } else {
    $('status').textContent = `Last sync ${when} failed:\n${r.error || r.skipped}` + walmartLine(r);
  }
};
// Live progress while the popup is open.
chrome.storage.onChanged.addListener((ch, area) => { if (area === 'local' && ch.lastResult) show(ch.lastResult.newValue); });
(async () => {
  let s = await chrome.storage.local.get(['server', 'token', 'device', 'lastResult']);
  if (!s.token) {
    try {
      const local = await fetch(chrome.runtime.getURL('local-config.json')).then((r) => r.json());
      if (local.token) { await chrome.storage.local.set({ server: local.server, token: local.token, device: local.device }); s = { ...s, ...local }; }
    } catch { /* none */ }
  }
  $('server').value = s.server || 'http://localhost:5101';
  $('token').value = s.token || '';
  $('device').value = s.device || '';
  show(s.lastResult);
})();
$('save').onclick = async () => {
  await chrome.storage.local.set({ server: $('server').value.trim() || 'http://localhost:5101', token: $('token').value.trim(), device: $('device').value.trim() || 'Chrome' });
  $('status').textContent = 'Saved.';
};
$('sync').onclick = async () => {
  $('status').textContent = 'Starting...';
  show(await chrome.runtime.sendMessage({ type: 'sync-now' }));
};
