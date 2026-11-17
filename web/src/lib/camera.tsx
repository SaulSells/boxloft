import { useEffect, useRef, useState } from 'react';
import { X, Camera } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useScanBus } from '@/lib/scan';

/*
 * Phone-camera scanning. Uses the browser's BarcodeDetector where it exists (Chrome on Android)
 * and ZXing everywhere else (iPhone Safari). Stays open for many scans; the same code is ignored
 * for 1.5 s so one label is not read twice.
 */

type Detector = { detect: (src: CanvasImageSource) => Promise<{ rawValue: string }[]> };

// Alex 11-11 "took a while to scan": ZXing (iPhone) tried only 2 frames a second at the camera's default size.
// Now: 1280x720 frames, ~10 tries a second, only the barcode kinds we use, and 1.5x zoom where the phone allows it
// (hold it farther back, where iPhone lenses focus sharply, and the barcode still fills the box).
const VIDEO: MediaTrackConstraints = { facingMode: 'environment', width: { ideal: 1280 }, height: { ideal: 720 } };

async function sharpen(v: HTMLVideoElement | null) {
  const track = (v?.srcObject as MediaStream | null)?.getVideoTracks()[0];
  if (!track?.getCapabilities) return;
  const caps = track.getCapabilities() as MediaTrackCapabilities & { zoom?: { min: number; max: number }; focusMode?: string[] };
  const adv: Record<string, unknown> = {};
  if (caps.focusMode?.includes('continuous')) adv.focusMode = 'continuous';
  if (caps.zoom && caps.zoom.max >= 1.5) adv.zoom = 1.5;
  if (Object.keys(adv).length) {
    try { await track.applyConstraints({ advanced: [adv as MediaTrackConstraintSet] }); } catch { /* not supported: keep defaults */ }
  }
}

// Alex 11-11: iPhone Safari asked "Allow camera?" every time the scanner opened, because closing it stopped the
// camera and opening asked for a new one. Now the page keeps one camera stream: the scanner gets a copy, closing
// stops only the copy, and the kept stream (switched off) ends after 5 idle minutes or when the app is put away.
let held: MediaStream | null = null;
let idle: ReturnType<typeof setTimeout> | undefined;
function dropHeld() {
  clearTimeout(idle);
  held?.getTracks().forEach((t) => t.stop());
  held = null;
}
if (typeof document !== 'undefined') document.addEventListener('visibilitychange', () => { if (document.hidden) dropHeld(); });
async function openStream(): Promise<MediaStream> {
  clearTimeout(idle);
  if (!held || !held.getVideoTracks().some((t) => t.readyState === 'live')) {
    held = await navigator.mediaDevices.getUserMedia({ video: VIDEO, audio: false });
  }
  const copy = held.clone();
  copy.getTracks().forEach((t) => { t.enabled = true; });
  held.getTracks().forEach((t) => { t.enabled = false; });
  return copy;
}
function letGo(copy: MediaStream | null) {
  copy?.getTracks().forEach((t) => t.stop());
  clearTimeout(idle);
  idle = setTimeout(dropHeld, 15 * 60_000);   // Alex 11-11: 5 min still asked again between scans
}

const IOS = typeof navigator !== 'undefined'
  && (/iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1));

/** Alex 11-11 "the camera still continues to ask us for permission". Safari asks on every new camera stream unless
 *  the site is set to Allow; the kept stream can't outlive a locked phone. So when Safari is about to ask, the scanner
 *  says how to set Allow once. Safari's own answer decides: once it says granted, the hint never shows. */
async function safariWillAsk(): Promise<boolean> {
  if (!IOS || (held && held.getVideoTracks().some((t) => t.readyState === 'live'))) return false;
  try {
    const st = await navigator.permissions.query({ name: 'camera' as PermissionName });
    return st.state === 'prompt';
  } catch {
    return true;   // older Safari has no camera permission query: it asks, so say how to stop it
  }
}

