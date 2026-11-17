import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Printer } from 'lucide-react';
import { get, post } from '@/api/client';
import type { Unit } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Empty, PageHeader, Stat } from '@/components/app/common';
import { PhotoStrip } from '@/pages/Product';
import { Link } from '@/lib/router';
import { money, prettyCode, when } from '@/lib/format';
import { printLabels, unitLabel } from '@/lib/labels';

export function UnitPage({ id }: { id: number }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['unit', id], queryFn: () => get<Unit>(`/api/units/${id}`) });
  const [cond, setCond] = useState<string | null>(null);
  const [notes, setNotes] = useState<string | null>(null);
  const u = q.data;
  if (q.error) return <Empty>{(q.error as Error).message}</Empty>;
  if (!u) return <Skeleton className="h-64" />;
  const refresh = () => qc.invalidateQueries({ queryKey: ['unit', id] });
  const save = async () => { await post(`/api/units/${id}`, { condition: cond ?? u.condition, notes: notes ?? u.notes, seen: { condition: u.condition, notes: u.notes } }); setCond(null); setNotes(null); refresh(); };
  const h = u.holder;
  return (
    <div className="max-w-3xl space-y-4 sm:space-y-6">
      <PageHeader title={`Unit ${u.id}`} subtitle={<span>{prettyCode(u.scan_code)} · <Link to={`/inventory/${u.product_id}`} className="text-primary hover:underline">{u.title}</Link> · <span className="font-mono">{u.sku}</span></span>}
        actions={<Button variant="outline" onClick={() => printLabels([unitLabel({ ...u, condition: u.condition })])}><Printer className="mr-1.5 h-4 w-4" />Label</Button>} />
      <PhotoStrip photos={u.photos} uploadTo={`/api/photos?unit_id=${u.id}`} onChange={refresh} />
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
        <Stat label="Where" value={h ? (h.box_id ? <Link to={`/boxes/${h.box_id}`} className="text-primary hover:underline">Box {h.box_id}</Link> : h.location_code) : '—'} hint={h?.location_kind === 'virtual' ? 'not on the shelves' : h?.location_code} />
        <Stat label="Cost" value={money(u.cost_cents)} />
        <Stat label="Received" value={<span className="text-base">{when(u.created_at)}</span>} />
      </div>
      <Card><CardContent className="space-y-3 p-4 sm:p-6">
        <div className="space-y-1.5"><Label>Condition</Label><Input value={cond ?? u.condition} onChange={(e) => setCond(e.target.value)} className="h-10" /></div>
        <div className="space-y-1.5"><Label>Notes (flaws, what is included)</Label><Input value={notes ?? u.notes} onChange={(e) => setNotes(e.target.value)} className="h-10" /></div>
        {(cond !== null || notes !== null) && <Button onClick={save}>Save</Button>}
      </CardContent></Card>
    </div>
  );
}
