import { useEffect, useState } from 'react';

/* The box being filled (flow audit 11-12, redesign 1). Scanning or opening a box sets it, and it stays across pages
 * and saves until another box is scanned or it is cleared. An item scan anywhere goes into it at once. It is per
 * phone (localStorage), so Alex's desk computer never puts an item into the box Sam is filling. After STALE_MS with
 * no save, the next item scan asks "Still A36?" before it saves, so a box from this morning never catches a scan. */

/* Alex 11-17 ("put items at Alex's house in no box"): it can be a place too, where items sit loose. Then box_id is 0
 * and location_id is set. Use curHolder / curPath, never box_id alone. */
export interface CurrentBox { box_id: number; location_id?: number; name: string; at: number }

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
/** A place with no box (Alex's house, a shelf): item scans go there loose. */
export const setCurrentPlace = (location_id: number, name: string) => write({ box_id: 0, location_id, name, at: Date.now() });
/** The box or place it fills, as the API takes it. */
export const curHolder = (c: CurrentBox): { box_id: number } | { location_id: number } =>
  (c.location_id ? { location_id: c.location_id } : { box_id: c.box_id });
/** Its page: the box page, or the place's page (both have the Add card). */
export const curPath = (c: CurrentBox) => (c.location_id ? `/locations/${c.location_id}` : `/boxes/${c.box_id}`);
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
