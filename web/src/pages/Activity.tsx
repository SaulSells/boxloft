import { useState } from 'react';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronRight, Download, Undo2 } from 'lucide-react';
import { get, post } from '@/api/client';
import type { ActionRow } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Empty, PageHeader } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { Link } from '@/lib/router';
import { when } from '@/lib/format';
import { cn } from '@/lib/utils';

const KIND_TONE: Record<string, string> = {
  receive: 'bg-primary/15 text-primary', move: 'bg-info/15 text-info-ink', box_move: 'bg-info/15 text-info-ink',
  count: 'bg-warning/15 text-warning-ink', remove: 'bg-rose-500/15 text-rose-700 dark:text-rose-300', undo: 'bg-muted text-muted-foreground',
};
const KIND_NAME: Record<string, string> = { receive: 'Receive', move: 'Move', box_move: 'Box move', count: 'Count', remove: 'Out', undo: 'Undo' };

interface Detail {
  moves: { id: number; sku: string; title: string; qty: number; unit_id: number; from_box_id: number; to_box_id: number; from_location_code: string | null; to_location_code: string | null }[];
  box_moves: { box_id: number; from_code: string; to_code: string }[];
  count_lines: { sku: string; title: string; expected: number; counted: number; unit_id: number }[];
  device: string;
}

function Holder({ box, loc }: { box: number; loc: string | null }) {
  return box ? <Link to={`/boxes/${box}`} className="text-primary hover:underline">Box {box}</Link> : <span className="font-mono">{loc}</span>;
}

function ActionDetail({ id }: { id: number }) {
  const q = useQuery({ queryKey: ['action', id], queryFn: () => get<Detail>(`/api/actions/${id}`) });
  if (!q.data) return <div className="py-2 text-xs text-muted-foreground">Loading...</div>;
  const d = q.data;
  return (
    <div className="space-y-1 py-2 text-xs">
      {d.count_lines.length > 0 && d.count_lines.map((c, i) => (
        <div key={i} className={cn(c.expected !== c.counted && 'font-medium')}>{c.sku}{c.unit_id ? ` unit ${c.unit_id}` : ''}: expected {c.expected}, counted {c.counted}</div>
      ))}
      {d.moves.map((m) => (
        <div key={m.id}>{m.qty} × <span className="font-mono">{m.sku}</span>{m.unit_id ? ` (unit ${m.unit_id})` : ''}: <Holder box={m.from_box_id} loc={m.from_location_code} /> → <Holder box={m.to_box_id} loc={m.to_location_code} /></div>
      ))}
      {d.box_moves.map((b, i) => <div key={i}><Link to={`/boxes/${b.box_id}`} className="text-primary hover:underline">Box {b.box_id}</Link>: {b.from_code} → {b.to_code}</div>)}
      {d.device && <div className="text-muted-foreground">Device: {d.device}</div>}
    </div>
  );
}

export function ActivityList({ actions, compact, onChange }: { actions: ActionRow[]; compact?: boolean; onChange?: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState<number | null>(null);
  const [confirm, setConfirm] = useState<number | null>(null);
  if (!actions.length) return <Empty>No activity yet</Empty>;
  const undo = async (id: number) => {
    try {
      const r = await post<{ message?: string }>('/api/undo', { action_id: id });
      toast(r?.message ?? 'Undone. The original stays in the history.');
      qc.invalidateQueries();
      onChange?.();
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Undo failed', { tone: 'error' });
    } finally {
      setConfirm(null);
    }
  };
  return (
    <div className="divide-y">
      {actions.map((a) => (
        <div key={a.id} className="py-2">
          <div className="flex items-start gap-2">
            <button className="mt-0.5 text-muted-foreground" onClick={() => setOpen(open === a.id ? null : a.id)} aria-label="Details">
              {open === a.id ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            </button>
            <div className="min-w-0 flex-1">
              <div className={cn('text-sm', a.reversed_by && 'text-muted-foreground line-through')}>
                {/* The summary names the kind already ("Received 2 x ..."): the chip then read "ReceiveReceived". */}
                {!(a.summary || '').toLowerCase().startsWith((KIND_NAME[a.kind] ?? a.kind).toLowerCase().slice(0, 4)) &&
                  <span className={cn('mr-2 rounded-full px-2 py-0.5 text-xs font-semibold', KIND_TONE[a.kind])}>{KIND_NAME[a.kind] ?? a.kind}</span>}
                {a.summary}
              </div>
              <div className="text-xs text-muted-foreground">{when(a.at)} · {a.user_name}{a.note ? ` · ${a.note}` : ''}{a.reversed_by ? ' · undone' : ''}</div>
              {open === a.id && <ActionDetail id={a.id} />}
            </div>
            {!a.reversed_by && !a.reverses_action_id && (
              confirm === a.id ? (
                <div className="flex shrink-0 gap-1">
                  <Button size="sm" variant="destructive" onClick={() => undo(a.id)}>Undo it</Button>
                  <Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>Keep</Button>
                </div>
              ) : (
                <Button size="sm" variant="ghost" className={cn('shrink-0', compact && 'h-7 px-2')} onClick={() => setConfirm(a.id)}><Undo2 className="h-3.5 w-3.5 sm:mr-1" /><span className="hidden sm:inline">Undo</span></Button>
              )
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

export function ActivityPage() {
  const [kind, setKind] = useState('all');
  const q = useInfiniteQuery({
    queryKey: ['actions', kind],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => get<{ actions: ActionRow[] }>(`/api/actions?limit=100${pageParam ? `&before=${pageParam}` : ''}${kind !== 'all' ? `&kind=${kind}` : ''}`),
    getNextPageParam: (last) => (last.actions.length === 100 ? last.actions[last.actions.length - 1]!.id : undefined),
  });
  const actions = q.data?.pages.flatMap((p) => p.actions) ?? [];
  return (
    <div className="max-w-5xl">
      <PageHeader
        title="Activity"
        subtitle="Every change, who made it and when." about="Nothing is ever edited or deleted: undo writes the opposite moves."
        actions={<Button variant="outline" asChild><a href="/api/export/ledger.csv"><Download className="mr-1.5 h-4 w-4" />Ledger CSV</a></Button>}
      />
      <div className="mb-3">
        <Select value={kind} onValueChange={setKind}>
          <SelectTrigger className="h-10 w-48"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All activity</SelectItem>
            {Object.entries(KIND_NAME).map(([k, v]) => <SelectItem key={k} value={k}>{v}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      <div className="rounded-lg border bg-card px-3"><ActivityList actions={actions} /></div>
      {q.hasNextPage && <Button variant="outline" className="mt-3" onClick={() => q.fetchNextPage()}>Load more</Button>}
      <p className="mt-3 text-xs text-muted-foreground"><Badge variant="outline">Tip</Badge> Undo is blocked when stock has moved on since. Then fix it with a new move or a count.</p>
    </div>
  );
}
