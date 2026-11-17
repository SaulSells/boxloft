import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowDown, MoveRight, RotateCcw } from 'lucide-react';
import { get, post } from '@/api/client';
import type { Box, ContentRow, Holder, Location, ScanResult } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { ContentsList, HolderPicker, PageHeader } from '@/components/app/common';
import { MarketPicker } from '@/components/app/MarketPicker';
import { useToast } from '@/components/app/toast';
import { useScan, useScanBus } from '@/lib/scan';
import { useQueryParam } from '@/lib/router';
import { units } from '@/lib/format';
import { cn } from '@/lib/utils';
import { boxName, placeText } from '@/lib/places';

/*
 * Move, scan first. Typical flows, no taps needed:
 *   box -> shelf               puts the whole box on that shelf
 *   box -> item(s) -> box      moves the scanned items (each scan of a UPC adds 1)
 *   unit label -> box          moves that one unit (its current box is looked up)
 *   box -> same box again      picks everything; then scan the destination
 * Every move is one action with an Undo button.
 */

interface Source { holder: Holder; name: string; contents: ContentRow[]; isBox: boolean }
const key = (r: { product_id: number; unit_id: number }) => `${r.product_id}-${r.unit_id}`;
const same = (a: Holder, b: Holder) => ('box_id' in a && 'box_id' in b && a.box_id === b.box_id) || ('location_id' in a && 'location_id' in b && a.location_id === b.location_id);

