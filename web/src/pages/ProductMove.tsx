import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Minus, MoveRight, Plus, ScanLine, Undo2, X } from 'lucide-react';
import { get, post } from '@/api/client';
import type { Holder, Product, ScanResult, WhereRow } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Empty, HolderPicker, TapAction, tapTile } from '@/components/app/common';
import { MarketPicker } from '@/components/app/MarketPicker';
import { useToast } from '@/components/app/toast';
import { isFresh, touchCurrentBox, useCurrentBox } from '@/lib/currentBox';
import { money } from '@/lib/format';
import { boxName, isShelf, placeText } from '@/lib/places';
import { Link } from '@/lib/router';
import { useScan, useScanBus } from '@/lib/scan';
import { cn } from '@/lib/utils';

/*
 * Move an item from its own page (Alex 11-13: "should be able to move items from this page"). One item can sit in
 * several boxes and loose on shelves, so each place has its own Move, and the form always names the place it takes
 * from. Tap Move, say how many, then tap or scan where they go. One save, with Undo. "Move all into one box" puts
 * every pile in one place in one save (one Undo for all).
 */

const holderOf = (w: WhereRow): Holder => (w.box_id ? { box_id: w.box_id } : { location_id: w.location_id });
const hkey = (h: Holder) => ('box_id' in h ? `b${h.box_id}` : `l${h.location_id}`);
const rowKey = (w: WhereRow) => `${w.box_id}-${w.location_id}-${w.unit_id}`;
const placeOf = (w: WhereRow) => ({ code: w.location_code, site: w.location_site, name: w.location_name });
/** The building a box sits in ("Main warehouse"), or a loose spot as people say it ("S2", "Alex's house"). */
const looseName = (w: WhereRow) => (isShelf(placeOf(w)) ? w.location_code : placeText(placeOf(w)));
const spotName = (w: WhereRow) => (w.box_id ? boxName({ id: w.box_id, name: w.box_name }) : looseName(w));

interface Done { action_id: number; text: string }

export function WhereItIs({ p }: { p: Product }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState<string | null>(null);   // a row's key, or 'all'
  const [last, setLast] = useState<Done | null>(null);
  const places = new Set(p.where.map((w) => hkey(holderOf(w)))).size;

  const undo = async (d: Done) => {
    try {
      await post('/api/undo', { action_id: d.action_id });
      toast('Move undone');
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Undo did not work', { tone: 'error' });
    }
    setLast((l) => (l?.action_id === d.action_id ? null : l));
    void qc.invalidateQueries();
  };
  const done = (d: Done) => {
    setOpen(null);
    setLast(d);
    void qc.invalidateQueries();
  };

  if (!p.where.length) return <Empty>Not in stock</Empty>;
  return (
    <div className="space-y-3 text-sm">
      {last && (
        <div className="flex items-center gap-2 rounded-lg border border-primary/40 bg-primary/10 p-3" data-testid="move-done">
          <span className="min-w-0 flex-1 font-medium">{last.text}.</span>
          <Button variant="outline" className="h-11" onClick={() => void undo(last)}><Undo2 className="mr-1 h-4 w-4" />Undo</Button>
        </div>
      )}
      <div className="divide-y rounded-lg border">
        {p.where.map((w) => {
          const k = rowKey(w);
          return (
            <div key={k} data-testid="where-row">
              <div className="flex items-center gap-3 p-3">
                <div className="min-w-0 flex-1">
                  {w.box_id
                    ? <Link to={`/boxes/${w.box_id}`} className="text-base font-semibold text-primary hover:underline">{spotName(w)}</Link>
                    : <span className="text-base font-semibold">Loose · {looseName(w)}</span>}
                  {w.box_id ? <span className="block text-xs text-muted-foreground">{placeText(placeOf(w))}</span> : null}
                  {w.unit_id ? (
                    <div className="text-xs text-muted-foreground">
                      <Link to={`/units/${w.unit_id}`} className="text-primary hover:underline">Unit {w.unit_id}</Link>
                      {w.unit_condition ? ` · ${w.unit_condition}` : ''}{w.unit_cost_cents !== null ? ` · ${money(w.unit_cost_cents)}` : ''}
                    </div>
                  ) : null}
                </div>
                <div className="text-base font-bold tabular-nums">{w.qty}</div>
                <button type="button" onClick={() => setOpen(open === k ? null : k)} aria-expanded={open === k}
                  aria-label={open === k ? 'Close the move form' : `Move from ${spotName(w)}`}
                  className={cn(tapTile, 'flex min-h-11! items-center px-3')}>
                  {open === k ? <span className="text-sm font-semibold text-primary">Close</span> : <TapAction label="Move" />}
                </button>
              </div>
              {open === k && <MovePanel p={p} row={w} onDone={done} onClose={() => setOpen(null)} />}
            </div>
          );
        })}
      </div>
      {places > 1 && (open === 'all'
        ? <div className="rounded-lg border"><MovePanel p={p} row={null} onDone={done} onClose={() => setOpen(null)} /></div>
        : (
          <button type="button" onClick={() => setOpen('all')} className={cn(tapTile, 'flex min-h-12! w-full items-center gap-3 px-3 py-2')}>
            <span className="min-w-0 flex-1 font-medium">It is in {places} places. Move all {p.on_hand} into one box</span>
            <TapAction label="Choose" />
          </button>
        ))}
    </div>
  );
}

