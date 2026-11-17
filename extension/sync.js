// The sync (2.0). Runs in an offscreen extension page so it can use fetch with your Amazon,
// Seller Central and Walmart sign-ins. It only reads pages; it never clicks, buys or changes anything.
//
// Boxloft plans the run and reads the pages; this file only fetches what Boxloft asks for:
// 1. POST /api/inbound/v2/step with the pages just read. Boxloft answers with the next pages
//    to read, or done.
// 2. Fetch those pages, check each one, send it back. Repeat.
//
// The safety rules live HERE, and nothing Boxloft sends can change them:
// - ALLOW: only Amazon Your Orders pages, a shipped package's Track page, Seller Central's data for one
//   order (2.2: sellercentral.amazon.com/orders-api/order/<order id>, a read; Boxloft asks only for
//   orders you just shipped), and walmart.com/orders/<digits>. Any other URL is refused.
// - Pace: Amazon 4 pages at a time, then 0.3-0.6 s (as 1.x did); Seller Central one order at a time,
//   0.6-1.2 s apart; Walmart one page at a time, 2-5 s apart.
// - MAX_PAGES per site per run. At the cap the site stops; the next run continues.
// - A sign-in, captcha or robot-check page stops that site for the rest of the run.
// - What leaves this computer: Amazon pages without their scripts and styles; for Walmart only
//   the order object from the page's __NEXT_DATA__, never the page; for Seller Central only each
//   package's id, carrier, box size, weight, label cost and ASINs (sellerPackages), never the buyer's
//   name, address or anything else in the answer.

const ALLOW = {
  amazon: [
    /^https:\/\/www\.amazon\.com\/your-orders\/orders\?timeFilter=year-\d{4}&startIndex=\d{1,4}$/,
    /^https:\/\/www\.amazon\.com\/your-orders\/order-details\?orderID=\d{3}-\d{7}-\d{7}$/,
    // 2.1: a shipped package's Track page (carrier, tracking ID, delivery photo). Exact shape only: never
    // the preship/cancel-items page the order page also links.
    /^https:\/\/www\.amazon\.com\/progress-tracker\/package\?orderId=\d{3}-\d{7}-\d{7}&shipmentId=[A-Za-z0-9]{4,24}&packageIndex=\d{1,2}$/,
  ],
  // 2.2 read one order's data in Seller Central. Off since 2.3 (Alex 11-11): Amazon's seller agreement wants
  // automated tools to identify themselves, and this used the signed-in business account. Nothing matches here.
  seller: [],
  walmart: [/^https:\/\/www\.walmart\.com\/orders\/\d{10,20}$/],
};
const MAX_PAGES = { amazon: 1500, seller: 300, walmart: 300 };
const MAX_PER_STEP = 20;
const AMAZON_PARALLEL = 4;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pace = {
  amazon: () => sleep(300 + Math.random() * 300),
  seller: () => sleep(600 + Math.random() * 600),
  walmart: () => sleep(2000 + Math.random() * 3000),
};

const siteOf = (url) => Object.keys(ALLOW).find((s) => ALLOW[s].some((re) => re.test(url))) || null;

async function api(cfg, method, path, body) {
  const r = await fetch(cfg.server + path, {
    method,
    headers: { Authorization: `Bearer ${cfg.token}`, 'X-Boxloft': '1', 'Content-Type': 'application/json', 'X-Device': cfg.device },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`Boxloft: ${data.error || r.status}`);
  return data;
}

class Stop extends Error {}

// Cut <script>, <style>, <svg> and <noscript> bodies: Boxloft reads only the visible markup, and
// this drops about three quarters of each page (and Amazon's session id, which looks like an order ID).
const trimPage = (html) => html.replace(/<(script|style|svg|noscript)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '');

async function readAmazon(url) {
  const r = await fetch(url, { credentials: 'include' });
  const html = await r.text();
  if (/\/ap\/signin/.test(r.url) || /<title>[^<]*Sign.?In/i.test(html)) throw new Stop('Signed out of Amazon in this Chrome profile');
  if (r.status === 503 || r.status === 429 || /validateCaptcha|api-services-support@amazon\.com/.test(html)) {
    throw new Stop('Amazon is limiting requests (captcha or slow-down page). Stopped; the next run continues.');
  }
  if (!r.ok) return { url, error: `HTTP ${r.status}` };
  return { url, html: trimPage(html) };
}

// Only these fields of each package leave this computer. The answer also holds the buyer's name and
// address: never sent.
const sellerPackages = (order) => ((order && order.packages) || []).map((p) => {
  const m = p.MssDetails || {};
  const d = m.Dimensions || {};
  return {
    id: p.PackageId,
    carrier: p.Carrier,
    type: m.PackageType,
    dims: { l: d.Length, w: d.Width, h: d.Height, unit: d.Unit },
    weight: (m.PackageWeight || []).map((x) => ({ value: x.Value, unit: x.Unit })),
    label: m.MssCharges && m.MssCharges.ShippingLabel ? m.MssCharges.ShippingLabel.Amount : undefined,
    items: (p.PackageItems || []).map((i) => ({ asin: i.ASIN, qty: i.QuantityShipped ?? i.QuantityOrdered })),
  };
});

async function readSeller(url) {
  const r = await fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } });
  if (/\/ap\/signin|\/signin/.test(r.url) || r.status === 401 || r.status === 403) throw new Stop('Signed out of Seller Central in this Chrome profile');
  if (r.status === 429 || r.status === 503) throw new Stop('Seller Central is limiting requests. Stopped; the next run continues.');
  if (!r.ok) return { url, error: `HTTP ${r.status}` };
  const text = await r.text();
  let j;
  try {
    j = JSON.parse(text);
  } catch {
    throw new Stop('Seller Central sent a page, not order data (signed out?)');
  }
  if (!j || !j.order) return { url, error: 'no order in the answer' };
  return { url, packages: sellerPackages(j.order) };
}

