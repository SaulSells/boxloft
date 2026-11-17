import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Printer } from 'lucide-react';
import { get, post } from '@/api/client';
import type { Location, LocationRow } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
import { ContentsList, Empty, PageHeader } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { Link, navigate } from '@/lib/router';
import { plural, prettyCode, units } from '@/lib/format';
import { locationLabel, printLabels } from '@/lib/labels';

/*
 * Shelf codes: AISLE-BAY-SHELF[-BIN], e.g. A-01-03 or A-01-03-B. Number bays from the door in,
 * shelves from the floor up. Letters skip I and O so they never read as 1 and 0.
 */
const LETTERS = 'ABCDEFGHJKLMNPQRSTUVWXYZ'.split('');
const pad = (n: number) => String(n).padStart(2, '0');

function GeneratorDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [aisles, setAisles] = useState('A');
  const [bays, setBays] = useState('1-4');
  const [shelves, setShelves] = useState('1-5');
  const [bins, setBins] = useState('');
  const [single, setSingle] = useState('');
  const [print, setPrint] = useState(true);
  const range = (s: string) => {
    const m = s.trim().match(/^(\d+)\s*-\s*(\d+)$/);
    if (m) { const a = Number(m[1]), b = Number(m[2]); return a <= b && b - a < 100 ? Array.from({ length: b - a + 1 }, (_, i) => a + i) : []; }
    return s.trim() && /^\d+$/.test(s.trim()) ? [Number(s.trim())] : [];
  };
  const aisleList = (s: string) => {
    const m = s.trim().toUpperCase().match(/^([A-Z])\s*-\s*([A-Z])$/);
    if (m) { const a = LETTERS.indexOf(m[1]!), b = LETTERS.indexOf(m[2]!); return a >= 0 && b >= a ? LETTERS.slice(a, b + 1) : []; }
    return s.toUpperCase().split(/[\s,]+/).filter(Boolean);
  };
  const codes = useMemo(() => {
    if (single.trim()) return single.split(/[\n,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
    const out: string[] = [];
    const binList = bins.trim() ? aisleList(bins) : [''];
    for (const a of aisleList(aisles)) for (const b of range(bays)) for (const s of range(shelves)) for (const x of binList) out.push(`${a}-${pad(b)}-${pad(s)}${x ? `-${x}` : ''}`);
    return out;
  }, [aisles, bays, shelves, bins, single]);
  const create = useMutation({
    mutationFn: () => post<{ created: { id: number; code: string; scan_code: string }[] }>('/api/locations', { codes }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['locations'] });
      toast(`Created ${plural(r.created.length, 'location')}${r.created.length < codes.length ? ` (${codes.length - r.created.length} already existed)` : ''}`);
      onOpenChange(false);
      if (print && r.created.length) void printLabels(r.created.map((c) => locationLabel(c)));
    },
    onError: (e: Error) => toast(e.message, { tone: 'error' }),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>Add shelf locations</DialogTitle></DialogHeader>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5"><Label>Aisles</Label><Input value={aisles} onChange={(e) => setAisles(e.target.value)} placeholder="A or A-C" className="h-10" /></div>
          <div className="space-y-1.5"><Label>Bays (racks)</Label><Input value={bays} onChange={(e) => setBays(e.target.value)} placeholder="1-4" className="h-10" /></div>
          <div className="space-y-1.5"><Label>Shelves (floor up)</Label><Input value={shelves} onChange={(e) => setShelves(e.target.value)} placeholder="1-5" className="h-10" /></div>
          <div className="space-y-1.5"><Label>Bins (optional)</Label><Input value={bins} onChange={(e) => setBins(e.target.value)} placeholder="A-C" className="h-10" /></div>
        </div>
        <div className="space-y-1.5"><Label>Or type codes (one per line), e.g. PACK, FBA-PREP, OVERFLOW</Label>
          <textarea value={single} onChange={(e) => setSingle(e.target.value)} rows={2} className="w-full rounded-md border bg-transparent p-2 text-sm" /></div>
        <div className="max-h-28 overflow-y-auto rounded-md bg-muted p-2 font-mono text-xs">{codes.slice(0, 60).join('  ')}{codes.length > 60 ? ` ... (${codes.length})` : ''}</div>
        <div className="flex items-center gap-2"><Checkbox id="prl" checked={print} onCheckedChange={(v) => setPrint(!!v)} /><Label htmlFor="prl">Print shelf labels now</Label></div>
        <DialogFooter><Button className="h-11" disabled={!codes.length || codes.length > 500 || create.isPending} onClick={() => create.mutate()}>Create {codes.length}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function Locations() {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const q = useQuery({ queryKey: ['locations'], queryFn: () => get<{ locations: LocationRow[] }>('/api/locations') });
  const rows = q.data?.locations ?? [];
  return (
    <div>
      <PageHeader title="Locations" subtitle="Shelves and spots. Boxes sit on them; you can start with just RECV and add shelves later."
        actions={<>
          {selected.size > 0 && <Button variant="outline" onClick={() => printLabels(rows.filter((l) => selected.has(l.id)).map(locationLabel))}><Printer className="mr-1.5 h-4 w-4" />Print {selected.size}</Button>}
          <Button onClick={() => setOpen(true)}><Plus className="mr-1.5 h-4 w-4" />Add locations</Button>
        </>} />
      <GeneratorDialog open={open} onOpenChange={setOpen} />
      {q.data && !rows.length ? <Empty>No locations yet</Empty> : (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {rows.map((l) => (
            <div key={l.id} className="flex items-center gap-3 rounded-lg border bg-card p-3">
              <Checkbox checked={selected.has(l.id)} onCheckedChange={() => setSelected((s) => { const n = new Set(s); if (n.has(l.id)) n.delete(l.id); else n.add(l.id); return n; })} />
              <button className="min-w-0 flex-1 text-left" onClick={() => navigate(`/locations/${l.id}`)}>
                <div className="font-mono font-bold">{l.code}</div>
                <div className="text-xs text-muted-foreground">{l.kind === 'dock' ? 'Dock · ' : ''}{plural(l.boxes, 'box', 'boxes')} · {units(l.units)}</div>
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function LocationPage({ id }: { id: number }) {
  const q = useQuery({ queryKey: ['location', id], queryFn: () => get<Location>(`/api/locations/${id}`) });
  const l = q.data;
  if (q.error) return <Empty>{(q.error as Error).message}</Empty>;
  if (!l) return <Skeleton className="h-64" />;
  return (
    <div className="max-w-5xl space-y-4 sm:space-y-6">
      <PageHeader title={<span className="font-mono">{l.code}</span>} subtitle={`${prettyCode(l.scan_code)}${l.name ? ` · ${l.name}` : ''} · ${plural(l.boxes.length, 'box', 'boxes')}`}
        actions={<Button variant="outline" onClick={() => printLabels([locationLabel(l)])}><Printer className="mr-1.5 h-4 w-4" />Label</Button>} />
      <div className="grid gap-4 lg:grid-cols-2 sm:gap-6 [&>*]:min-w-0">
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Boxes here</CardTitle></CardHeader>
          <CardContent>
            {l.boxes.length ? <div className="divide-y text-sm">{l.boxes.map((b) => (
              <Link key={b.id} to={`/boxes/${b.id}`} className="flex justify-between py-2 hover:text-primary"><span>{b.name || `Box ${b.id}`}</span><span className="tabular-nums">{units(b.units)}</span></Link>
            ))}</div> : <Empty>No boxes here</Empty>}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Loose items (not in a box)</CardTitle></CardHeader>
          <CardContent><ContentsList rows={l.loose} empty="None" /></CardContent>
        </Card>
      </div>
    </div>
  );
}
