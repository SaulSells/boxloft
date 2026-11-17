import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { PackageCheck } from 'lucide-react';
import { get, post, ApiError } from '@/api/client';
import type { ShipBox } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Thumb } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { useLocation } from '@/lib/router';
import { useAuth } from '@/context/AuthContext';
import { plural } from '@/lib/format';

/** Ship box (Alex 11-11). Orders Alex ships in Seller Central are scanned out here by Boxloft by itself; this asks
 *  the person packing which box each one went out in. Amazon's label size is chosen already when Boxloft has it.
 *  The answer teaches the next suggestion for that item (backend/shipbox.py). */
interface PendingLine {
  id: number;
  title: string;
  qty: number;
  external_id: string;
  channel: string;
  shipped_at: string;
  photo_id: number | null;
  image_url: string | null;
  ship_box_id: number | null;
  suggested: number | null;
  amazon: { dims: string; box_id: number | null; carrier: string; weight_oz: number | null; label_cents: number | null } | null;
}


function labelText(a: PendingLine['amazon']) {
  if (!a) return '';
  const parts = [a.dims, a.carrier, a.label_cents != null ? `$${(a.label_cents / 100).toFixed(2)}` : '',
    a.weight_oz ? `${a.weight_oz >= 16 ? `${(a.weight_oz / 16).toFixed(1)} lb` : `${a.weight_oz} oz`}` : ''];
  return `Amazon label: ${parts.filter(Boolean).join(', ')}`;
}

function LineRow({ line, boxes }: { line: PendingLine; boxes: ShipBox[] }) {
  const qc = useQueryClient();
  const toast = useToast();
  const amazonOther = line.amazon && !line.amazon.box_id ? line.amazon.dims.replace(/ in$/, '') : '';
  const [pick, setPick] = useState(line.suggested ? String(line.suggested) : amazonOther ? 'other' : '');
  const [other, setOther] = useState(amazonOther);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      await post(`/api/ship-confirm/${line.id}`, {
        ship_box_id: pick && pick !== 'other' && pick !== 'own' ? Number(pick) : null,
        other: pick === 'other' ? other : '', own: pick === 'own',
        seen: { box_confirmed_at: null },
      });
      toast(`Box saved for order ${line.external_id}`);
      qc.invalidateQueries({ queryKey: ['ship-confirm'] });
      qc.invalidateQueries({ queryKey: ['ship-boxes'] });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Not saved', { tone: 'error' });
      qc.invalidateQueries({ queryKey: ['ship-confirm'] });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex gap-3">
        <Thumb photoId={line.photo_id} url={line.image_url} size="dense" />
        <div className="min-w-0 flex-1 text-sm">
          <p className="line-clamp-2 font-medium">{line.qty > 1 ? `${line.qty} × ` : ''}{line.title}</p>
          <p className="text-xs text-muted-foreground">Order {line.external_id}</p>
          {line.amazon && <p className="text-xs">{labelText(line.amazon)}</p>}
        </div>
      </div>
      <Select value={pick} onValueChange={setPick}>
        <SelectTrigger className="h-11 w-full"><SelectValue placeholder="Which box did it go out in?" /></SelectTrigger>
        <SelectContent>
          {boxes.map((b) => {
            const size = `${b.inner_l_in} x ${b.inner_w_in} x ${b.inner_h_in}`;
            return <SelectItem key={b.id} value={String(b.id)}>{b.name === size ? `${size} in` : `${b.name} · ${size} in`}</SelectItem>;
          })}
          <SelectItem value="other">Other size (type it)</SelectItem>
          <SelectItem value="own">Its own packaging</SelectItem>
        </SelectContent>
      </Select>
      {pick === 'other' && (
        <Input className="h-11" inputMode="decimal" placeholder="L x W x H in inches, like 12 x 10 x 6" value={other} onChange={(e) => setOther(e.target.value)} />
      )}
      <Button className="h-11 w-full" disabled={busy || !pick || (pick === 'other' && !other.trim())} onClick={save}>
        <PackageCheck className="mr-1.5 h-4 w-4" />{busy ? 'Saving...' : 'This is the box'}
      </Button>
    </div>
  );
}

/* Flow audit 11-12: the popup opened over Sam's Home for orders Alex shipped, and after Later a 90 px banner sat on
 * every page for both people. The carton question is Alex's: one line on his Home, opened when he taps it. */
export function BoxConfirm() {
  const { path } = useLocation();
  const { user } = useAuth();
  const mine = !!user?.is_admin && path === '/';
  const q = useQuery({
    queryKey: ['ship-confirm'],
    queryFn: () => get<{ lines: PendingLine[]; boxes: ShipBox[] }>('/api/ship-confirm'),
    refetchInterval: 60000,
    enabled: !!user?.is_admin,
  });
  const [open, setOpen] = useState(false);
  const lines = q.data?.lines ?? [];
  useEffect(() => { if (open && !lines.length) setOpen(false); }, [open, lines.length]);

  if (!lines.length || (!mine && !open)) return null;
  return (
    <>
      {!open && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-3 py-2 text-sm sm:px-4">
          <PackageCheck className="h-4 w-4 shrink-0" />
          <span className="min-w-0 flex-1"><b>{plural(lines.length, 'shipped order')}</b>: which box did {lines.length === 1 ? 'it' : 'they'} go out in?</span>
          <Button size="sm" onClick={() => setOpen(true)}>Answer</Button>
        </div>
      )}
      <Dialog open={open} onOpenChange={(v) => { if (!v) setOpen(false); }}>
        <DialogContent className="max-h-[92vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Which box did {lines.length === 1 ? 'this order' : 'these orders'} go out in?</DialogTitle>
            <DialogDescription>Shipped in Seller Central, so Boxloft took {lines.length === 1 ? 'it' : 'them'} off the shelf. Your answer is the box suggested next time.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">{lines.map((l) => <LineRow key={l.id} line={l} boxes={q.data?.boxes ?? []} />)}</div>
          <DialogFooter><Button variant="outline" className="h-11 w-full" onClick={() => setOpen(false)}>Later</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