export function CameraScanner({ onClose }: { onClose: () => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const { emit, last } = useScanBus();
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState(false);
  const [askHint, setAskHint] = useState(false);
  // The page needs a tap (pick a row, add an extra): close so its card shows. Only for a scan made while open.
  const opened = useRef(Date.now());
  useEffect(() => {   // toasts move to the top while the camera is open (globals.css), off the Done button
    document.documentElement.dataset.camera = '1';
    return () => { delete document.documentElement.dataset.camera; };
  }, []);
  useEffect(() => {
    if (last?.decide && last.at >= opened.current) onClose();
  }, [last, onClose]);
  // Alex 11-11: a scan that opens another screen (the product, New product, Receive) closes the camera, so that
  // screen shows at once. Pages that take many scans in a row (Stock audit, Count, Move) stay on their screen.
  useEffect(() => {
    const close = () => onClose();
    window.addEventListener('boxloft:navigate', close);
    window.addEventListener('popstate', close);
    return () => {
      window.removeEventListener('boxloft:navigate', close);
      window.removeEventListener('popstate', close);
    };
  }, [onClose]);

  useEffect(() => {
    let stop = false;
    let stream: MediaStream | null = null;
    let zxingControls: { stop: () => void } | null = null;
    const recent = new Map<string, number>();

    const hit = (code: string) => {
      // Every sighting refreshes the time, so a code held in view is sent once: it must leave the frame for 1.5 s
      // before it counts again (red line 11-11: one item held 3 s saved 3 units).
      const now = Date.now();
      const seen = recent.get(code) ?? 0;
      recent.set(code, now);
      if (seen > now - 1500) return;
      setFlash(true);
      setTimeout(() => setFlash(false), 250);
      emit(code);
    };

    (async () => {
      try {
        if (await safariWillAsk()) setAskHint(true);
        const BD = (window as unknown as { BarcodeDetector?: new (o: unknown) => Detector }).BarcodeDetector;
        if (BD) {
          stream = await openStream();
          if (stop) return;
          const v = video.current!;
          v.srcObject = stream;
          await v.play();
          void sharpen(v);
          const det = new BD({ formats: ['code_128', 'qr_code', 'ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_39', 'data_matrix'] });
          const loop = async () => {
            if (stop) return;
            try {
              const found = await det.detect(v);
              for (const f of found) hit(f.rawValue);
            } catch {
              // frame not ready
            }
            setTimeout(loop, 80);
          };
          loop();
        } else {
          const { BrowserMultiFormatReader, BarcodeFormat: F } = await import('@zxing/browser');
          // Hint 2 = POSSIBLE_FORMATS: skip PDF417, Aztec, MaxiCode and the rest on every frame.
          const hints = new Map<number, unknown>([[2, [F.EAN_13, F.UPC_A, F.CODE_128, F.EAN_8, F.UPC_E, F.QR_CODE, F.CODE_39, F.DATA_MATRIX]]]);
          const reader = new BrowserMultiFormatReader(hints as never, { delayBetweenScanAttempts: 100, delayBetweenScanSuccess: 300 });
          stream = await openStream();
          if (stop) return;
          zxingControls = await reader.decodeFromStream(
            stream,
            video.current!,
            (result) => {
              if (result) hit(result.getText());
            },
          );
          if (stop) zxingControls.stop();
          else void sharpen(video.current);
        }
      } catch (e) {
        setError(!window.isSecureContext ? 'The camera needs the HTTPS address. Type the code instead.'
          : e instanceof Error && e.name === 'NotAllowedError'
            ? (IOS ? 'Safari said no to the camera. Settings app → Apps → Safari → Camera → Allow, then open the camera again.'
              : 'Camera permission was denied. Allow it in the browser settings.')
            : 'Could not start the camera.');
      }
    })();

    return () => {
      stop = true;
      zxingControls?.stop();
      letGo(stream);
    };
  }, [emit]);

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black">
      <div className="flex items-center justify-between p-3 text-white">
        <span className="flex items-center gap-2 text-sm font-medium"><Camera className="h-4 w-4" /> Point at a barcode</span>
        <Button size="icon" variant="ghost" className="text-white hover:bg-white/10" onClick={onClose} aria-label="Close camera">
          <X className="h-6 w-6" />
        </Button>
      </div>
      <div className="relative flex-1 overflow-hidden">
        <video ref={video} className="h-full w-full object-cover" playsInline muted />
        <div className={`pointer-events-none absolute inset-x-8 top-1/2 h-40 -translate-y-1/2 rounded-xl border-4 transition-colors ${flash ? 'border-green-400' : 'border-white/70'}`} />
        {error && <p className="absolute inset-x-4 top-4 rounded bg-red-600 p-3 text-sm text-white">{error}</p>}
        {askHint && !error && (
          <div className="absolute inset-x-3 bottom-3 rounded-lg bg-white/95 p-3 text-sm text-black shadow-lg">
            <p><b>Tired of "Allow camera?"</b> Safari asks again each time the phone locks. Set it once:</p>
            <p className="mt-1">Settings app → <b>Apps</b> → <b>Safari</b> → <b>Camera</b> → <b>Allow</b>.</p>
            <p className="mt-1 text-xs text-black/70">Only for this site: in Safari, the page menu at the left of the address → Website Settings → Camera → Allow.</p>
            <button type="button" className="mt-2 text-xs underline" onClick={() => setAskHint(false)}>Hide</button>
          </div>
        )}
      </div>
      <div className="min-h-16 p-4 text-center text-white">
        {last ? (
          <span className={last.ok === false ? 'text-red-400' : last.ok === 'next' ? 'text-amber-300' : last.ok ? 'text-green-400' : ''}>
            {last.text ? <span className="block text-base font-semibold">{last.text}</span> : null}
            <span className={last.text ? 'text-xs opacity-80' : ''}>Last: {last.code}</span>
          </span>
        ) : (
          'Fill the box with the barcode. Blurry? Move back a little.'
        )}
        <Button className="mt-3 w-full" variant="secondary" onClick={onClose}>Done</Button>
      </div>
    </div>
  );
}
