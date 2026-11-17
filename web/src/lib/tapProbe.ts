/**
 * Alex 11-12: the header's menu button didn't work in the home-screen web app. No iPhone or simulator can run that
 * here, so the app reports what the phone really does: viewport and safe-area sizes once per load, and every
 * touch or click in the top 160 px (where it landed, what it hit). Home-screen app only, 40 events per load at most.
 * Read on the server: /var/lib/boxloft/client-diag.jsonl.
 */
import { post } from '@/api/client';

type Ev = { type: string; x: number; y: number; hit: string; ms: number };

function hitName(t: EventTarget | null): string {
  const el = t instanceof Element ? t : null;
  if (!el) return '';
  const slot = el.closest('[data-slot], [data-sidebar], button, a, header');
  const s = slot as HTMLElement | null;
  return s ? (s.dataset.slot || s.dataset.sidebar || s.tagName.toLowerCase()) : el.tagName.toLowerCase();
}

function safeTop(): number {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;top:0;left:0;visibility:hidden;padding-top:env(safe-area-inset-top,0px)';
  document.body.appendChild(d);
  const v = parseFloat(getComputedStyle(d).paddingTop) || 0;
  d.remove();
  return v;
}

export function startTapProbe() {
  if (!document.documentElement.classList.contains('standalone')) return;
  const t0 = performance.now();
  const events: Ev[] = [];
  let sent = 0;
  let reports = 0;
  let timer: number | undefined;
  const header = () => document.querySelector('header.app-header')?.getBoundingClientRect();
  const trigger = () => document.querySelector('[data-slot="sidebar-trigger"]')?.getBoundingClientRect();
  const send = () => {
    const h = header();
    const b = trigger();
    const batch = events.splice(0);
    if (reports > 0 && !batch.length) return;
    reports++;
    void post('/api/client-diag', {
      kind: batch.length ? 'taps' : 'load',
      standalone: true,
      ua: navigator.userAgent,
      inner: [window.innerWidth, window.innerHeight],
      screen: [screen.width, screen.height],
      vv: window.visualViewport ? [window.visualViewport.offsetTop, window.visualViewport.height] : null,
      safe_top: safeTop(),
      header: h ? [Math.round(h.top), Math.round(h.bottom)] : null,
      trigger: b ? [Math.round(b.left), Math.round(b.top), Math.round(b.right), Math.round(b.bottom)] : null,
      sidebar_open: !!document.querySelector('[data-mobile="true"][data-state="open"]'),
      events: batch,
    }).catch(() => {});
  };
  const record = (e: Event) => {
    if (sent >= 40) return;
    const p = 'changedTouches' in e ? (e as TouchEvent).changedTouches[0] : (e as MouseEvent);
    if (!p || p.clientY > 160) return;
    sent++;
    events.push({ type: e.type, x: Math.round(p.clientX), y: Math.round(p.clientY), hit: hitName(e.target), ms: Math.round(performance.now() - t0) });
    window.clearTimeout(timer);
    timer = window.setTimeout(send, 2500);
  };
  for (const type of ['touchstart', 'touchend', 'touchcancel', 'click']) {
    window.addEventListener(type, record, { capture: true, passive: true });
  }
  window.setTimeout(send, 3000);
}
