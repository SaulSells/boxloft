import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ClipboardCheck, Minus, Plus, Trash2 } from 'lucide-react';
import { ApiError, get, post } from '@/api/client';
import type { Box, BoxRow, ContentRow, Holder, Location, ProductRow, ScanResult } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { Empty, HolderPicker, PageHeader, SearchInput, Thumb } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { useScan, useScanBus } from '@/lib/scan';
import { useQueryParam } from '@/lib/router';
import { ago, plural, units } from '@/lib/format';
import { cn } from '@/lib/utils';
import { boxName, placeText } from '@/lib/places';

/*
 * Blind count of one box or shelf. You scan what is really there; the expected numbers stay
 * hidden until you submit, so they cannot steer the count. Differences post as moves to or from
 * ADJUST (one action, undoable) and every count is kept for the variance report.
 */

interface Line { product_id: number; unit_id: number; qty: number; sku: string; title: string; photo_id: number | null; tracking: string }
interface Result { action_id: number; differences: { product_id: number; unit_id: number; expected: number; counted: number }[] }

export function Count() {
  const qc = useQueryClient();
  const toast = useToast();
  const { feedback } = useScanBus();
  const [target, setTarget] = useState<{ holder: Holder; name: string; expected: ContentRow[] } | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [showExpected, setShowExpected] = useState(false);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  // Blind count: a shortage asks for one recount (naming the items, not the numbers) before it writes stock off.
  const [recount, setRecount] = useState<string[] | null>(null);
  const [search, setSearch] = useState('');
  const [busy, setBusy] = useState(false);
  const boxParam = useQueryParam('box');
  useEffect(() => { if (boxParam) void open({ box_id: Number(boxParam) }); }, [boxParam]); // eslint-disable-line react-hooks/exhaustive-deps
  const boxes = useQuery({ queryKey: ['boxes'], queryFn: () => get<{ boxes: BoxRow[] }>('/api/boxes'), enabled: !target });
  const found = useQuery({
    queryKey: ['products', 'count', search],
    queryFn: () => get<{ products: ProductRow[] }>(`/api/products?q=${encodeURIComponent(search)}`),
    enabled: search.trim().length > 1,
  });

  const open = async (h: Holder) => {
    if ('box_id' in h) {
      const b = await get<Box>(`/api/boxes/${h.box_id}`);
      setTarget({ holder: h, name: `Box ${boxName(b)}`, expected: b.contents });
    } else {
      const l = await get<Location>(`/api/locations/${h.location_id}`);
      setTarget({ holder: h, name: `${placeText(l)} (loose items)`, expected: l.loose });
    }
    setLines([]);
    setResult(null);
    setMsg(null);
    setRecount(null);
  };

  const add = (l: Omit<Line, 'qty'>, n = 1) => {
    setLines((cur) => {
      const i = cur.findIndex((x) => x.product_id === l.product_id && x.unit_id === l.unit_id);
      if (i < 0) return [{ ...l, qty: n }, ...cur];
      if (l.unit_id) return cur;
      return cur.map((x, k) => (k === i ? { ...x, qty: x.qty + n } : x));
    });
  };

  useScan(async (code) => {
    const r = await get<ScanResult>(`/api/scan?code=${encodeURIComponent(code)}`);
    if (!target || result) {
      if (r.kind === 'box') { feedback(true); return open({ box_id: r.box.id }); }
      if (r.kind === 'location') { feedback(true); return open({ location_id: r.location.id }); }
      feedback(false);
      return setMsg({ text: 'Scan the box or shelf you are counting first', bad: true });
    }
    if (r.kind === 'product') {
      const p = r.products.find((x) => target.expected.some((e) => e.product_id === x.id)) ?? r.products[0]!;
      if (p.tracking === 'unit') { feedback(false); return setMsg({ text: 'One-off units: scan each unit label (SU...)', bad: true }); }
      add({ product_id: p.id, unit_id: 0, sku: p.sku, title: p.title, photo_id: p.photo_id, tracking: p.tracking });
      feedback(true);
      return setMsg({ text: `+1 ${p.sku}` });
    }
    if (r.kind === 'unit') {
      add({ product_id: r.unit.product_id, unit_id: r.unit.id, sku: r.unit.sku, title: r.unit.title, photo_id: r.unit.photos[0]?.id ?? null, tracking: 'unit' });
      feedback(true);
      return setMsg({ text: `Found unit ${r.unit.id}` });
    }
    if (r.kind === 'box' || r.kind === 'location') { feedback(false); return setMsg({ text: 'Submit or cancel this count before starting another', bad: true }); }
    feedback(false);
    setMsg({ text: r.message, bad: true });
  });

  const submit = async () => {
    if (!target) return;
    if (!recount) {
      const short = target.expected.filter((e) => e.qty > lines.filter((l) => l.product_id === e.product_id && l.unit_id === e.unit_id).reduce((n, l) => n + l.qty, 0));
      if (short.length) {
        setRecount(short.map((e) => `${e.sku}${e.unit_id ? ` unit ${e.unit_id}` : ''} · ${e.title}`));
        feedback(false);
        return;
      }
    }
    setRecount(null);
    setBusy(true);
    try {
      const r = await post<Result>('/api/count', {
        holder: target.holder,
        counted: lines.map((l) => ({ product_id: l.product_id, unit_id: l.unit_id, qty: l.qty })),
        note: showExpected ? 'not blind' : '',
        // What the screen showed: if the other person moved stock in or out since, the server refuses (409).
        seen: target.expected.map((e) => ({ product_id: e.product_id, unit_id: e.unit_id ?? 0, qty: e.qty })),
      });
      setResult(r);
      qc.invalidateQueries();
      toast(r.differences.length ? `Count saved: ${plural(r.differences.length, 'difference')} corrected` : 'Count saved: everything matched', {
        undo: async () => { await post('/api/undo', { action_id: r.action_id }); qc.invalidateQueries(); toast('Count undone'); },
      });
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Count failed', { tone: 'error' });
      if (e instanceof ApiError && e.status === 409) await open(target.holder);   // show what is there now
    } finally {
      setBusy(false);
    }
  };

  const name = (pid: number, uid: number) => {
    const e = target?.expected.find((x) => x.product_id === pid && x.unit_id === uid) ?? lines.find((x) => x.product_id === pid && x.unit_id === uid);
    return e ? `${e.sku}${uid ? ` unit ${uid}` : ''} · ${e.title}` : `#${pid}`;
  };

  if (!target) {
    const queue = [...(boxes.data?.boxes ?? [])].filter((b) => b.units > 0).sort((a, b) => (a.last_counted ?? '').localeCompare(b.last_counted ?? ''));
    return (
      <div className="max-w-2xl space-y-4">
        <PageHeader title="Count" subtitle="Scan a box or shelf to count it. Counting a few boxes a day keeps the numbers honest." />
        {msg && <div className="rounded-lg border border-destructive/50 px-4 py-3 text-sm">{msg.text}</div>}
        <Card><CardContent className="p-4 sm:p-6"><HolderPicker value={null} listen={false} label="What are you counting?" onChange={(h) => open(h)} /></CardContent></Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Count next (oldest count first)</CardTitle></CardHeader>
          <CardContent>
            {queue.length ? (
              <div className="divide-y text-sm">
                {queue.slice(0, 15).map((b) => (
                  <button key={b.id} className="flex w-full items-center justify-between gap-2 py-2 text-left hover:text-primary" onClick={() => open({ box_id: b.id })}>
                    <span>Box {boxName(b)} · {placeText({ code: b.location_code, site: b.location_site, name: b.location_name })} · {units(b.units)}</span>
                    <span className="text-xs text-muted-foreground">{b.last_counted ? `counted ${ago(b.last_counted)}` : 'never counted'}</span>
                  </button>
                ))}
              </div>
            ) : <Empty>No boxes with stock yet</Empty>}
          </CardContent>
        </Card>
      </div>
    );
  }

  const total = lines.reduce((a, l) => a + l.qty, 0);
  return (
    <div className="max-w-3xl space-y-4">
      <PageHeader title={`Counting ${target.name}`} subtitle="Scan everything in it. Type a number for items without a barcode." actions={<Button variant="outline" onClick={() => setTarget(null)}>{result ? 'Count another' : 'Cancel'}</Button>} />
      {msg && <div className={cn('rounded-lg border px-4 py-3 text-sm font-medium', msg.bad ? 'scan-bad border-destructive/50' : 'scan-ok border-primary/40 bg-primary/10')}>{msg.text}</div>}

      {result ? (
        <Card><CardContent className="space-y-3 p-4 sm:p-6">
          <div className="flex items-center gap-2 font-semibold"><ClipboardCheck className="h-5 w-5 text-primary" />{result.differences.length ? `${plural(result.differences.length, 'difference')} found and corrected` : 'Everything matched'}</div>
          {result.differences.map((d) => (
            <div key={`${d.product_id}-${d.unit_id}`} className="flex justify-between gap-2 border-t pt-2 text-sm">
              <span className="min-w-0 truncate">{name(d.product_id, d.unit_id)}</span>
              <span className={cn('shrink-0 tabular-nums font-medium', d.counted < d.expected ? 'text-destructive' : 'text-primary')}>expected {d.expected}, counted {d.counted} ({d.counted - d.expected > 0 ? '+' : ''}{d.counted - d.expected})</span>
            </div>
          ))}
        </CardContent></Card>
      ) : (
        <>
          <div className="flex items-center gap-2"><Switch id="exp" checked={showExpected} onCheckedChange={setShowExpected} /><Label htmlFor="exp">Show what the system expects (counts are more honest without it)</Label></div>
          {showExpected && (
            <div className="rounded-lg border p-3 text-sm">
              {target.expected.length ? target.expected.map((e) => <div key={`${e.product_id}-${e.unit_id}`} className="flex justify-between"><span>{e.sku}{e.unit_id ? ` unit ${e.unit_id}` : ''}</span><span className="tabular-nums">{e.qty}</span></div>) : 'Expected empty'}
            </div>
          )}
          <div className="space-y-2">
            {lines.map((l) => (
              <div key={`${l.product_id}-${l.unit_id}`} className="flex items-center gap-3 rounded-lg border bg-card p-2.5">
                <Thumb photoId={l.photo_id} size="dense" />
                <div className="min-w-0 flex-1"><div className="truncate text-sm font-medium">{l.title}</div><div className="font-mono text-xs text-muted-foreground">{l.sku}{l.unit_id ? ` · unit ${l.unit_id}` : ''}</div></div>
                {l.unit_id ? (
                  <Button size="icon" variant="ghost" onClick={() => setLines(lines.filter((x) => x !== l))} aria-label="Remove"><Trash2 className="h-4 w-4" /></Button>
                ) : (
                  <div className="flex items-center gap-1">
                    <Button size="icon" variant="outline" className="h-9 w-9" onClick={() => setLines(lines.map((x) => (x === l ? { ...x, qty: Math.max(0, x.qty - 1) } : x)))} aria-label="Less"><Minus className="h-4 w-4" /></Button>
                    <Input inputMode="numeric" value={l.qty} onChange={(e) => setLines(lines.map((x) => (x === l ? { ...x, qty: Number(e.target.value.replace(/\D/g, '') || 0) } : x)))} className="h-9 w-16 text-center font-bold tabular-nums" />
                    <Button size="icon" variant="outline" className="h-9 w-9" onClick={() => setLines(lines.map((x) => (x === l ? { ...x, qty: x.qty + 1 } : x)))} aria-label="More"><Plus className="h-4 w-4" /></Button>
                  </div>
                )}
              </div>
            ))}
            {!lines.length && <Empty>Nothing counted yet. Scan the first item. If the box is really empty, submit as is.</Empty>}
          </div>
          <div className="space-y-2">
            <SearchInput placeholder="No barcode? Search to add it..." value={search} onChange={(e) => setSearch(e.target.value)} />
            {(found.data?.products ?? []).slice(0, 6).map((p) => (
              <button key={p.id} disabled={p.tracking === 'unit'} className="flex w-full items-center gap-2 rounded-md border p-2 text-left text-sm hover:bg-muted/50 disabled:opacity-50"
                onClick={() => { add({ product_id: p.id, unit_id: 0, sku: p.sku, title: p.title, photo_id: p.photo_id, tracking: p.tracking }); setSearch(''); }}>
                <Plus className="h-4 w-4" /> {p.title} <span className="font-mono text-xs text-muted-foreground">{p.sku}{p.tracking === 'unit' ? ' (scan unit labels)' : ''}</span>
              </button>
            ))}
          </div>
          {recount && (
            <div className="space-y-1 rounded-lg bg-warning/10 px-3 py-2 text-sm">
              <div className="font-medium">Fewer than expected. Look once more for:</div>
              {recount.slice(0, 8).map((t) => <div key={t} className="truncate text-xs">{t}</div>)}
              {recount.length > 8 && <div className="text-xs text-muted-foreground">and {recount.length - 8} more</div>}
              <div className="text-xs text-muted-foreground">Scan any you find. Submitting again writes the missing ones off (Undo stays available).</div>
            </div>
          )}
          <Button className="h-12 w-full text-base" onClick={submit} disabled={busy}>{busy ? 'Saving...' : recount ? `Recounted: submit (${units(total)})` : `Done: submit count (${units(total)})`}</Button>
        </>
      )}
    </div>
  );
}
