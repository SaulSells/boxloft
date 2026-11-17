import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Plus } from 'lucide-react';
import { get, post } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useToast } from '@/components/app/toast';
import { plural } from '@/lib/format';
import { printLabels, targetLabel } from '@/lib/labels';
import { cn } from '@/lib/utils';

/*
 * Alex 11-11: stock is sold on Amazon, eBay, Walmart or locally, and a market is the box's letter
 * (A, E, W, L). Pick the market first: this offers its boxes in use and "Next box", which is the lowest empty
 * box, or a new one numbered after the highest Boxloft has, whose 4x6 label goes to the printer (or the print
 * queue on a phone).
 */

export const MARKETS = [
  { letter: 'A', name: 'Amazon' },
  { letter: 'E', name: 'eBay' },
  { letter: 'W', name: 'Walmart' },
  { letter: 'L', name: 'Local' },
] as const;

export const marketOf = (code: string | null | undefined) => {
  const m = /^([A-Z]+)-?\d+$/i.exec(code ?? '');
  return MARKETS.find((x) => x.letter === m?.[1]?.toUpperCase()) ?? null;
};

interface MarketBox { id: number; code: string; units: number }
interface MarketBoxes { market: string; name: string; boxes: MarketBox[]; next_empty: MarketBox | null; next_new: string }
export interface Target { type: 'box' | 'shelf'; id: number; code: string; name: string; scan_code: string; where: string; new?: boolean }

export function MarketPicker({ current, onPick, className, suggest, typed = true, exclude }: {
  /** A box that can't be picked (the one a move takes from): left out of the list. */
  exclude?: number | null;
  current?: string | null;
  /** The field for a box's handwritten name (A31). Stock audit has its own, so it turns this off. */
  typed?: boolean;
  /** Box letter of a market where the item is for sale now: shown first with "listed". Never picks a box by itself. */
  suggest?: string | null;
  onPick: (box: { id: number; code: string }) => void;
  className?: string;
}) {
  const toast = useToast();
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const active = marketOf(current)?.letter ?? null;
  const q = useQuery({ queryKey: ['market-boxes', open], queryFn: () => get<MarketBoxes>(`/api/markets/${open}/boxes`), enabled: !!open });
  const [name, setName] = useState('');
  const [offer, setOffer] = useState<{ name: string; retired: boolean } | null>(null);
  const pick = (b: { id: number; code: string }) => { setOpen(null); setName(''); setOffer(null); onPick(b); };
  // Alex 11-12: "says you can add a new box but has no place to type it". The name written on the box: that box if
  // Boxloft has it, else an offer to make it and print its label (the same calls as the Stock audit's Box bar).
  const findTyped = async () => {
    const n = name.trim();
    if (!n) return;
    try {
      const r = await post<{ kind: string; target?: Target; name?: string; retired?: boolean }>('/api/targets/find', { name: n });
      if (r.kind === 'target' && r.target?.type === 'box') pick(r.target);
      else if (r.kind === 'target') toast(`${r.target?.code ?? n} is a shelf, not a box. Pick or type a box here.`, { tone: 'error' });
      else setOffer({ name: r.name ?? n, retired: !!r.retired });
    } catch (e) {
      toast((e as Error).message, { tone: 'error' });
    }
  };
  const makeTyped = async () => {
    if (!offer) return;
    setBusy(true);
    try {
      const b = await post<Target>('/api/targets/make-box', { name: offer.name });
      if (b.new) {
        await printLabels([targetLabel(b)]);
        toast(`Box ${b.code} made. Its 4x6 label is printing (or waiting in the print queue).`);
      }
      pick(b);
    } catch (e) {
      toast((e as Error).message, { tone: 'error' });
    } finally {
      setBusy(false);
    }
  };
  const next = async () => {
    if (!open) return;
    setBusy(true);
    try {
      const b = await post<Target>(`/api/markets/${open}/next-box`);
      if (b.new) {
        await printLabels([targetLabel(b)]);
        toast(`New box ${b.code}. Its 4x6 label is printing (or waiting in the print queue).`);
      }
      pick(b);
    } catch (e) {
      toast((e as Error).message, { tone: 'error' });
    } finally {
      setBusy(false);
    }
  };
  const d = q.data;
  return (
    <div className={cn('space-y-2', className)}>
      <div className="grid grid-cols-4 gap-1.5">
        {[...MARKETS].sort((a, b) => Number(b.letter === suggest) - Number(a.letter === suggest)).map((m) => (
          <button key={m.letter} type="button" onClick={() => setOpen(open === m.letter ? null : m.letter)}
            className={cn('relative flex h-11 items-center justify-center gap-1 rounded-md border text-sm font-medium',
              suggest === m.letter && active !== m.letter && 'border-warning bg-warning/10',
              active === m.letter && 'border-primary bg-primary text-primary-foreground',
              open === m.letter && active !== m.letter && 'border-primary')}>
            <span className="text-xs opacity-70">{m.letter}</span>{m.name}
            {suggest === m.letter && <span className="absolute -top-2 right-1 rounded-full bg-warning px-1.5 text-[10px] font-semibold leading-4 text-black">listed</span>}
          </button>
        ))}
      </div>
      {open && (
        <div className="space-y-2 rounded-md border bg-muted/40 p-2">
          {!d ? <div className="text-sm text-muted-foreground">Loading {MARKETS.find((m) => m.letter === open)?.name} boxes...</div> : (
            <>
              <Button className="h-11 w-full" disabled={busy} onClick={next}>
                <Plus className="mr-1.5 h-4 w-4" />
                {d.next_empty ? `Next empty ${d.name} box: ${d.next_empty.code}` : `New ${d.name} box: ${d.next_new} (prints its label)`}
              </Button>
              {d.boxes.length > 0 && (
                <>
                  <div className="text-xs text-muted-foreground">{d.name} boxes with stock, last filled first:</div>
                  <div className="flex flex-wrap gap-1.5">
                    {d.boxes.filter((b) => b.id !== exclude).slice(0, 24).map((b) => (
                      <Button key={b.id} size="sm" variant={b.code === current ? 'default' : 'outline'} className="h-9" onClick={() => pick(b)}>
                        {b.code} <span className="ml-1 text-xs opacity-70">{plural(b.units, 'unit')}</span>
                      </Button>
                    ))}
                  </div>
                </>
              )}
              {typed && (
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Input value={name} onChange={(e) => { setName(e.target.value); setOffer(null); }} placeholder="Box with a handwritten name? Type it: A31"
                      onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void findTyped(); } }} className="h-11 flex-1" autoCapitalize="characters" autoCorrect="off" />
                    <Button variant="outline" className="h-11" disabled={!name.trim() || busy} onClick={() => void findTyped()}>Use</Button>
                  </div>
                  {offer && (
                    <div className="space-y-2 rounded-md border border-primary/40 bg-primary/5 p-2">
                      <div className="text-sm">{offer.retired ? `Box ${offer.name} was retired. Bring it back with a new label?` : `No box named ${offer.name} in Boxloft yet.`}</div>
                      <div className="flex flex-wrap gap-2">
                        <Button className="h-11" disabled={busy} onClick={() => void makeTyped()}><Plus className="mr-1.5 h-4 w-4" />Make box {offer.name} + print its label</Button>
                        <Button className="h-11" variant="ghost" onClick={() => setOffer(null)}>Cancel</Button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
