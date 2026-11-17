import { useState, type ReactNode } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronRight, ImageOff, Printer, ScanLine } from 'lucide-react';
import { get, post } from '@/api/client';
import type { BoxRow, ContentRow, Holder, LocationRow, ScanResult } from '@/api/types';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Link, navigate } from '@/lib/router';
import { useScan, useScanBus } from '@/lib/scan';
import { money, shortName, units } from '@/lib/format';
import { cn } from '@/lib/utils';
import { HOME_SITE, NAME_RE, boxName, buildingOf, isShelf, placeText, spotOf } from '@/lib/places';
import { printLabels, targetLabel } from '@/lib/labels';
import type { Target } from '@/components/app/MarketPicker';

/** Page title, a one-line subtitle, and the longer explanation folded under "About this page". */
export function PageHeader({ title, subtitle, about, actions }: { title: ReactNode; subtitle?: ReactNode; about?: ReactNode; actions?: ReactNode }) {
  // A product name as the title: smaller, so a long name doesn't fill the phone screen.
  const long = typeof title === 'string' && title.length > 40;
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3 sm:mb-6">
      <div className="min-w-0">
        <h1 className={long ? 'text-xl font-semibold leading-snug tracking-tight sm:text-2xl' : 'text-2xl font-bold tracking-tight sm:text-[28px]'}>{title}</h1>
        {subtitle && <div className="mt-1 text-sm text-muted-foreground">{subtitle}</div>}
        {about && (
          <details className="group mt-1 text-sm">
            <summary className="inline-flex min-h-9 cursor-pointer list-none items-center gap-1 font-medium underline underline-offset-4 [&::-webkit-details-marker]:hidden">
              About this page
            </summary>
            <div className="mt-1 max-w-2xl space-y-2 text-muted-foreground">{about}</div>
          </details>
        )}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: 'warn' | 'bad' }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs font-medium text-muted-foreground">{label}</div>
        <div className={cn('mt-1 text-2xl font-bold tabular-nums tracking-tight', tone === 'warn' && 'text-warning-ink', tone === 'bad' && 'text-destructive')}>{value}</div>
        {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </Card>
  );
}

/** Product picture sizes by role: dense tables, list rows, the item being worked on, the product gallery. */
const THUMB = { dense: 'size-10', row: 'size-14', focus: 'size-20 sm:size-24', gallery: 'size-28' } as const;

/** A saved photo; `url` (e.g. the Amazon photo) shows while there is no saved copy yet. The whole picture
 *  shows (contain on white), and a picture that fails to load shows the no-photo icon. */
export function Thumb({ photoId, url, size = 'row', className }: {
  photoId: number | null | undefined; url?: string | null; size?: keyof typeof THUMB; className?: string;
}) {
  const src = photoId ? `/api/photos/${photoId}` : url || null;
  const [bad, setBad] = useState<string | null>(null);
  const box = cn('shrink-0 rounded-md border', THUMB[size], className);
  if (!src || src === bad) {
    return (
      <div className={cn(box, 'flex items-center justify-center bg-muted text-muted-foreground')}>
        <ImageOff className="h-4 w-4 opacity-50" />
      </div>
    );
  }
  return <img src={src} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setBad(src)} className={cn(box, 'bg-white object-contain')} />;
}

/** Filter chips: one row that scrolls sideways on a phone. The chosen one is tinted, not filled,
 *  so it never looks like the page's main action. */
