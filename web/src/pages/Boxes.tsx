import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Archive, ClipboardCheck, MoveRight, Plus, Printer, LogOut } from 'lucide-react';
import { get, post } from '@/api/client';
import type { Box, BoxRow, LocationRow } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Skeleton } from '@/components/ui/skeleton';
import { ContentsList, Empty, PageHeader, SearchInput } from '@/components/app/common';
import { ActivityList } from '@/pages/Activity';
import { useToast } from '@/components/app/toast';
import { navigate, useQueryParam, Link } from '@/lib/router';
import { ago, plural, prettyCode, units, when } from '@/lib/format';
import { boxLabel, printLabels } from '@/lib/labels';
import { boxName, buildingOf, placeText, spotOf } from '@/lib/places';
import { AddItems } from '@/pages/BoxAdd';
import { setCurrentBox } from '@/lib/currentBox';
import { APP } from '@/lib/appConfig';

function useLocations() {
  return useQuery({ queryKey: ['locations'], queryFn: () => get<{ locations: LocationRow[] }>('/api/locations') });
}

function NewBoxesDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const locs = useLocations();
  const [count, setCount] = useState('1');
  const [kind, setKind] = useState('box');
  const [loc, setLoc] = useState('');
  const [name, setName] = useState('');
  const [print, setPrint] = useState(true);
  const create = useMutation({
    mutationFn: () => post<{ boxes: BoxRow[] }>('/api/boxes', { count: name.trim() ? 1 : Number(count), kind, name: name.trim().toUpperCase(), location_id: loc ? Number(loc) : undefined }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['boxes'] });
      setName('');
      toast(`Made ${r.boxes.map(boxName).join(', ')}`);
      onOpenChange(false);
      if (print) void printLabels([...r.boxes].reverse().map(boxLabel));
    },
    onError: (e: Error) => toast(e.message, { tone: 'error' }),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>New boxes</DialogTitle></DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5"><Label>Name written on the box</Label><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="A31" className="h-11" autoCapitalize="characters" />
            <p className="text-xs text-muted-foreground">The label prints this name big, so the floor and Boxloft use one name. Each name is used once.</p></div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5"><Label>How many</Label><Input inputMode="numeric" value={name.trim() ? '1' : count} disabled={!!name.trim()} onChange={(e) => setCount(e.target.value.replace(/\D/g, ''))} className="h-11" /></div>
            <div className="space-y-1.5"><Label>Type</Label>
              <Select value={kind} onValueChange={setKind}><SelectTrigger className="h-11 w-full"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="box">Box</SelectItem><SelectItem value="tote">Tote</SelectItem><SelectItem value="pallet">Pallet</SelectItem></SelectContent></Select>
            </div>
          </div>
          <div className="space-y-1.5"><Label>Starts on</Label>
            <Select value={loc} onValueChange={setLoc}><SelectTrigger className="h-11 w-full"><SelectValue placeholder={APP.site} /></SelectTrigger>
              <SelectContent>{locs.data?.locations.filter((l) => l.kind !== 'virtual' && !l.archived).map((l) => <SelectItem key={l.id} value={String(l.id)}>{placeText(l)}</SelectItem>)}</SelectContent></Select>
          </div>
          <div className="flex items-center gap-2"><Checkbox id="pr" checked={print} onCheckedChange={(v) => setPrint(!!v)} /><Label htmlFor="pr">Print the labels now</Label></div>
        </div>
        <DialogFooter><Button onClick={() => create.mutate()} disabled={create.isPending || (!name.trim() && !Number(count))} className="h-11">{name.trim() ? `Make box ${name.trim().toUpperCase()}` : `Make ${count || 0}`}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type Row = { key: string; name: string; sub: string; building: string; spot: string; units: number; skus: number | null;
  counted: string | null; href: string; print: () => ReturnType<typeof boxLabel> };

/** Boxes (Alex 11-11 12:20 PM): boxes sit on shelves, and each has its name written on it (A31) and a printed 4x6
 *  label with that name and a barcode. Open shelving (items loose, no box) is on the Locations page. */
