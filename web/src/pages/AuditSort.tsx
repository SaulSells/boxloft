import { isFresh, readCurrentBox, setCurrentBox, touchCurrentBox } from '@/lib/currentBox';
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, ArrowRight, Check, ChevronDown, ChevronUp, PackagePlus, Printer, Tags, Undo2, X } from 'lucide-react';
import { ApiError, get, post } from '@/api/client';
import type { PickWhere } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Thumb } from '@/components/app/common';
import { MarketPicker } from '@/components/app/MarketPicker';
import { useToast } from '@/components/app/toast';
import { useScan, useScanBus } from '@/lib/scan';
import { printLabels, targetLabel } from '@/lib/labels';
import { centsFromInput, kinds, money, plural, units, when } from '@/lib/format';
import { cn } from '@/lib/utils';

/*
 * Sorting mode for the Stock audit (Alex 11-11 12:20 PM: boxes on shelves, no bins). Set the box once (scan its
 * label or type the name written on it), then scan each item. A box name nobody has yet is made here and its 4x6
 * label prints. Open shelving works the same way with a shelf (S2): items sit on it loose.
 * One scan = one unit. An item with one open audit row is recorded at once; anything else asks first.
 */

interface Row {
  id: number; asin: string; line_id: number | null; store: string; title: string; image_url: string | null;
  expected: number; accounted: number; found: number; closed: boolean; status: string; product_id: number | null; score?: number;
}
interface Content { product_id: number; sku: string; title: string; qty: number; photo_id: number | null; image_url: string | null }
interface Target {
  type: 'box' | 'shelf'; id: number; code: string; name: string; scan_code: string; where: string;
  units: number; items: number; contents: Content[]; new?: boolean;
}
interface Hint { listed_on: string[]; letter: string | null; mismatch: boolean }
interface Info {
  code: string; title: string; image_url: string | null; asin: string | null; brand: string;
  product: { id: number; sku: string; title: string } | null; from_catalog: boolean; lookup_error: string;
}
interface Extra { id: number; at: string; code: string; product_id: number; item_id: number | null; qty: number; unit_cost_cents: number | null; bin: string; sku: string; title: string; image_url: string | null; note: string; user_name?: string }
interface Lookup { rows: Row[]; guesses: Row[]; info: Info; in_stock: PickWhere[]; hint: Hint }
type Resp =
  | { kind: 'target'; target: Target }
  | { kind: 'no_target'; name: string; retired: boolean }
  | { kind: 'wrong'; message: string }
  | { kind: 'found'; item: Row; count_id: number; qty: number; repeat: boolean; here: number; target: Target; info: Info | null; hint: Hint; elsewhere: Elsewhere[] }
  | ({ kind: 'need_target' | 'pick' | 'full' | 'not_listed' } & Lookup)
  | { kind: 'extra'; extra: Extra; repeat: boolean; target: Target; extras: { units: number; rows: number } };
interface Elsewhere { location_id: number | null; box_id: number | null; code: string; qty: number; join: boolean }
type Shown = (Resp & { code: string }) | { kind: 'undone'; text: string; code: string }
  | { kind: 'joined'; text: string; code: string; action_id: number };
const DECIDE = ['pick', 'full', 'not_listed'];
// A box or shelf label (SB/SL + 8 digits) or a typed name (A31): allowed while a card waits, so a box can be set first.
const looksLikeTarget = (c: string) => /^S[BL]\d{8}$/i.test(c) || /^[A-Z]{1,3}-?\d{1,4}$/i.test(c);

type Cur = { type: 'box' | 'shelf'; id: number };
const KEY = 'boxloft-audit-target';
function readCur(): Cur | null {
  // One box being filled across the app (flow audit 11-12): the box page and this bar share it.
  const cb = readCurrentBox();
  if (cb && isFresh(cb)) return { type: 'box', id: cb.box_id };
  try {
    const m = /^(box|shelf):(\d+)$/.exec(localStorage.getItem(KEY) ?? '');
    return m ? { type: m[1] as Cur['type'], id: Number(m[2]) } : null;
  } catch { return null; }
}
function saveCur(c: Cur | null) {
  try { if (c) localStorage.setItem(KEY, `${c.type}:${c.id}`); else localStorage.removeItem(KEY); } catch { /* private mode */ }
}
const holder = (c: Cur | null) => (!c ? {} : c.type === 'box' ? { box_id: c.id } : { location_id: c.id });
const same = (a: Cur | null, t: Target | undefined) => !!a && !!t && a.type === t.type && a.id === t.id;
const newScanId = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
const where = (w: PickWhere) => (w.box_id ? (w.box_name ? w.box_name.toUpperCase() : `Box ${w.box_id}`) : w.location_code);