/** row = the one place to take from; null = every place, into one. */
function MovePanel({ p, row, onDone, onClose }: { p: Product; row: WhereRow | null; onDone: (d: Done) => void; onClose: () => void }) {
  const qc = useQueryClient();
  const { feedback } = useScanBus();
  const cur = useCurrentBox();
  const ref = useRef<HTMLDivElement>(null);
  const [qtyText, setQtyText] = useState('1');
  const [to, setTo] = useState<{ h: Holder; name: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => { ref.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, []);

  const src = row ? holderOf(row) : null;
  const srcName = row ? spotName(row) : '';
  const max = row ? row.qty : p.on_hand;
  const typed = Math.floor(Number(qtyText));
  const one = !!row && (!!row.unit_id || max === 1);
  const n = !row ? max : one ? 1 : typed;
  const badQty = !!row && !one && !(n >= 1 && n <= max);
  const holds = (h: Holder) => p.where.filter((w) => hkey(holderOf(w)) === hkey(h)).reduce((a, w) => a + w.qty, 0);
  const curBox = isFresh(cur) && (!src || hkey(src) !== `b${cur!.box_id}`) ? cur : null;
  // The other places this item already sits in: a person may want to join one of those piles.
  const piles = [...new Map(p.where.map((w) => [hkey(holderOf(w)), { h: holderOf(w), name: spotName(w) }])).values()]
    .filter((s) => (!src || hkey(s.h) !== hkey(src)) && (!curBox || hkey(s.h) !== `b${curBox.box_id}`));

  const fail = (text: string) => { feedback(false); setErr(text); };
  const save = async (h: Holder, name: string) => {
    if (busy) return;
    if (src && hkey(h) === hkey(src)) return fail(`It is already in ${name}. Choose another place.`);
    if (badQty) return fail(`Type how many to move: 1 to ${max}. ${srcName} holds ${max}.`);
    setBusy(true);
    setErr('');
    try {
      const r = row
        ? await post<{ action_id: number; moved?: number }>('/api/move', { from: src, to: h, lines: [{ product_id: p.id, unit_id: row.unit_id, qty: n }] })
        : await post<{ action_id: number; moved?: number }>(`/api/products/${p.id}/gather`, { to: h });
      feedback(true);
      if (cur && 'box_id' in h && h.box_id === cur.box_id) touchCurrentBox();
      onDone({ action_id: r.action_id, text: row ? `Moved ${n} from ${srcName} to ${name}` : `Moved all ${r.moved ?? n} into ${name}` });
    } catch (e) {
      // The other person moved it first: say what the server said and show what is there now.
      fail(e instanceof Error ? e.message : 'The move did not save');
      void qc.invalidateQueries({ queryKey: ['product', p.id] });
    } finally {
      setBusy(false);
    }
  };

  // A box or shelf label scanned while this form is open is where it goes.
  useScan(async (code) => {
    if (busy) return;
    const r = await get<ScanResult>(`/api/scan?code=${encodeURIComponent(code)}`).catch((e: Error) => e);
    if (r instanceof Error) return fail(r.message);
    if (r.kind === 'box') return save({ box_id: r.box.id }, boxName(r.box));
    if (r.kind === 'location') return save({ location_id: r.location.id }, r.location.code);
    fail(r.kind === 'bad_check' || r.kind === 'unknown' ? r.message : 'Scan the label of the box or shelf it goes to.');
  });

  const tile = (h: Holder, name: string, sub: string) => (
    <button key={hkey(h)} type="button" disabled={busy} onClick={() => void save(h, name)} data-testid="move-tile"
      className={cn(tapTile, 'flex min-h-14! w-full items-center gap-3 px-3 py-2')}>
      <span className="min-w-0 flex-1">
        <span className="block text-base font-semibold">Into {name}</span>
        <span className="block text-xs text-muted-foreground">{sub}</span>
      </span>
      <TapAction label={row ? `Move ${badQty ? '' : n}` : 'Move all'} />
    </button>
  );

  return (
    <div ref={ref} className="space-y-3 border-t bg-muted/30 p-3" data-testid="move-panel">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1 text-base font-semibold">
          {row
            ? <>Move from {srcName}: {row.unit_id ? `unit ${row.unit_id}` : `${max} here`}</>
            : <>Move all {max} into one box</>}
          {!row && <span className="block text-xs font-normal text-muted-foreground">Every pile of this item goes to the place you choose. One Undo puts them all back.</span>}
        </div>
        <button type="button" onClick={onClose} aria-label="Close the move form" className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md border bg-background"><X className="h-5 w-5" /></button>
      </div>

      {row && !one && (
        <div className="space-y-1">
          <div className="text-xs font-medium text-muted-foreground">How many?</div>
          <div className="flex items-center gap-2">
            <Button type="button" variant="outline" className="h-11 w-11 p-0" aria-label="One fewer" disabled={!(n > 1)} onClick={() => setQtyText(String(Math.min(max, n) - 1))}><Minus className="h-4 w-4" /></Button>
            <Input type="number" inputMode="numeric" min={1} max={max} value={qtyText} aria-label="How many to move"
              onChange={(e) => { setQtyText(e.target.value); setErr(''); }} onFocus={(e) => e.target.select()}
              className={cn('h-11 w-20 text-center text-base font-semibold', badQty && 'border-destructive')} />
            <Button type="button" variant="outline" className="h-11 w-11 p-0" aria-label="One more" disabled={!(n < max)} onClick={() => setQtyText(String(Math.max(0, n || 0) + 1))}><Plus className="h-4 w-4" /></Button>
            <Button type="button" variant={n === max ? 'default' : 'outline'} className="h-11 flex-1" onClick={() => setQtyText(String(max))}>All {max}</Button>
          </div>
          {badQty && <p className="text-sm text-destructive">{srcName} holds {max}. Type 1 to {max}.</p>}
        </div>
      )}

      {err && <div className="rounded-md border border-destructive/50 bg-red-50 px-3 py-2 text-sm font-medium text-red-900 dark:bg-red-950 dark:text-red-100" data-testid="move-error">{err}</div>}

      <div className="space-y-2">
        <div className="text-xs font-medium text-muted-foreground">Where to? Tap a box here, or scan a box or shelf label.</div>
        {curBox && tile({ box_id: curBox.box_id }, curBox.name,
          `The box you are filling${holds({ box_id: curBox.box_id }) ? ` · already holds ${holds({ box_id: curBox.box_id })}` : ''}`)}
        {piles.map((s) => tile(s.h, s.name, `Already holds ${holds(s.h)} of this item`))}
      </div>

      <div className="space-y-2">
        <div className="flex items-center gap-1 text-xs font-medium text-muted-foreground"><ScanLine className="h-3.5 w-3.5" />Or choose another box or shelf:</div>
        <MarketPicker current={to && 'box_id' in to.h ? to.name : null} exclude={src && 'box_id' in src ? src.box_id : null}
          onPick={(b) => (src && hkey(src) === `b${b.id}` ? fail(`It is already in ${b.code}. Choose another box.`) : (setErr(''), setTo({ h: { box_id: b.id }, name: b.code })))} />
        <HolderPicker value={to?.h ?? null} listen={false} exclude={src} label="Any box or shelf" onChange={(h, name) => { setErr(''); setTo({ h, name }); }} />
        {to && (
          <Button className="h-12 w-full text-base" disabled={busy || badQty} onClick={() => void save(to.h, to.name)} data-testid="move-confirm">
            <MoveRight className="mr-1.5 h-5 w-5" />
            {row ? `Move ${badQty ? '' : n} to ${to.name}` : `Move all ${max} into ${to.name}`}
            {holds(to.h) ? <span className="ml-1.5 text-xs font-normal opacity-80">(already holds {holds(to.h)})</span> : null}
          </Button>
        )}
      </div>
    </div>
  );
}
