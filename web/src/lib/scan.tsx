import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';

/*
 * One scan bus for the whole app. Three inputs feed it:
 *  1. A USB/Bluetooth scanner acting as a keyboard. Its keys arrive < 35 ms apart and end with
 *     Enter, so a global listener can tell it from a person typing, wherever focus is.
 *  2. The phone camera (CameraScanner), for when no scanner is at hand.
 *  3. The manual code box in the header.
 * The most recently mounted handler gets each scan, so a page like Move owns scans while open
 * and the shell's "open whatever was scanned" handler is the fallback.
 */

type Handler = (code: string) => void | Promise<void>;
interface ScanCtx {
  emit: (code: string) => void;
  register: (h: Handler, fallback?: boolean) => () => void;
  /** text: what the scan did, in a few words (the camera view shows it). decide: the page needs a tap, so the
   *  camera closes to show it. */
  last: { code: string; at: number; ok: ScanTone | null; text?: string; decide?: boolean } | null;
  feedback: (ok: ScanTone, text?: string, decide?: boolean) => void;
}

/** true = done, false = wrong, 'next' = right so far, one more step (amber, no shake). */
export type ScanTone = boolean | 'next';

const Ctx = createContext<ScanCtx | null>(null);

const KEY_GAP_MS = 35;
const MIN_LEN = 4;

let audio: AudioContext | null = null;
export function beep(tone: ScanTone) {
  try {
    audio = audio ?? new AudioContext();
    const o = audio.createOscillator();
    const g = audio.createGain();
    const ok = tone !== false;
    o.frequency.value = tone === 'next' ? 880 : ok ? 1320 : 220;
    o.type = ok ? 'sine' : 'square';
    g.gain.value = 0.08;
    o.connect(g).connect(audio.destination);
    o.start();
    o.stop(audio.currentTime + (ok ? 0.08 : 0.35));
    navigator.vibrate?.(ok ? 40 : [80, 60, 80]);
  } catch {
    // no audio on this device
  }
}

export function ScanProvider({ children }: { children: ReactNode }) {
  const handlers = useRef<Handler[]>([]);
  const [last, setLast] = useState<ScanCtx['last']>(null);

  const emit = useCallback((raw: string) => {
    const code = raw.trim();
    if (!code) return;
    setLast({ code, at: Date.now(), ok: null });
    const h = handlers.current[handlers.current.length - 1];
    if (h) void h(code);
  }, []);

  // Pages register on top. The shell's fallback goes to the bottom: React runs a parent's
  // effects after its children's, so plain push order would put the shell above the page.
  const register = useCallback((h: Handler, fallback = false) => {
    if (fallback) handlers.current.unshift(h);
    else handlers.current.push(h);
    return () => {
      handlers.current = handlers.current.filter((x) => x !== h);
    };
  }, []);

  const feedback = useCallback((ok: ScanTone, text?: string, decide?: boolean) => {
    beep(ok);
    setLast((l) => (l ? { ...l, ok, text, decide } : l));
  }, []);

  useEffect(() => {
    let buf = '';
    let lastAt = 0;
    let fast = 0;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const now = performance.now();
      const gap = now - lastAt;
      lastAt = now;
      if (e.key === 'Enter' || e.key === 'Tab') {
        if (buf.length >= MIN_LEN && fast >= buf.length - 1) {
          const target = e.target as HTMLElement | null;
          const inField = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA');
          // A field marked data-scan-input keeps the scan as typed text (e.g. the UPC box on a form).
          if (!(inField && target.dataset.scanInput !== undefined)) {
            e.preventDefault();
            e.stopPropagation();
            if (inField) {
              // Take back the characters the scanner typed into the field.
              const el = target as HTMLInputElement;
              if (el.value.endsWith(buf)) {
                const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value')?.set;
                setter?.call(el, el.value.slice(0, -buf.length));
                el.dispatchEvent(new Event('input', { bubbles: true }));
              }
            }
            emit(buf);
          }
        }
        buf = '';
        fast = 0;
        return;
      }
      if (e.key.length !== 1) return;
      if (gap > KEY_GAP_MS) {
        buf = e.key;
        fast = 0;
      } else {
        buf += e.key;
        fast += 1;
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [emit]);

  return <Ctx.Provider value={{ emit, register, last, feedback }}>{children}</Ctx.Provider>;
}

export function useScanBus() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useScanBus outside ScanProvider');
  return ctx;
}

/** Receive scans while this component is mounted. The handler may change between renders. */
export function useScan(handler: Handler, enabled = true, fallback = false) {
  const { register } = useScanBus();
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => {
    if (!enabled) return;
    return register((c) => ref.current(c), fallback);
  }, [register, enabled, fallback]);
}