export function Chips<K extends string>({ items, value, onChange }: {
  items: { key: K; label: ReactNode; count?: ReactNode }[];
  value: K;
  onChange: (k: K) => void;
}) {
  return (
    <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1 [scrollbar-width:none] sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0 sm:pb-0" role="tablist">
      {items.map((t) => {
        const on = t.key === value;
        return (
          <button key={t.key} type="button" role="tab" aria-selected={on} onClick={() => onChange(t.key)}
            className={cn('inline-flex h-11 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-3.5 text-sm transition-colors sm:h-9',
              on ? 'border-primary bg-primary/10 font-semibold text-foreground' : 'bg-card font-medium hover:bg-accent')}>
            {t.label}{t.count !== undefined && t.count !== null && t.count !== '' && <span className="tabular-nums opacity-75">{t.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

/** A section heading inside a page: 16px semibold, with an optional count and a one-line hint. */
export function SectionTitle({ children, count, hint }: { children: ReactNode; count?: ReactNode; hint?: ReactNode }) {
  return (
    <div>
      <h2 className="text-base font-semibold">{children}{count !== undefined && <span className="font-normal text-muted-foreground"> ({count})</span>}</h2>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">{children}</div>;
}

export function CodeBadge({ code }: { code: string }) {
  return <Badge variant="outline" className="font-mono text-xs">{shortName(code)}</Badge>;
}

/** A row you tap to open or link something (Alex 11-12: "not clear that it's clickable"). One look everywhere: light
 *  green tile, green border, and the action named on the right (TapAction). Tokens --tap / --tap-border in globals.css. */
export const tapTile = 'tap-row rounded-lg border-2 border-[hsl(var(--tap-border))] bg-[hsl(var(--tap))] text-left '
  + 'transition-[filter] hover:brightness-[0.97] active:brightness-95 disabled:opacity-60';

export function TapAction({ label }: { label?: string }) {
  return (
    <span className="flex shrink-0 items-center gap-0.5 text-sm font-semibold text-primary" aria-hidden>
      {label}<ChevronRight className="h-5 w-5" />
    </span>
  );
}

export function ContentsList({ rows, onPick, picked, empty = 'Empty', heading }: {
  rows: ContentRow[];
  onPick?: (r: ContentRow) => void;
  picked?: (r: ContentRow) => number;
  empty?: string;
  /** Box page (Alex 11-12 Hydro Flask): the contents repeat a unit the scan line just showed. Say it is the same unit. */
  heading?: string;
}) {
  if (!rows.length) return <Empty>{empty}</Empty>;
  const href = (r: ContentRow) => (r.unit_id ? `/units/${r.unit_id}` : `/inventory/${r.product_id}`);
  if (!onPick) {   // box and shelf pages (Alex 11-12): the whole row opens the item, green so it reads as a button
    return (
      <div className="space-y-2">
        {heading && <h2 className="pt-1 text-base font-semibold">{heading}</h2>}
        <p className="text-xs text-muted-foreground">
          {heading ? 'Everything in it now, including what you just put in (the same units, not extra). ' : ''}
          Tap an item to open its page: where it is, its orders and buys, photos and history.
        </p>
        {rows.map((r) => (
          <button key={`${r.product_id}-${r.unit_id}`} type="button" onClick={() => navigate(href(r))}
            className={cn('flex min-h-16 w-full items-center gap-3 p-3', tapTile)}>
            <Thumb photoId={r.photo_id} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">{r.title}</div>
              <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                <span className="font-mono">{r.sku}</span>
                {r.unit_code && <CodeBadge code={r.unit_code} />}
                {r.unit_condition && <span>{r.unit_condition}</span>}
                {r.owner !== 'Boxloft' && <Badge variant="secondary">{r.owner}</Badge>}
                {r.unit_cost_cents !== null && <span>{money(r.unit_cost_cents)}</span>}
              </div>
            </div>
            <div className="shrink-0 text-sm font-bold tabular-nums">{r.qty}</div>
            <TapAction />
          </button>
        ))}
      </div>
    );
  }
  return (
    <div className="divide-y rounded-lg border">
      {rows.map((r) => {
        const sel = picked?.(r) ?? 0;
        return (
          <div
            key={`${r.product_id}-${r.unit_id}`}
            className={cn('flex cursor-pointer items-center gap-3 p-3 hover:bg-muted/50', sel > 0 && 'bg-primary/10')}
            onClick={() => onPick?.(r)}
          >
            <Thumb photoId={r.photo_id} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">
                <Link to={href(r)} onClick={(e) => e.stopPropagation()} className="hover:underline">
                  {r.title}
                </Link>
              </div>
              <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                <span className="font-mono">{r.sku}</span>
                {r.unit_code && <CodeBadge code={r.unit_code} />}
                {r.unit_condition && <span>{r.unit_condition}</span>}
                {r.owner !== 'Boxloft' && <Badge variant="secondary">{r.owner}</Badge>}
                {r.unit_cost_cents !== null && <span>{money(r.unit_cost_cents)}</span>}
              </div>
            </div>
            <div className="text-right tabular-nums">
              {sel > 0 && <div className="text-sm font-bold text-primary">{sel} picked</div>}
              <div className={cn('text-sm', sel > 0 ? 'text-muted-foreground' : 'font-bold')}>{r.qty}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** "Main warehouse · Back room", "Alex's house", or "Shelf S1". */
export function placeLabel(l: { code: string; name: string; site?: string }): string {
  if (l.site) return l.name && l.name !== l.site ? `${l.site} · ${l.name}` : l.site;
  return l.name || `Shelf ${l.code}`;
}

/** A spot inside its building: "Shelf S2", "Receiving dock", or "Anywhere here" when the building is the place. */
const spotLabel = (l: LocationRow) => spotOf(l) || 'Anywhere here';
const PICK_MAX = 8;

function Choice({ active, onClick, children }: { active?: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick}
      className={cn('min-h-11 rounded-md border px-3 py-2 text-left text-sm', active ? 'border-primary bg-primary/10 font-medium' : 'hover:bg-muted/50')}>
      {children}
    </button>
  );
}

/** Choose a box or a place (Alex 11-11 12:20 PM: boxes sit on shelves; open shelving holds items loose on named
 *  shelves). Scan its label, or three short steps: 1. the building or room, 2. in a box or loose on a shelf,
 *  3. search by name (8 results at most). A name nobody has yet (A31, S4) can be made here, and its label prints. */
export function HolderPicker({ value, onChange, label = 'Where', allowLocations = true, listen = true, exclude }: {
  value: Holder | null;
  onChange: (h: Holder, name: string) => void;
  label?: string;
  allowLocations?: boolean;
  listen?: boolean;
  exclude?: Holder | null;
}) {
  const { feedback } = useScanBus();
  const qc = useQueryClient();
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [site, setSite] = useState<string | null>(null);
  const [mode, setMode] = useState<'loose' | 'box' | null>(allowLocations ? null : 'box');
  const [q, setQ] = useState('');
  const [making, setMaking] = useState(false);
  const boxes = useQuery({ queryKey: ['boxes'], queryFn: () => get<{ boxes: BoxRow[] }>('/api/boxes') });
  const locs = useQuery({ queryKey: ['locations'], queryFn: () => get<{ locations: LocationRow[] }>('/api/locations') });

  const reset = () => { setEditing(false); setSite(null); setMode(allowLocations ? null : 'box'); setQ(''); };
  const pick = (h: Holder, name: string) => { onChange(h, name); setErr(null); reset(); };

  useScan(async (code) => {
    const r = await get<ScanResult>(`/api/scan?code=${encodeURIComponent(code)}`);
    if (r.kind === 'box') {
      pick({ box_id: r.box.id }, boxName(r.box));
      feedback(true);
    } else if (r.kind === 'location' && allowLocations) {
      pick({ location_id: r.location.id }, r.location.code);
      feedback(true);
    } else {
      feedback(false);
      setErr(r.kind === 'bad_check' || r.kind === 'unknown' ? r.message : `That is a ${r.kind}. Scan a box${allowLocations ? ' or a shelf' : ''}.`);
    }
  }, listen);

  const ex = exclude ? ('box_id' in exclude ? `b:${exclude.box_id}` : `l:${exclude.location_id}`) : '';
  const places = (locs.data?.locations ?? []).filter((l) => l.kind !== 'virtual' && !l.archived);
  const placeById = new Map(places.map((l) => [l.id, l]));
  const allBoxes = (boxes.data?.boxes ?? []).filter((b) => !b.archived);
  const boxSite = (b: BoxRow) => { const l = placeById.get(b.location_id); return l ? buildingOf(l) : HOME_SITE; };
  const sites = [...new Set([...places.map(buildingOf), ...allBoxes.map(boxSite)])]
    .sort((a, b) => (a === HOME_SITE ? -1 : b === HOME_SITE ? 1 : a.localeCompare(b)));

  const describe = (h: Holder): string => {
    if ('box_id' in h) {
      const b = allBoxes.find((x) => x.id === h.box_id);
      const l = b ? placeById.get(b.location_id) : undefined;
      return `Box ${b ? boxName(b) : h.box_id}${l ? ` · ${placeText(l)}` : ''}`;
    }
    const l = placeById.get(h.location_id);
    if (!l) return 'That place';
    return `${placeText(l)} (loose)`;
  };

  const step = editing || !value;
  const needle = q.trim().toUpperCase().replace(/^([A-Z]{1,3})-(\d)/, '$1$2');
  const spots = site ? places.filter((l) => buildingOf(l) === site && `l:${l.id}` !== ex) : [];
  // The building itself (where boxes sit) is a loose spot only when it has no named spots: Alex's house, not the warehouse.
  const looseSpots = spots.filter((l) => spotOf(l)).length ? spots.filter((l) => spotOf(l)) : spots;
  const shelfHits = looseSpots.filter((l) => !needle || l.code.toUpperCase().startsWith(needle) || spotLabel(l).toUpperCase().includes(needle)).slice(0, PICK_MAX);
  const exactShelf = looseSpots.find((l) => l.code.toUpperCase() === needle);
  const siteBoxes = site ? allBoxes.filter((b) => boxSite(b) === site && `b:${b.id}` !== ex) : [];
  const exactBox = siteBoxes.find((b) => b.name.toUpperCase() === needle) ?? allBoxes.find((b) => b.name.toUpperCase() === needle);
  const boxHits = [
    ...(exactBox ? [exactBox] : []),
    ...siteBoxes.filter((b) => b !== exactBox && (needle ? String(b.id) === needle.replace(/^0+/, '') || b.name.toUpperCase().startsWith(needle)
      || b.notes.toUpperCase().includes(needle) : b.units > 0)),
  ];
  const shownBoxes = boxHits.slice(0, PICK_MAX);
  const canMake = NAME_RE.test(needle);
  const pickLoc = (l: LocationRow) => pick({ location_id: l.id }, isShelf(l) ? l.code : placeLabel(l));
  const pickBox = (b: BoxRow) => pick({ box_id: b.id }, boxName(b));
  const home = site ? spots.find((l) => !spotOf(l)) : undefined;   // the building itself: a new box sits there

  const make = async (what: 'box' | 'shelf') => {
    setMaking(true);
    try {
      const t = await post<Target>(`/api/targets/make-${what}`, what === 'box'
        ? { name: needle, location_id: site === HOME_SITE ? null : home?.id ?? null }
        : { name: needle, site });
      if (t.new) await printLabels([targetLabel(t)]);
      void qc.invalidateQueries({ queryKey: ['boxes'] });
      void qc.invalidateQueries({ queryKey: ['locations'] });
      pick(what === 'box' ? { box_id: t.id } : { location_id: t.id }, t.code);
    } catch (e) { setErr((e as Error).message); } finally { setMaking(false); }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-sm font-medium">
        {label} <span className="flex items-center gap-1 text-xs font-normal text-muted-foreground"><ScanLine className="h-3.5 w-3.5" /> scan a label, or choose below</span>
      </div>
      {!step && value && (
        <div className="flex items-center gap-2 rounded-md border border-primary bg-primary/5 px-3 py-2 text-sm">
          <span className="min-w-0 flex-1 font-medium">{describe(value)}</span>
          <button type="button" className="text-sm underline" onClick={() => setEditing(true)}>Change</button>
        </div>
      )}
      {step && (
        <div className="space-y-3 rounded-md border p-3">
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-muted-foreground">1. Which building or room?</p>
            <div className="flex flex-wrap gap-2">
              {sites.map((x) => (
                <Choice key={x} active={site === x} onClick={() => { setSite(x); setMode(allowLocations ? null : 'box'); setQ(''); }}>{x}</Choice>
              ))}
            </div>
          </div>
          {site && allowLocations && (
            <div className="space-y-1.5">
              <p className="text-xs font-medium text-muted-foreground">2. In a box, or loose on a shelf?</p>
              <div className="grid grid-cols-2 gap-2">
                <Choice active={mode === 'box'} onClick={() => { setMode('box'); setQ(''); }}>
                  In a box{siteBoxes.length ? ` (${siteBoxes.length})` : ''}
                </Choice>
                {looseSpots.length > 0 && (
                  <Choice active={mode === 'loose'} onClick={() => {
                    if (looseSpots.length === 1 && !spotOf(looseSpots[0]!)) pickLoc(looseSpots[0]!);   // the house itself: done
                    else { setMode('loose'); setQ(''); }
                  }}>Loose{looseSpots.length === 1 && !spotOf(looseSpots[0]!) ? ' (no box)' : ' on a shelf'}</Choice>
                )}
              </div>
            </div>
          )}
          {site && mode === 'loose' && (
            <div className="space-y-1.5">
              <p className="text-xs font-medium text-muted-foreground">3. Which shelf or spot?</p>
              <SearchInput autoFocus placeholder="Shelf name, like S2" value={q} onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && exactShelf) { e.preventDefault(); pickLoc(exactShelf); } }} />
              <div className="grid gap-2">
                {shelfHits.map((l) => <Choice key={l.id} onClick={() => pickLoc(l)}>{isShelf(l) ? l.code : spotLabel(l)}
                  <span className="block text-xs text-muted-foreground">{isShelf(l) ? 'Shelf · ' : ''}{l.units ? units(l.units) : 'empty'}</span></Choice>)}
              </div>
              {canMake && !exactShelf && (
                <Button type="button" variant="outline" className="h-11 w-full" disabled={making} onClick={() => make('shelf')}>
                  <Printer className="mr-1.5 h-4 w-4" />New shelf {needle} + print its label
                </Button>
              )}
              {needle && !shelfHits.length && !canMake && <p className="text-xs text-muted-foreground">No shelf named {q.trim()} in {site}.</p>}
            </div>
          )}
          {site && mode === 'box' && (
            <div className="space-y-1.5">
              <p className="text-xs font-medium text-muted-foreground">{allowLocations ? '3.' : '2.'} Which box? Type the name written on it</p>
              <SearchInput autoFocus placeholder="Box name, like A31" value={q} onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key !== 'Enter') return;
                  e.preventDefault();
                  if (exactBox) pickBox(exactBox);
                  else if (canMake && !making) void make('box');
                }} />
              <div className="grid gap-2">
                {shownBoxes.map((b) => (
                  <Choice key={b.id} onClick={() => pickBox(b)}>
                    {boxName(b)}
                    <span className="block text-xs text-muted-foreground">
                      {(() => { const l = placeById.get(b.location_id); return l ? placeText(l) : b.location_code; })()} · {b.units ? units(b.units) : 'empty'}
                    </span>
                  </Choice>
                ))}
              </div>
              {canMake && !exactBox && (
                <Button type="button" className="h-11 w-full" disabled={making} onClick={() => make('box')}>
                  <Printer className="mr-1.5 h-4 w-4" />New box {needle} + print its 4x6 label
                </Button>
              )}
              {!needle && !shownBoxes.length && <p className="text-xs text-muted-foreground">Type the name written on the box (A31).</p>}
              {needle && !shownBoxes.length && !canMake && <p className="text-xs text-muted-foreground">No box named {q.trim()}.</p>}
              {boxHits.length > shownBoxes.length && <p className="text-xs text-muted-foreground">{boxHits.length - shownBoxes.length} more: type to narrow it down.</p>}
            </div>
          )}
          {value && editing && <button type="button" className="text-sm underline" onClick={reset}>Keep {describe(value)}</button>}
        </div>
      )}
      {err && <p className="text-sm text-destructive">{err}</p>}
    </div>
  );
}

export function SearchInput(props: React.ComponentProps<typeof Input>) {
  return <Input type="search" autoCapitalize="none" autoCorrect="off" className="h-10" {...props} />;
}