export function Boxes() {
  const wantNew = useQueryParam('new') === '1';
  const [newOpen, setNewOpen] = useState(wantNew);
  useEffect(() => { if (wantNew) setNewOpen(true); }, [wantNew]);
  const [q, setQ] = useState('');
  const [site, setSite] = useState('all');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const boxes = useQuery({ queryKey: ['boxes', q], queryFn: () => get<{ boxes: BoxRow[] }>(`/api/boxes?${new URLSearchParams({ q })}`) });
  const needle = q.trim().toUpperCase();
  const all: Row[] = (boxes.data?.boxes ?? []).map((b) => {
    const at = { code: b.location_code, site: b.location_site, name: b.location_name };
    return {
      key: `b:${b.id}`, name: boxName(b), sub: b.name ? '' : 'no name yet', building: buildingOf(at), spot: spotOf(at),
      units: b.units, skus: b.skus, counted: b.last_counted, href: `/boxes/${b.id}`, print: () => boxLabel(b),
    };
  }).sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  const sites = [...new Set(all.map((r) => r.building))].sort();
  const rows = all.filter((r) => site === 'all' || r.building === site);
  const toggle = (k: string) => setSelected((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });

  return (
    <div>
      <PageHeader
        title="Boxes"
        subtitle={`${plural(rows.length, 'box', 'boxes')} · ${units(rows.reduce((a, r) => a + r.units, 0))} inside`}
        actions={
          <>
            {selected.size > 0 && <Button variant="outline" onClick={() => printLabels(rows.filter((r) => selected.has(r.key)).map((r) => r.print()))}><Printer className="mr-1.5 h-4 w-4" />Print {selected.size}</Button>}
            <Button onClick={() => setNewOpen(true)}><Plus className="mr-1.5 h-4 w-4" />New boxes</Button>
          </>
        }
      />
      <NewBoxesDialog open={newOpen} onOpenChange={(v) => { setNewOpen(v); if (!v && wantNew) navigate('/boxes', true); }} />
      <div className="mb-4 flex flex-wrap gap-3">
        <SearchInput placeholder="Box name (A31) or number" value={q} onChange={(e) => setQ(e.target.value)} className="h-10 w-full sm:max-w-xs" />
        <Select value={site} onValueChange={setSite}><SelectTrigger className="h-10 w-56"><SelectValue /></SelectTrigger>
          <SelectContent><SelectItem value="all">All buildings</SelectItem>{sites.map((x) => <SelectItem key={x} value={x}>{x}</SelectItem>)}</SelectContent></Select>
      </div>
      {boxes.data && !rows.length ? <Empty>{needle ? `No box named ${q.trim()}.` : 'No boxes yet.'}</Empty> : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader><TableRow>
              <TableHead className="w-10" /><TableHead>Name</TableHead><TableHead>Location</TableHead><TableHead className="text-right">Items</TableHead>
              <TableHead className="hidden text-right sm:table-cell">SKUs</TableHead><TableHead className="hidden md:table-cell">Last counted</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.key} className="cursor-pointer" onClick={() => navigate(r.href)}>
                  <TableCell onClick={(e) => e.stopPropagation()}><Checkbox checked={selected.has(r.key)} onCheckedChange={() => toggle(r.key)} /></TableCell>
                  <TableCell><div className="font-semibold">{r.name}</div>{r.sub && <div className="text-xs text-muted-foreground">{r.sub}</div>}</TableCell>
                  <TableCell><div className="text-sm">{r.building}</div>{r.spot && <div className="text-xs text-muted-foreground">{r.spot}</div>}</TableCell>
                  <TableCell className="text-right font-bold tabular-nums">{r.units || <span className="font-normal text-muted-foreground">empty</span>}</TableCell>
                  <TableCell className="hidden text-right tabular-nums sm:table-cell">{r.skus ?? ''}</TableCell>
                  <TableCell className="hidden text-sm text-muted-foreground md:table-cell">{r.counted ? ago(r.counted) : 'never'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

function RemoveDialog({ box, open, onOpenChange }: { box: Box; open: boolean; onOpenChange: (v: boolean) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [target, setTarget] = useState('CUSTOMER');
  const [note, setNote] = useState('');
  const [qty, setQty] = useState<Record<string, number>>({});
  const lines = box.contents.map((c) => ({ c, k: `${c.product_id}-${c.unit_id}` })).filter(({ k }) => (qty[k] ?? 0) > 0)
    .map(({ c, k }) => ({ product_id: c.product_id, unit_id: c.unit_id, qty: qty[k] }));
  const go = useMutation({
    mutationFn: () => post<{ action_id: number }>('/api/remove', { from: { box_id: box.id }, lines, target, note }),
    onSuccess: (r) => {
      qc.invalidateQueries();
      onOpenChange(false);
      setQty({});
      toast(`Took ${units(lines.reduce((a, l) => a + (l.qty ?? 0), 0))} out to ${target}`, { undo: async () => { await post('/api/undo', { action_id: r.action_id }); qc.invalidateQueries(); } });
    },
    onError: (e: Error) => toast(e.message, { tone: 'error' }),
  });
  const TARGETS: Record<string, string> = { CUSTOMER: 'Sold / shipped to a customer', FBA: 'Sent to Amazon FBA', WFS: 'Sent to Walmart WFS', CLIENT: 'Returned to the 3PL client', DAMAGED: 'Damaged / thrown out', ADJUST: 'Lost / not found' };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader><DialogTitle>Take stock out of box {boxName(box)}</DialogTitle></DialogHeader>
        <Select value={target} onValueChange={setTarget}><SelectTrigger className="h-11 w-full"><SelectValue /></SelectTrigger>
          <SelectContent>{Object.entries(TARGETS).map(([k, v]) => <SelectItem key={k} value={k}>{v}</SelectItem>)}</SelectContent></Select>
        <div className="divide-y rounded-lg border">
          {box.contents.map((c) => {
            const k = `${c.product_id}-${c.unit_id}`;
            return (
              <div key={k} className="flex items-center gap-2 p-2 text-sm">
                <div className="min-w-0 flex-1"><div className="truncate">{c.title}</div><div className="font-mono text-xs text-muted-foreground">{c.sku}{c.unit_id ? ` · unit ${c.unit_id}` : ''} · {c.qty} here</div></div>
                {c.unit_id ? <Checkbox checked={!!qty[k]} onCheckedChange={(v) => setQty({ ...qty, [k]: v ? 1 : 0 })} /> : (
                  <Input inputMode="numeric" className="h-9 w-20 text-center" placeholder="0" value={qty[k] || ''} onChange={(e) => setQty({ ...qty, [k]: Math.min(c.qty, Number(e.target.value.replace(/\D/g, '') || 0)) })} />
                )}
              </div>
            );
          })}
        </div>
        <Input placeholder="Note: order #, FBA shipment ID..." value={note} onChange={(e) => setNote(e.target.value)} className="h-10" />
        <DialogFooter><Button className="h-11" disabled={!lines.length || go.isPending} onClick={() => go.mutate()}>Take out {lines.reduce((a, l) => a + (l.qty ?? 0), 0)}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function BoxPage({ id }: { id: number }) {
  const qc = useQueryClient();
  const toast = useToast();
  const locs = useLocations();
  const q = useQuery({ queryKey: ['box', id], queryFn: () => get<Box>(`/api/boxes/${id}`) });
  const [removeOpen, setRemoveOpen] = useState(false);
  const [name, setName] = useState<string | null>(null);
  const b = q.data;
  // Opening a box makes it the box being filled: an item scanned on any page now goes in here (lib/currentBox).
  useEffect(() => { if (b && !b.archived) setCurrentBox(b.id, boxName(b)); }, [b?.id, b?.name, b?.archived]); // eslint-disable-line react-hooks/exhaustive-deps
  if (q.error) return <Empty>{(q.error as Error).message}</Empty>;
  if (!b) return <Skeleton className="h-64" />;
  const refresh = () => qc.invalidateQueries({ queryKey: ['box', id] });
  const market = ({ A: 'Amazon', E: 'eBay', W: 'Walmart', L: 'Local' } as Record<string, string>)[(b.name || '').toUpperCase().match(/^([A-Z])\d/)?.[1] ?? ''];

  const moveTo = async (locationId: string) => {
    try {
      const r = await post<{ action_id: number }>('/api/move-boxes', { box_ids: [b.id], location_id: Number(locationId) });
      qc.invalidateQueries();
      toast(`Box ${boxName(b)} moved`, { undo: async () => { await post('/api/undo', { action_id: r.action_id }); qc.invalidateQueries(); } });
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Move failed', { tone: 'error' });
    }
  };
  const update = async (body: object) => {
    try { await post(`/api/boxes/${b.id}`, { ...body, seen: { name: b.name, notes: b.notes, archived: b.archived } }); refresh(); qc.invalidateQueries({ queryKey: ['boxes'] }); } catch (e) { toast(e instanceof Error ? e.message : 'Save failed', { tone: 'error' }); }
  };

  return (
    <div className="max-w-5xl space-y-4 sm:space-y-6">
      {/* Flow audit 11-12: the box is the largest thing on its page, then the scan line. Label, Move, Count and Take out
          are rare, so they sit under the contents, and the SB code and "counted never" are gone from the floor view. */}
      <div>
        <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">{boxName(b)}{b.archived ? <Badge variant="destructive">retired</Badge> : null}</h1>
        <p className="text-sm text-muted-foreground">{market ? `${market} · ` : ''}{units(b.units)} · <Link to={`/locations/${b.location_id}`} className="hover:underline">{placeText({ code: b.location_code, site: b.location_site, name: b.location_name })}</Link></p>
      </div>
      <RemoveDialog box={b} open={removeOpen} onOpenChange={setRemoveOpen} />
      <div className="grid gap-4 lg:grid-cols-3 sm:gap-6">
        <div className="min-w-0 space-y-4 lg:col-span-2">
          {!b.archived && <AddItems boxId={b.id} boxName={boxName(b)} />}
          <ContentsList rows={b.contents} empty="This box is empty" heading={`In ${boxName(b)} now: ${units(b.units)}`} />
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => printLabels([boxLabel(b)])}><Printer className="mr-1.5 h-4 w-4" />Print label</Button>
            <Button variant="outline" onClick={() => navigate(`/move?from=box:${b.id}`)}><MoveRight className="mr-1.5 h-4 w-4" />Move items</Button>
            <Button variant="outline" onClick={() => navigate(`/count?box=${b.id}`)}><ClipboardCheck className="mr-1.5 h-4 w-4" />Count</Button>
            {b.units > 0 && <Button variant="outline" onClick={() => setRemoveOpen(true)}><LogOut className="mr-1.5 h-4 w-4" />Take out</Button>}
          </div>
          <p className="text-xs text-muted-foreground">Label {prettyCode(b.scan_code)} · counted {b.last_counted ? ago(b.last_counted) : 'never'}</p>
        </div>
        <div className="min-w-0 space-y-4">
          <Card>
            <CardHeader className="pb-2"><CardTitle className="text-base">Box details</CardTitle></CardHeader>
            <CardContent className="space-y-3">
              <div className="space-y-1.5"><Label>Name on the label</Label>
                <div className="flex gap-2"><Input value={name ?? b.name} onChange={(e) => setName(e.target.value)} className="h-10" placeholder="The name written on it, like A31" autoCapitalize="characters" />
                  {name !== null && name !== b.name && <Button onClick={() => { void update({ name }); setName(null); }}>Save</Button>}</div>
                {b.renames?.map((r) => (
                  <p key={r.at} className="text-xs text-muted-foreground">
                    Renamed from {r.old_name} to {r.new_name} {when(r.at)}{r.user_name ? ` by ${r.user_name}` : ''}.
                    {r.new_name === b.name && ` A label printed before then still says ${r.old_name}: print a new one.`}
                  </p>
                ))}
              </div>
              <div className="space-y-1.5"><Label>Move whole box to</Label>
                <Select value="" onValueChange={moveTo}><SelectTrigger className="h-10 w-full"><SelectValue placeholder="Pick a location (or scan it on Move)" /></SelectTrigger>
                  <SelectContent>{locs.data?.locations.filter((l) => l.id !== b.location_id && l.kind !== 'virtual' && !l.archived).map((l) => <SelectItem key={l.id} value={String(l.id)}>{placeText(l)}</SelectItem>)}</SelectContent></Select>
              </div>
              {!b.archived && b.units === 0 && <Button variant="outline" size="sm" onClick={() => update({ archived: true })}><Archive className="mr-1.5 h-3.5 w-3.5" />Archive empty box</Button>}
              {!!b.archived && <Button variant="outline" size="sm" onClick={() => update({ archived: false })}>Restore box</Button>}
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2"><CardTitle className="text-base">History</CardTitle></CardHeader>
            <CardContent><ActivityList actions={b.history} compact onChange={refresh} /></CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