export function Move() {
  const qc = useQueryClient();
  const toast = useToast();
  const { feedback } = useScanBus();
  const fromParam = useQueryParam('from');
  const [src, setSrc] = useState<Source | null>(null);
  const [picks, setPicks] = useState<Record<string, number>>({});
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmAll, setConfirmAll] = useState<{ to: Holder; name: string } | null>(null);
  const [manualTo, setManualTo] = useState<Holder | null>(null);
  const [manualToName, setManualToName] = useState('');

  const loadSource = useCallback(async (h: Holder): Promise<Source> => {
    if ('box_id' in h) {
      const b = await get<Box>(`/api/boxes/${h.box_id}`);
      return { holder: h, name: `Box ${boxName(b)} at ${placeText({ code: b.location_code, site: b.location_site, name: b.location_name })}`, contents: b.contents, isBox: true };
    }
    const l = await get<Location>(`/api/locations/${h.location_id}`);
    return { holder: h, name: `${placeText(l)} (loose items)`, contents: l.loose, isBox: false };
  }, []);

  // Open with ?from=box:12 from a box page.
  useEffect(() => {
    if (fromParam?.startsWith('box:')) void loadSource({ box_id: Number(fromParam.slice(4)) }).then(setSrc);
  }, [fromParam, loadSource]);

  const bad = (text: string) => { feedback(false); setMsg({ text, bad: true }); };
  const ok = (text: string) => { feedback(true); setMsg({ text }); };
  const reset = () => { setSrc(null); setPicks({}); setMsg(null); setConfirmAll(null); setManualTo(null); };
  const pickedCount = Object.values(picks).reduce((a, b) => a + b, 0);

  const afterAction = async (actionId: number, text: string, keepSource: Holder | null) => {
    qc.invalidateQueries();
    toast(text, { undo: async () => { await post('/api/undo', { action_id: actionId }); qc.invalidateQueries(); if (keepSource) setSrc(await loadSource(keepSource)); toast('Move undone'); } });
    setPicks({});
    setConfirmAll(null);
    setManualTo(null);
    if (keepSource) setSrc(await loadSource(keepSource));
  };

  const commit = async (to: Holder, toName: string) => {
    if (!src) return;
    setBusy(true);
    try {
      const lines = Object.entries(picks).map(([k, qty]) => {
        const [product_id, unit_id] = k.split('-').map(Number);
        return { product_id, unit_id, qty };
      });
      const r = await post<{ action_id: number }>('/api/move', { from: src.holder, to, lines });
      feedback(true);
      setMsg({ text: `Moved ${units(pickedCount)} to ${toName}. Keep scanning items from ${src.name.split(' (')[0]}, or Start over.` });
      await afterAction(r.action_id, `Moved ${units(pickedCount)} → ${toName}`, src.holder);
    } catch (e) {
      bad(e instanceof Error ? e.message : 'Move failed');
    } finally {
      setBusy(false);
    }
  };

  const moveAll = async (to: Holder, toName: string) => {
    if (!src) return;
    setBusy(true);
    try {
      const r = await post<{ action_id: number }>('/api/move-all', { from: src.holder, to });
      ok(`Emptied ${src.name.split(' (')[0]} into ${toName}.`);
      await afterAction(r.action_id, `Emptied ${src.name.split(' (')[0]} → ${toName}`, src.holder);
    } catch (e) {
      bad(e instanceof Error ? e.message : 'Move failed');
    } finally {
      setBusy(false);
    }
  };

  const moveBox = async (boxId: number, locationId: number, code: string) => {
    setBusy(true);
    try {
      const r = await post<{ action_id: number }>('/api/move-boxes', { box_ids: [boxId], location_id: locationId });
      ok(`Box ${boxId} is now on ${code}. Scan the next box.`);
      await afterAction(r.action_id, `Box ${boxId} → ${code}`, null);
      setSrc(null);
    } catch (e) {
      bad(e instanceof Error ? e.message : 'Move failed');
    } finally {
      setBusy(false);
    }
  };

  const addPick = (r: ContentRow, n = 1) => {
    const k = key(r);
    const cur = picks[k] ?? 0;
    if (r.unit_id) {
      setPicks({ ...picks, [k]: cur ? 0 : 1 });
      return true;
    }
    if (cur + n > r.qty) return false;
    setPicks({ ...picks, [k]: cur + n });
    return true;
  };

  useScan(async (code) => {
    if (busy) return;
    const r = await get<ScanResult>(`/api/scan?code=${encodeURIComponent(code)}`).catch((e: Error) => e);
    if (r instanceof Error) return bad(r.message);
    if (r.kind === 'bad_check' || r.kind === 'unknown') return bad(r.message);

    if (!src) {
      if (r.kind === 'box') { setSrc(await loadSource({ box_id: r.box.id })); return ok(`From box ${boxName(r.box)}. Scan items to move, scan a shelf to move the whole box, or scan another box to empty it there.`); }
      if (r.kind === 'location') { setSrc(await loadSource({ location_id: r.location.id })); return ok(`From ${r.location.code}. Scan the items to move.`); }
      if (r.kind === 'unit') {
        const h = r.unit.holder;
        if (!h || h.location_kind === 'virtual') return bad(`Unit ${r.unit.id} is not on the shelves (${h?.location_code ?? 'unknown'})`);
        const s = await loadSource(h.box_id ? { box_id: h.box_id } : { location_id: h.location_id });
        setSrc(s);
        setPicks({ [`${r.unit.product_id}-${r.unit.id}`]: 1 });
        return ok(`Unit ${r.unit.id} picked from ${s.name.split(' (')[0]}. Now scan where it goes.`);
      }
      return bad('Scan the box (or shelf) the item is in first. Then scan the item.');
    }

    if (r.kind === 'product') {
      const ids = new Set(r.products.map((p) => p.id));
      const rows = src.contents.filter((c) => ids.has(c.product_id));
      if (!rows.length) return bad(`${r.products[0]?.title ?? r.value} is not in ${src.name.split(' (')[0]}`);
      const qtyRow = rows.find((c) => !c.unit_id && (picks[key(c)] ?? 0) < c.qty);
      if (qtyRow) { addPick(qtyRow); return ok(`+1 ${qtyRow.sku} (${(picks[key(qtyRow)] ?? 0) + 1} of ${qtyRow.qty})`); }
      if (rows.some((c) => c.unit_id)) return bad('These are one-off units. Scan the unit label (SU...) instead.');
      return bad(`All ${rows[0]!.qty} of ${rows[0]!.sku} are already picked`);
    }
    if (r.kind === 'unit') {
      const row = src.contents.find((c) => c.unit_id === r.unit.id);
      if (row) { addPick(row); return ok(`${picks[key(row)] ? 'Unpicked' : 'Picked'} unit ${r.unit.id}`); }
      return bad(`Unit ${r.unit.id} is in ${r.unit.holder?.box_id ? `Box ${r.unit.holder.box_id}` : r.unit.holder?.location_code}, not here`);
    }
    if (r.kind !== 'box' && r.kind !== 'location') return;
    const dest: Holder = r.kind === 'box' ? { box_id: r.box.id } : { location_id: r.location.id };
    const destName = r.kind === 'box' ? `Box ${r.box.id}` : r.location.code;
    if (same(dest, src.holder)) {
      if (pickedCount) { setPicks({}); return ok('Cleared the picks'); }
      setPicks(Object.fromEntries(src.contents.map((c) => [key(c), c.qty])));
      return ok('Picked everything. Now scan where it goes.');
    }
    if (pickedCount) return commit(dest, destName);
    if (src.isBox && r.kind === 'location' && 'box_id' in src.holder) return moveBox(src.holder.box_id, r.location.id, r.location.code);
    if (!src.contents.length) return bad(`${src.name.split(' (')[0]} is empty. Nothing to move.`);
    feedback(true);
    setConfirmAll({ to: dest, name: destName });
    setMsg({ text: `Move everything in ${src.name.split(' (')[0]} into ${destName}?` });
  });

  const step = !src ? 1 : pickedCount ? 3 : 2;
  return (
    <div className="max-w-3xl space-y-4">
      <PageHeader title="Move" subtitle="Scan, scan, done. Every move can be undone." actions={src && <Button variant="outline" onClick={reset}><RotateCcw className="mr-1.5 h-4 w-4" />Start over</Button>} />

      <div className="grid grid-cols-3 gap-2 text-center text-xs font-medium">
        {['1. Scan where from', '2. Scan items (or tap)', '3. Scan where to'].map((t, i) => (
          <div key={t} className={cn('rounded-md border px-2 py-2', step === i + 1 ? 'border-primary bg-primary/10 text-primary' : 'text-muted-foreground')}>{t}</div>
        ))}
      </div>

      {msg && <div className={cn('rounded-lg border px-4 py-3 text-sm font-medium', msg.bad ? 'scan-bad border-destructive/50 bg-red-50 text-red-900 dark:bg-red-950 dark:text-red-100' : 'scan-ok border-primary/40 bg-primary/10')}>{msg.text}</div>}

      {confirmAll && (
        <Card className="border-primary"><CardContent className="flex flex-wrap items-center gap-2 p-4">
          <span className="flex-1 text-sm">Empty <b>{src?.name.split(' (')[0]}</b> ({units(src?.contents.reduce((a, c) => a + c.qty, 0) ?? 0)}) into <b>{confirmAll.name}</b>?</span>
          <Button onClick={() => moveAll(confirmAll.to, confirmAll.name)} disabled={busy}>Move everything</Button>
          <Button variant="ghost" onClick={() => { setConfirmAll(null); setMsg(null); }}>Cancel</Button>
        </CardContent></Card>
      )}

      {!src ? (
        <Card><CardContent className="space-y-4 p-6 text-center">
          <MoveRight className="mx-auto h-10 w-10 text-primary" />
          <p className="text-sm text-muted-foreground">Scan a box label, a shelf label, or a unit label. Use the Scan button on a phone.</p>
          <div className="text-left">
            <HolderPicker value={null} listen={false} label="Or pick where from" onChange={async (h) => { setSrc(await loadSource(h)); setMsg(null); }} />
          </div>
        </CardContent></Card>
      ) : (
        <>
          <div className="flex items-center justify-between gap-2">
            <div className="font-semibold">{src.name}</div>
            <div className="flex gap-2">
              {pickedCount > 0 && <Button size="sm" variant="ghost" onClick={() => setPicks({})}>Clear picks</Button>}
              <Button size="sm" variant="outline" onClick={() => setPicks(Object.fromEntries(src.contents.map((c) => [key(c), c.qty])))}>Pick all</Button>
            </div>
          </div>
          <ContentsList rows={src.contents} empty="Nothing in here" picked={(r) => picks[key(r)] ?? 0}
            onPick={(r) => { if (!addPick(r)) setPicks({ ...picks, [key(r)]: 0 }); }} />
          {pickedCount > 0 && (
            <Card><CardContent className="space-y-3 p-4">
              <div className="flex items-center gap-2 text-sm font-medium"><ArrowDown className="h-4 w-4" /> {units(pickedCount)} picked. Scan where they go, or pick:</div>
              <MarketPicker current={manualTo && 'box_id' in manualTo ? manualToName : null} onPick={(b) => { setManualTo({ box_id: b.id }); setManualToName(b.code); }} />
              <HolderPicker value={manualTo} listen={false} exclude={src.holder} label="Destination" onChange={(h, n) => { setManualTo(h); setManualToName(n); }} />
              <Button className="h-11 w-full" disabled={!manualTo || busy} onClick={() => manualTo && commit(manualTo, manualToName)}>Move {units(pickedCount)}{manualToName ? ` to ${manualToName}` : ''}</Button>
            </CardContent></Card>
          )}
          {!pickedCount && src.isBox && (
            <p className="text-sm text-muted-foreground">Tip: scan a shelf label to put this whole box there. Scan another box to empty this one into it.</p>
          )}
        </>
      )}
    </div>
  );
}