function HintLine({ hint, here }: { hint: Hint; here: string }) {
  if (!hint.listed_on.length) return null;
  const text = `Listed on ${hint.listed_on.join(', ')}${hint.letter ? `: ${hint.letter === 'W' ? 'a' : 'an'} ${hint.letter} box` : ''}`;
  if (!hint.mismatch) return <div className="text-xs text-muted-foreground">{text}</div>;
  return (
    <div className="flex items-start gap-1.5 rounded-lg bg-warning/10 px-3 py-2 text-sm text-warning-ink">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>{text}. This is box {here}. It was still saved here: fine if you meant it, or Undo and scan the right box.</span>
    </div>
  );
}

function ItemHead({ title, image, sub }: { title: string; image: string | null; sub?: React.ReactNode }) {
  return (
    <div className="flex gap-3">
      <Thumb photoId={null} url={image} />
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="line-clamp-2 text-sm font-medium">{title}</div>
        {sub}
      </div>
    </div>
  );
}

export function SortBar({ onChange }: { onChange: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { feedback } = useScanBus();
  const [curKey, setCurState] = useState<Cur | null>(readCur);
  const [shown, setShown] = useState<Shown | null>(null);
  const [busy, setBusy] = useState(false);
  const [typed, setTyped] = useState('');
  const [offer, setOffer] = useState<{ name: string; retired: boolean } | null>(null);
  const [open, setOpen] = useState<'none' | 'contents' | 'extras'>('none');
  const [boxesDialog, setBoxesDialog] = useState(false);
  const [cost, setCost] = useState('');
  const [name, setName] = useState('');
  const [more, setMore] = useState('');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // An item scanned before any box was set: the next box scan puts it there (no second scan of the item).
  const pending = useRef<string | null>(null);

  const setCur = (c: Cur | null) => { setCurState(c); saveCur(c); };
  const tq = useQuery({
    queryKey: ['audit-target', curKey?.type, curKey?.id], enabled: !!curKey, retry: false,
    queryFn: () => get<Target>(`/api/audit-sort/target?${curKey!.type === 'box' ? 'box_id' : 'location_id'}=${curKey!.id}`),
  });
  useEffect(() => {
    if (!tq.error) return;
    // Only a box the server refused (retired, gone) is cleared; a Wi-Fi blip keeps the box set.
    const st = (tq.error as { status?: number }).status;
    if (st === 400 || st === 404) { toast(`${(tq.error as Error).message} Scan a box label.`, { tone: 'error' }); setCur(null); }
  }, [tq.error]); // eslint-disable-line react-hooks/exhaustive-deps
  const extras = useQuery({ queryKey: ['audit-extras'], queryFn: () => get<{ rows: Extra[]; units: number; no_cost: number }>('/api/audit-sort/extras') });
  const cur = same(curKey, tq.data) ? tq.data! : null;

  // The audit list is big: reload it once the scanning pauses, not after every unit.
  const changed = (t?: Target) => {
    if (t) qc.setQueryData(['audit-target', t.type, t.id], t);
    touchCurrentBox();
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(onChange, 2500);
  };
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const targetSet = (t: Target) => {
    qc.setQueryData(['audit-target', t.type, t.id], t);
    const c = { type: t.type, id: t.id };
    setCur(c); setOpen('none'); setOffer(null);
    if (t.type === 'box') setCurrentBox(t.id, t.code);
    setShown((p) => (p && DECIDE.includes(p.kind) ? p : null));
    feedback(true, `${t.type === 'box' ? 'Box' : 'Shelf'} ${t.code}`);
    const held = pending.current;
    pending.current = null;
    if (held) void send('/api/audit-sort/scan', { code: held, ...holder(c) }, held, c);
  };

  const show = (r: Resp, code: string) => {
    setCost(''); setName(''); setMore('');
    if (r.kind === 'target') { targetSet(r.target); return; }
    if (r.kind === 'no_target') { setOffer({ name: r.name, retired: r.retired }); feedback('next', `No box ${r.name} yet`); return; }
    setShown({ ...r, code });
    if (r.kind !== 'need_target') pending.current = null;
    if (r.kind === 'wrong') { feedback(false, r.message); toast(r.message, { tone: 'error' }); return; }
    if (r.kind === 'found') {
      feedback(true, `${r.repeat ? 'Already saved: ' : ''}Found ${r.qty ?? 1} → ${r.target.code} (${r.item.accounted} of ${r.item.expected})`);
      changed(r.target); return;
    }
    if (r.kind === 'extra') { feedback(true, `Extra 1 → ${r.target.code}`); changed(r.target); void qc.invalidateQueries({ queryKey: ['audit-extras'] }); return; }
    if (r.kind === 'need_target') {
      pending.current = code;
      feedback('next', 'Now scan the box it goes in');
      return;
    }
    // needs a person: low beep, the camera closes, and the card is at the top of the page
    feedback(false, 'Check the screen', true);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const inflight = useRef(false);
  const send = async (path: string, body: object, code: string, to: Cur | null = curKey) => {
    setBusy(true);
    inflight.current = true;
    // One id per save, kept on resends: when the reply is lost (Wi-Fi blip) the save may have landed, and the
    // server answers a resent id with the first save instead of adding a second unit.
    const req = { scan_id: newScanId(), ...holder(to), ...body };
    try {
      let r: Resp | undefined;
      for (let i = 0; i < 3 && r === undefined; i++) {
        try { r = await post<Resp>(path, req); }
        catch (e) {
          if (e instanceof ApiError) throw e;
          if (i === 2) throw new Error('No connection. Not sure this saved: check the box count before you scan it again.');
          await new Promise((ok) => setTimeout(ok, 700 * (i + 1)));
        }
      }
      show(r!, code);
    }
    catch (e) {
      const msg = (e as Error).message;
      feedback(false, msg);
      toast(msg, { tone: 'error' });
      if (/retired|not in Boxloft|no longer in use/.test(msg)) setCur(null);
    } finally { setBusy(false); inflight.current = false; }
  };

  useScan((code) => {
    // The last scan has no answer yet (it may become a card that needs a tap): never let the next one replace it.
    if (inflight.current && !looksLikeTarget(code)) {
      feedback(false, 'Wait for the last scan to finish, then scan again.', true);
      return;
    }
    // A card waiting for a choice is never dropped by the next item: say so, keep the card.
    if (shown && DECIDE.includes(shown.kind) && !looksLikeTarget(code)) {
      const msg = 'First finish the card on screen: pick one, add it, or Skip.';
      feedback(false, msg, true);
      toast(msg, { tone: 'error' });
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }
    return send('/api/audit-sort/scan', { code }, code);
  });

  const pickFound = (row: Row, code: string, qty = 1) => send('/api/audit-sort/found', { item_id: row.id, code, qty }, code);
  const join = async (s: Extract<Shown, { kind: 'found' }>, to: Elsewhere) => {
    setBusy(true);
    try {
      const from = s.target.type === 'box' ? { from_box_id: s.target.id } : { from_location_id: s.target.id };
      const dst = to.box_id ? { to_box_id: to.box_id } : { to_location_id: to.location_id };
      const r = await post<{ action_id: number; target: Target; to: string; qty: number }>('/api/audit-sort/join',
        { product_id: s.item.product_id, qty: s.qty ?? 1, ...from, ...dst });
      qc.setQueryData(['audit-target', r.target.type, r.target.id], r.target);
      const text = `Moved ${r.qty} to ${r.to}, with the same item. Put it in ${r.to}.`;
      setShown({ kind: 'joined', text, code: s.code, action_id: r.action_id });
      feedback(true, text);
      changed();
    } catch (e) { toast((e as Error).message, { tone: 'error' }); } finally { setBusy(false); }
  };
  // The typed words name an extra only when the lookup gave no title (they are search words, not a better name).
  const addExtra = (code: string, itemId?: number) => send('/api/audit-sort/extra', {
    code, item_id: itemId ?? null, cost_cents: centsFromInput(cost),
    title: shown?.kind === 'not_listed' && !shown.info.title ? name.trim() : '',
  }, code);
  const search = useQuery({
    queryKey: ['audit-sort-search', name.trim().toLowerCase()],
    enabled: shown?.kind === 'not_listed' && name.trim().length >= 3, staleTime: 5000,
    queryFn: () => get<{ rows: Row[] }>(`/api/audit-sort/search?q=${encodeURIComponent(name.trim())}`),
  });

  const undo = async () => {
    if (!shown) return;
    setBusy(true);
    try {
      if (shown.kind === 'found') {
        await post(`/api/audit/${shown.item.id}/undo/${shown.count_id}`);
        setShown({ kind: 'undone', text: `Undone: ${shown.qty ?? 1} × ${shown.item.title.slice(0, 60)} is out of ${shown.target.code} again.`, code: shown.code });
      } else if (shown.kind === 'joined') {
        await post('/api/undo', { action_id: shown.action_id, note: 'Audit: undo put-with' });
        setShown({ kind: 'undone', text: 'Undone: it is back in this box.', code: shown.code });
      } else if (shown.kind === 'extra') {
        await post(`/api/audit-sort/extras/${shown.extra.id}/undo`);
        setShown({ kind: 'undone', text: `Undone: the extra ${shown.extra.title.slice(0, 60)} is out of ${shown.target.code} again.`, code: shown.code });
        void qc.invalidateQueries({ queryKey: ['audit-extras'] });
      }
      void qc.invalidateQueries({ queryKey: ['audit-target'] });
      changed();
      toast('Undone');
    } catch (e) { toast((e as Error).message, { tone: 'error' }); } finally { setBusy(false); }
  };

  const setTypedTarget = async () => {
    const n = typed.trim();
    if (!n) return;
    try {
      const r = await post<Resp>('/api/targets/find', { name: n });
      setTyped('');
      show(r, n);
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  const make = async (what: 'box' | 'shelf') => {
    if (!offer) return;
    setBusy(true);
    try {
      const t = await post<Target>(`/api/targets/make-${what}`, { name: offer.name });
      if (t.new) await printLabels([targetLabel(t)]);
      toast(t.new ? `${what === 'box' ? 'Box' : 'Shelf'} ${t.code} made. Its label is printing (or waiting in the print queue). Stick it on, then scan items in.`
        : `${t.code} is set.`);
      void qc.invalidateQueries({ queryKey: ['boxes'] });
      targetSet(t);
    } catch (e) { toast((e as Error).message, { tone: 'error' }); } finally { setBusy(false); }
  };

  const word = cur?.type === 'shelf' ? 'shelf' : 'box';
  const status = (() => {
    if (!shown) return null;
    if (shown.kind === 'found') return <><Check className="h-4 w-4 shrink-0 text-success-ink" /><span className="min-w-0 flex-1 truncate">{shown.repeat ? 'Already saved: ' : ''}Found {shown.qty ?? 1} → {shown.target.code} ({shown.item.accounted} of {shown.item.expected})</span></>;
    if (shown.kind === 'joined') return <><Check className="h-4 w-4 shrink-0 text-success-ink" /><span className="min-w-0 flex-1 truncate">{shown.text}</span></>;
    if (shown.kind === 'need_target') return <><ArrowRight className="h-4 w-4 shrink-0 text-warning-ink" /><span className="min-w-0 flex-1 truncate">Now scan the box it goes in</span></>;
    if (shown.kind === 'extra') return <><PackagePlus className="h-4 w-4 shrink-0 text-success-ink" /><span className="min-w-0 flex-1 truncate">Extra 1 → {shown.target.code}: {shown.extra.title}</span></>;
    if (shown.kind === 'undone') return <span className="min-w-0 flex-1 truncate text-muted-foreground">{shown.text}</span>;
    if (shown.kind === 'wrong') return <><AlertTriangle className="h-4 w-4 shrink-0 text-destructive" /><span className="min-w-0 flex-1 truncate text-destructive">{shown.message}</span></>;
    return <><AlertTriangle className="h-4 w-4 shrink-0 text-warning-ink" /><span className="min-w-0 flex-1 truncate">Check the card below</span></>;
  })();

  return (
    <>
      <div className="sticky top-14 z-20 -mx-3 border-b bg-background/95 px-3 py-2 backdrop-blur sm:mx-0 sm:rounded-lg sm:border">
        <div className="flex items-center gap-2">
          <div className="min-w-0">
            <div className="text-xs font-medium text-muted-foreground">{cur?.type === 'shelf' ? 'Shelf (loose)' : 'Box'}</div>
            <div className={cn('text-2xl font-bold leading-none', !curKey && 'text-base font-medium text-muted-foreground')}>
              {cur ? cur.code : curKey ? '…' : 'Scan a box label'}
            </div>
          </div>
          {cur && (
            <button className="ml-1 flex items-center gap-1 rounded-md border px-2 py-1 text-xs" onClick={() => setOpen(open === 'contents' ? 'none' : 'contents')}>
              {units(cur.units)} · {kinds(cur.items)} {open === 'contents' ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            </button>
          )}
          <div className="ml-auto flex items-center gap-1">
            <Button size="sm" variant="outline" className="h-9" onClick={() => setBoxesDialog(true)}><Tags className="mr-1 h-4 w-4" />Boxes</Button>
            {curKey && <Button size="icon" variant="ghost" className="h-9 w-9" aria-label="Clear the box" onClick={() => { setCur(null); setShown(null); setOpen('none'); }}><X className="h-4 w-4" /></Button>}
          </div>
        </div>
        <MarketPicker className="mt-2" typed={false} current={cur?.type === 'box' ? cur.code : null}
          onPick={(b) => { setTyped(''); void send('/api/audit-sort/scan', { code: b.code }, b.code, null); }} />
        <div className="mt-2 flex items-center gap-2">
          <Input value={typed} onChange={(e) => { setTyped(e.target.value); setOffer(null); }} placeholder="Box with a handwritten name? Type it: A31"
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void setTypedTarget(); } }} className="h-9 flex-1" autoCapitalize="characters" />
          <Button size="sm" variant="outline" className="h-9" onClick={setTypedTarget} disabled={!typed.trim()}>Set</Button>
        </div>
        {offer && (
          <div className="mt-2 space-y-2 rounded-lg border border-primary/40 bg-primary/5 p-2">
            <div className="text-sm">{offer.retired ? `Box ${offer.name} was retired. Bring it back with a new label?` : `No box named ${offer.name} in Boxloft yet.`}</div>
            <div className="flex flex-wrap gap-2">
              <Button className="h-10" disabled={busy} onClick={() => make('box')}><Printer className="mr-1.5 h-4 w-4" />Make box {offer.name} + print its 4x6 label</Button>
              {!offer.retired && <Button className="h-10" variant="outline" disabled={busy} onClick={() => make('shelf')}>It's a shelf (loose items)</Button>}
              <Button className="h-10" variant="ghost" onClick={() => setOffer(null)}>Cancel</Button>
            </div>
          </div>
        )}
        {status && (
          <div className="mt-2 flex items-center gap-2 text-sm">
            {status}
            {(shown?.kind === 'found' || shown?.kind === 'extra' || shown?.kind === 'joined') && (
              <Button size="sm" variant="outline" className="h-8 shrink-0" disabled={busy} onClick={undo}><Undo2 className="mr-1 h-3.5 w-3.5" />Undo</Button>
            )}
          </div>
        )}
        <div className="mt-1 text-xs">
          <button className="underline" onClick={() => setOpen(open === 'extras' ? 'none' : 'extras')}>
            Found, not on the list: {plural(extras.data?.units ?? 0, 'unit')}
          </button>
        </div>
      </div>

      {open === 'contents' && cur && (
        <Card>
          <CardContent className="space-y-2 p-3">
            <div className="text-sm font-medium">In {word} {cur.code} now: {units(cur.units)}, {kinds(cur.items)}</div>
            <div className="text-xs text-muted-foreground">{cur.where}</div>
            {!cur.contents.length && <p className="text-sm text-muted-foreground">Empty.</p>}
            {cur.contents.map((c) => (
              <div key={c.product_id} className="flex items-center gap-2 text-sm">
                <Thumb photoId={c.photo_id} url={c.image_url} size="dense" />
                <span className="min-w-0 flex-1 truncate">{c.title}</span>
                <b className="tabular-nums">{c.qty}</b>
              </div>
            ))}
            <Button size="sm" variant="outline" className="h-9" onClick={() => printLabels([targetLabel(cur)])}><Printer className="mr-1 h-4 w-4" />Print {cur.code} label again</Button>
          </CardContent>
        </Card>
      )}

      {open === 'extras' && <ExtrasList rows={extras.data?.rows ?? []} noCost={extras.data?.no_cost ?? 0} onUndo={() => { void qc.invalidateQueries({ queryKey: ['audit-extras'] }); void qc.invalidateQueries({ queryKey: ['audit-target'] }); changed(); }} />}

      {shown && shown.kind === 'found' && (
        <Card className="border-success/50">
          <CardContent className="space-y-2 p-3">
            <ItemHead title={shown.item.title || shown.item.asin} image={shown.item.image_url ?? shown.info?.image_url ?? null}
              sub={<>
                <div className="text-xl font-bold">Found {shown.qty ?? 1} → {shown.target.code}</div>
                <div className="text-sm">{shown.item.accounted} of {shown.item.expected} accounted for · {shown.here} in {shown.target.code} now</div>
              </>} />
            <HintLine hint={shown.hint} here={shown.target.code} />
            {shown.elsewhere?.some((e) => e.join) && (
              <div className="space-y-1.5 rounded-lg bg-info/10 px-3 py-2 text-sm">
                <div>Same item already in {shown.elsewhere.map((e) => `${e.code} (${e.qty})`).join(', ')}. Keep them together?</div>
                <div className="flex flex-wrap gap-2">
                  {shown.elsewhere.filter((e) => e.join).slice(0, 2).map((e) => (
                    <Button key={e.code} size="sm" variant="outline" className="h-9" disabled={busy} onClick={() => join(shown, e)}>
                      Put {shown.qty > 1 ? `these ${shown.qty}` : 'it'} in {e.code} instead
                    </Button>
                  ))}
                </div>
              </div>
            )}
            {shown.item.accounted < shown.item.expected && (
              <div className="flex items-center gap-2 border-t pt-2">
                <Button className="h-10 shrink-0" variant="outline" disabled={busy} onClick={() => pickFound(shown.item, shown.code)}>+1 more</Button>
                <Input inputMode="numeric" value={more} onChange={(e) => setMore(e.target.value.replace(/\D/g, ''))}
                  placeholder="or how many more?" className="h-10 min-w-0 flex-1"
                  onKeyDown={(e) => { if (e.key === 'Enter' && Number(more) > 0) { e.preventDefault(); void pickFound(shown.item, shown.code, Number(more)); } }} />
                <Button className="h-10 shrink-0" disabled={busy || !(Number(more) > 0)} onClick={() => pickFound(shown.item, shown.code, Number(more))}>Add</Button>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {shown && (shown.kind === 'need_target' || shown.kind === 'pick') && (
        <Card className="border-warning/50">
          <CardContent className="space-y-3 p-3">
            <div className="text-sm font-medium">{shown.kind === 'need_target' ? 'Which box does it go in? Scan the box label (or type its name above). This item is saved into it.' : `${shown.rows.length} rows on the list match. Which one is it?`}</div>
            {shown.rows.map((r) => (
              <div key={r.id} className="space-y-2 rounded-md border p-2">
                <ItemHead title={r.title || r.asin} image={r.image_url} sub={<div className="text-xs text-muted-foreground">{r.line_id ? `Bought at ${r.store}` : r.asin} · {r.accounted} of {r.expected} accounted for{r.closed ? ' · marked missing' : ''}</div>} />
                {shown.kind === 'pick' && <Button className="h-10 w-full" disabled={busy || !curKey} onClick={() => pickFound(r, shown.code)}><Check className="mr-1.5 h-4 w-4" />This one: found 1 → {cur?.code ?? 'box'}</Button>}
              </div>
            ))}
            <HintLine hint={shown.hint} here={cur?.code ?? ''} />
          </CardContent>
        </Card>
      )}

      {shown && shown.kind === 'full' && (
        <Card className="border-warning/50">
          <CardContent className="space-y-3 p-3">
            {shown.rows.map((r) => (
              <ItemHead key={r.id} title={r.title || r.asin} image={r.image_url} sub={<>
                <div className="text-lg font-bold">All {r.expected} already accounted for. Another one?</div>
                <div className="text-xs text-muted-foreground">{r.found} found{shown.in_stock.length ? `: ${shown.in_stock.map((w) => `${w.qty} in ${where(w)}`).join(', ')}` : ''}. One more means you have more than the list says you bought.</div>
              </>} />
            ))}
            <div className="flex flex-wrap gap-2">
              <Button className="h-10" disabled={busy || !curKey} onClick={() => addExtra(shown.code, shown.rows[0]!.id)}><PackagePlus className="mr-1.5 h-4 w-4" />Yes: add 1 to {cur?.code ?? 'the box'} as extra</Button>
              <Button className="h-10" variant="ghost" onClick={() => setShown(null)}>No, I scanned it twice</Button>
            </div>
            {!curKey && <p className="text-xs text-destructive">Scan a box label first.</p>}
          </CardContent>
        </Card>
      )}

      {shown && shown.kind === 'not_listed' && (
        <Card className="border-warning/50">
          <CardContent className="space-y-3 p-3">
            <ItemHead title={shown.info.title || `Code ${shown.code}`} image={shown.info.image_url} sub={<>
              <div className="text-lg font-bold">{shown.info.lookup_error ? "Couldn't look this code up" : 'Not on the audit list by its barcode'}</div>
              <div className="text-xs text-muted-foreground">
                {shown.info.lookup_error
                  ? `${shown.info.lookup_error}. Scan it again in a minute, or find it by name below.`
                  : shown.info.title ? <>Code {shown.code}{shown.info.asin ? ` · ${shown.info.asin}` : ''}</> : 'No product info found for this code.'}
              </div>
            </>} />
            {shown.in_stock.length > 0 && (
              <div className="rounded-md border px-2 py-1.5 text-xs">Boxloft already has {shown.in_stock.map((w) => `${w.qty} in ${where(w)}`).join(', ')}. If this is one of those, it doesn't need adding.</div>
            )}
            {shown.guesses.length > 0 && (
              <div className="space-y-2">
                <div className="text-xs font-medium">Maybe one of these (a guess from the name):</div>
                {shown.guesses.map((g) => <ListPick key={g.id} r={g} busy={busy || !curKey} here={cur?.code} onPick={() => pickFound(g, shown.code)} />)}
              </div>
            )}
            <div className="space-y-1">
              <Label className="text-xs">What is it? A few words find it on the list</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. crocs toddler clog" className="h-10" />
            </div>
            {name.trim().length >= 3 && (
              <div className="space-y-2">
                {search.isFetching && !search.data && <p className="text-xs text-muted-foreground">Searching…</p>}
                {search.data && !search.data.rows.length && <p className="text-xs text-muted-foreground">Nothing open on the list has those words.</p>}
                {search.data?.rows.filter((r) => !shown.guesses.some((g) => g.id === r.id)).map((r) => (
                  <ListPick key={r.id} r={r} busy={busy || !curKey} here={cur?.code} onPick={() => pickFound(r, shown.code)} />
                ))}
              </div>
            )}
            <div className="space-y-2 border-t pt-3">
              <div className="text-xs font-medium">Not on the list at all? Add it as an extra:</div>
              <div className="flex items-end gap-2">
                <div className="w-28 space-y-1"><Label className="text-xs">Cost each (optional)</Label><Input inputMode="decimal" value={cost} onChange={(e) => setCost(e.target.value)} placeholder="$" className="h-10" /></div>
                <Button className="h-10" variant={shown.info.lookup_error ? 'outline' : 'default'} disabled={busy || !curKey} onClick={() => addExtra(shown.code)}><PackagePlus className="mr-1.5 h-4 w-4" />Add 1 to {cur?.code ?? 'the box'}</Button>
                <Button className="h-10" variant="ghost" onClick={() => setShown(null)}>Skip</Button>
              </div>
              {!curKey && <p className="text-xs text-destructive">Scan a box label first.</p>}
              <p className="text-xs text-muted-foreground">An extra goes on the "Found, not on the list" list{name.trim() ? `, named "${name.trim()}"` : ''}. If it matches an order still on its way, it is checked in against that order.</p>
            </div>
          </CardContent>
        </Card>
      )}

      {boxesDialog && <BoxesDialog onClose={() => setBoxesDialog(false)} onRetired={(id) => { if (curKey?.type === 'box' && id === curKey.id) setCur(null); void qc.invalidateQueries({ queryKey: ['audit-target'] }); void qc.invalidateQueries({ queryKey: ['boxes'] }); }} />}
    </>
  );
}

function ListPick({ r, busy, here, onPick }: { r: Row; busy: boolean; here?: string; onPick: () => void }) {
  return (
    <div className="space-y-1.5 rounded-md border border-dashed p-2">
      <ItemHead title={r.title || r.asin} image={r.image_url} sub={
        <div className="text-xs text-muted-foreground">{r.line_id ? `Bought at ${r.store}` : r.asin} · {r.accounted} of {r.expected} accounted for</div>} />
      <Button size="sm" variant="outline" className="h-9 w-full" disabled={busy} onClick={onPick}><Check className="mr-1 h-4 w-4" />That's it: found 1 → {here ?? 'box'}</Button>
    </div>
  );
}

function ExtrasList({ rows, noCost, onUndo }: { rows: Extra[]; noCost: number; onUndo: () => void }) {
  const toast = useToast();
  const undo = async (x: Extra) => {
    try { await post(`/api/audit-sort/extras/${x.id}/undo`); toast('Undone'); onUndo(); } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  return (
    <Card>
      <CardContent className="space-y-2 p-3">
        <div className="text-sm font-medium">Found, not on the list ({plural(rows.length, 'unit')}{noCost ? `, ${noCost} with no cost` : ''})</div>
        {!rows.length && <p className="text-sm text-muted-foreground">None yet.</p>}
        {rows.map((x) => (
          <div key={x.id} className="flex items-center gap-2 text-sm">
            <Thumb photoId={null} url={x.image_url} size="dense" />
            <div className="min-w-0 flex-1">
              <div className="truncate">{x.title}</div>
              <div className="text-xs text-muted-foreground">{x.bin} · {x.unit_cost_cents == null ? 'no cost' : money(x.unit_cost_cents)} · {x.item_id ? 'more than the list' : 'not on the list'} · {when(x.at)}{x.user_name ? ` · ${x.user_name}` : ''}</div>
            </div>
            <Button size="sm" variant="ghost" className="h-8 px-2" onClick={() => undo(x)}><Undo2 className="h-3.5 w-3.5" /></Button>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function BoxesDialog({ onClose, onRetired }: { onClose: () => void; onRetired: (id: number) => void }) {
  const toast = useToast();
  const [names, setNames] = useState('');
  const [busy, setBusy] = useState(false);
  const [retire, setRetire] = useState('');
  const [moveTo, setMoveTo] = useState('');
  const [needsMove, setNeedsMove] = useState<string | null>(null);
  type BoxesResp = { boxes: { id: number; code: string; scan_code: string; where: string; new: boolean }[]; created: number; existing: number; missing: number };
  const call = (create: boolean) => post<BoxesResp>('/api/audit-sort/boxes', { ranges: names, create });
  const make = async () => {
    setBusy(true);
    try {
      const r = await call(true);
      toast(`${plural(r.created, 'new box', 'new boxes')} made${r.existing ? `, ${r.existing} already there` : ''}.`);
      await printLabels(r.boxes.map((b) => targetLabel({ ...b, type: 'box' })));
    } catch (e) { toast((e as Error).message, { tone: 'error' }); } finally { setBusy(false); }
  };
  const doRetire = async () => {
    setBusy(true);
    try {
      const found = await post<BoxesResp>('/api/audit-sort/boxes', { ranges: retire, create: false });
      const b = found.boxes[0];
      if (!b) throw new Error(`There is no box ${retire.trim().toUpperCase()} in use`);
      const out = await post<{ retired: string; moved_units: number; moved_to: { code: string } | null }>(`/api/audit-sort/box/${b.id}/retire`, { move_to: moveTo });
      toast(out.moved_to ? `Moved ${plural(out.moved_units, 'unit')} to ${out.moved_to.code}. Box ${out.retired} is retired.` : `Box ${out.retired} is retired.`);
      onRetired(b.id);
      setRetire(''); setMoveTo(''); setNeedsMove(null);
    } catch (e) {
      const msg = (e as Error).message;
      if (/still holds/.test(msg)) setNeedsMove(msg); else toast(msg, { tone: 'error' });
    } finally { setBusy(false); }
  };
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-h-[92vh] overflow-y-auto">
        <DialogHeader><DialogTitle>Box labels</DialogTitle></DialogHeader>
        <div className="space-y-5">
          <div className="space-y-2">
            <Label>Names written on the boxes</Label>
            <Input value={names} onChange={(e) => setNames(e.target.value)} placeholder="A23, A31, A66, W3" className="h-11" autoCapitalize="characters" />
            <p className="text-xs text-muted-foreground">Each box gets a 4x6 label with its name big and a barcode. A name Boxloft already has prints its label again; nothing is made twice. A = Amazon, E = eBay, W = Walmart, L = local.</p>
            <Button className="h-10" disabled={busy || !names.trim()} onClick={make}><Printer className="mr-1.5 h-4 w-4" />Make + print labels</Button>
          </div>
          <div className="space-y-2 border-t pt-4">
            <Label>Retire a box (damaged or gone)</Label>
            <div className="grid grid-cols-2 gap-2">
              <Input value={retire} onChange={(e) => { setRetire(e.target.value); setNeedsMove(null); }} placeholder="Box, e.g. A12" className="h-10" autoCapitalize="characters" />
              <Input value={moveTo} onChange={(e) => setMoveTo(e.target.value)} placeholder="Move stock to, e.g. A13" className="h-10" autoCapitalize="characters" />
            </div>
            {needsMove && <p className="text-sm text-warning-ink">{needsMove}</p>}
            <Button className="h-10" variant="outline" disabled={busy || !retire.trim()} onClick={doRetire}>
              {moveTo.trim() ? `Move everything to ${moveTo.trim().toUpperCase()}, then retire` : 'Retire'}
            </Button>
            <p className="text-xs text-muted-foreground">An empty box is retired at once. A box with stock needs the box it all moves to; the move is in Activity like any other.</p>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
