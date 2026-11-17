import { useEffect, useState } from 'react';

/* The box being filled (flow audit 11-12, redesign 1). Scanning or opening a box sets it, and it stays across pages
 * and saves until another box is scanned or it is cleared. An item scan anywhere goes into it at once. It is per
 * phone (localStorage), so Alex's desk computer never puts an item into the box Sam is filling. After STALE_MS with
 * no save, the next item scan asks "Still A36?" before it saves, so a box from this morning never catches a scan. */

export interface CurrentBox { box_id: number; name: string; at: number }

const KEY = 'boxloft-current-box';
const EVENT = 'boxloft:current-box';
export const STALE_MS = 20 * 60_000;

export function readCurrentBox(): CurrentBox | null {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || 'null');
    return v && typeof v.box_id === 'number' ? (v as CurrentBox) : null;
  } catch {
    return null;
  }
}

function write(v: CurrentBox | null) {
  try {
    if (v) localStorage.setItem(KEY, JSON.stringify(v));
    else localStorage.removeItem(KEY);
  } catch { /* private mode: the box lasts for this page only */ }
  window.dispatchEvent(new Event(EVENT));
}

export const setCurrentBox = (box_id: number, name: string) => write({ box_id, name, at: Date.now() });
/** A save went into it: it is fresh again. */
export const touchCurrentBox = () => { const c = readCurrentBox(); if (c) write({ ...c, at: Date.now() }); };
export const clearCurrentBox = () => write(null);
export const isFresh = (c: CurrentBox | null) => !!c && Date.now() - c.at < STALE_MS;

export function useCurrentBox(): CurrentBox | null {
  const [v, setV] = useState(readCurrentBox);
  useEffect(() => {
    const f = () => setV(readCurrentBox());
    window.addEventListener(EVENT, f);
    window.addEventListener('storage', f);
    return () => { window.removeEventListener(EVENT, f); window.removeEventListener('storage', f); };
  }, []);
  return v;
}