async function readWalmart(url) {
  const r = await fetch(url, { credentials: 'include' });
  const html = await r.text();
  if (/\/account\/login|\/signin/.test(r.url)) throw new Stop('Signed out of Walmart in this Chrome profile');
  if (r.status === 429 || r.status === 412 || /px-captcha|Robot or human/i.test(html)) {
    throw new Stop('Walmart is limiting requests (robot check). Stopped; the next run continues.');
  }
  const m = html.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
  let order = null;
  try {
    const data = m ? ((((JSON.parse(m[1]) || {}).props || {}).pageProps || {}).initialData || {}).data : null;
    order = data && data.order ? data.order : null;
  } catch {
    order = null;
  }
  return { url, order };
}

async function run(cfg) {
  if (!cfg.token) return { ok: false, error: 'Add your Boxloft sync token in the extension popup' };
  const count = { amazon: 0, seller: 0, walmart: 0 };
  const stopped = {};               // site -> reason; a stopped site gets no more fetches this run
  let state = null;
  let results = [];
  let walmartStarted = false;
  for (let guard = 0; guard < 2000; guard++) {
    const stop = Object.keys(stopped).length ? stopped : undefined;
    const step = await api(cfg, 'POST', '/api/inbound/v2/step', { state, results, stop, device: cfg.device });
    const s = step.summary || {};
    chrome.runtime.sendMessage({ type: 'sync-progress', step: s.step, found_new: s.found_new, read: s.checked, sent: s.sent, pages: s.pages }).catch(() => {});
    if (step.done) return { ...s, ...(Object.keys(stopped).length ? { stopped } : {}) };
    state = step.state;
    results = [];
    const urls = (step.fetch || []).slice(0, MAX_PER_STEP).map(String);
    const bySite = { amazon: [], seller: [], walmart: [] };
    for (const url of urls) {
      const site = siteOf(url);
      if (!site) results.push({ url, refused: 'not on the allowlist' });
      else if (stopped[site]) results.push({ url, refused: `stopped: ${stopped[site]}` });
      else if (count[site] >= MAX_PAGES[site]) {
        stopped[site] = `page cap (${MAX_PAGES[site]}) reached; the next run continues`;
        results.push({ url, refused: stopped[site] });
      } else bySite[site].push(url);
    }
    try {
      for (let i = 0; i < bySite.amazon.length; i += AMAZON_PARALLEL) {
        const group = bySite.amazon.slice(i, i + AMAZON_PARALLEL);
        count.amazon += group.length;
        results.push(...(await Promise.all(group.map((u) => readAmazon(u).catch((e) => {
          if (e instanceof Stop) throw e;
          return { url: u, error: String(e && e.message || e) };
        })))));
        await pace.amazon();
      }
    } catch (e) {
      if (!(e instanceof Stop)) throw e;
      stopped.amazon = e.message;
    }
    try {
      for (const u of bySite.seller) {
        count.seller++;
        try {
          results.push(await readSeller(u));
        } catch (e) {
          if (e instanceof Stop) throw e;
          results.push({ url: u, error: String(e && e.message || e) });
        }
        await pace.seller();
      }
    } catch (e) {
      if (!(e instanceof Stop)) throw e;
      stopped.seller = e.message;
    }
    try {
      for (const u of bySite.walmart) {
        if (walmartStarted) await pace.walmart();
        walmartStarted = true;
        count.walmart++;
        try {
          results.push(await readWalmart(u));
        } catch (e) {
          if (e instanceof Stop) throw e;
          results.push({ url: u, error: String(e && e.message || e) });
        }
      }
    } catch (e) {
      if (!(e instanceof Stop)) throw e;
      stopped.walmart = e.message;
    }
  }
  return { ok: false, error: 'Run did not finish (too many steps)' };
}

// A full backfill can take longer than Chrome lets one request to the background worker wait
// (about 5 minutes), so answer at once and report back with messages, which also wake the worker.
chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg.type !== 'run-sync') return false;
  reply({ started: true });
  const done = (result) => chrome.runtime.sendMessage({ type: 'sync-done', result }).catch(() => {});
  run(msg.cfg || {}).then(done, (e) => done({ ok: false, error: String(e && e.message || e) }));
  return false;
});
