import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, ExternalLink, EyeOff, PackagePlus, RefreshCw } from 'lucide-react';
import { get, post } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Empty, PageHeader, Thumb, SectionTitle } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { receiveLine } from '@/lib/receive';
import { bucket } from '@/lib/arriving';
import { ago, money, packagesText, plural, when } from '@/lib/format';
import { cn } from '@/lib/utils';

/** `url`: the store's own product page (Amazon by ASIN, Walmart by item number), '' when the store gave no ID. */
interface Line { id: number; asin: string; title: string; qty: number; received: number; returned?: number; coming: number; unit_cost_cents: number; image_url: string | null; product_id: number | null; url?: string }
interface Order {
  id: number; order_id: string; source?: string; ordered_at: string | null; state: string; status_text: string;
  expected_from: string | null; expected_to: string | null; delivered_on: string | null;
  status_changed_at: string | null; last_seen_at: string | null; total_cents: number | null; hidden: number; lines: Line[];
  /** One card per package state: a split order (1 delivered, 1 on the way) is part 1 and 2 of 2. */
  key: string; part: number; parts: number;
  /** What each package's Amazon Track page said (extension 2.1+). */
  tracks?: Track[];
  /** Alex 11-16: a box label of this order was scanned here (in hand), newest scan. */
  label_scanned?: { at: string; by: string };
}
interface Track {
  pkg: number; carrier: string; tracking_id: string | null; note: string; headline: string; read_at: string;
  last_event: { date: string; time: string; text: string; where: string } | null; photo: string | null;
}

/** Carrier, tracking ID, latest scan and Amazon's drop-off photo for one package. Tap the photo to enlarge. */
function TrackRow({ t, many }: { t: Track; many: boolean }) {
  const [big, setBig] = useState(false);
  const ev = t.last_event;
  return (
    <div className="flex items-start gap-3 rounded-lg bg-muted/50 px-3 py-2 text-xs">
      {t.photo && (
        <button type="button" onClick={() => setBig(true)} className="shrink-0" aria-label="Show the delivery photo">
          <img src={t.photo} alt="Delivery photo" loading="lazy" className="h-16 w-12 rounded-md border object-cover" />
        </button>
      )}
      <div className="min-w-0 flex-1 space-y-0.5">
        <div>
          {many && <span className="text-muted-foreground">Package {t.pkg} · </span>}
          {t.carrier && <span className="font-medium">{t.carrier}</span>}
          {t.tracking_id && <> · <span className="select-all font-mono">{t.tracking_id}</span></>}
        </div>
        {t.note && <div className="font-medium text-foreground">{t.note}</div>}
        {!ev && t.headline && <div className="text-muted-foreground">{t.headline}</div>}
        {ev && <div className="text-muted-foreground">{ev.text}{ev.where ? ` · ${ev.where}` : ''}{ev.date ? ` · ${ev.date}${ev.time ? ` ${ev.time}` : ''}` : ''}</div>}
        {t.photo && <div className="text-muted-foreground">Amazon's delivery photo: tap it to see where the driver left it.</div>}
      </div>
      {t.photo && (
        <Dialog open={big} onOpenChange={setBig}>
          <DialogContent className="max-w-md">
            <DialogHeader><DialogTitle>Delivery photo{t.note ? `: ${t.note}` : ''}</DialogTitle></DialogHeader>
            <img src={t.photo} alt="Delivery photo" className="w-full rounded-md" />
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
interface Data { counts: Record<string, { packages: number; orders: number }>; orders: Order[]; last_sync: { at: string; device: string; orders: number; changed: number; note: string } | null; today: string }

const storeName = (url: string) => { try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return 'the store'; } };

/** An item title that opens the store's page when Boxloft has its link (Amazon, Walmart, or the order email's). */
function ItemTitle({ title, url }: { title: string; url?: string | null }) {
  if (!url) return <div className="line-clamp-2 text-sm">{title}</div>;
  return (
    <a href={url} target="_blank" rel="noreferrer" className="flex items-start gap-1 text-sm" aria-label={`${title}: open on ${storeName(url)}`}>
      <span className="line-clamp-2 underline decoration-muted-foreground/40 underline-offset-2">{title}</span>
      <ExternalLink className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    </a>
  );
}
const daysBetween =(a: string, b: string) => Math.round((Date.parse(b) - Date.parse(a)) / 86400000);
const fmtDay = (d: string) => new Date(d + 'T12:00:00').toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });


// Arriving today sits at the top of the page (Alex 11-13), then packages already here, the ones to scan in now.
const TODAY = { key: 'today', title: 'Arriving today', hint: '' };
const SECTIONS: { key: string; title: string; hint: string }[] = [
  { key: 'delivered', title: 'Delivered, not scanned in', hint: 'Amazon says delivered. Scan them in; 2+ days means look for it.' },
  { key: 'late', title: 'Late', hint: 'The expected date has passed and Amazon has not said delivered' },
  { key: 'week', title: 'This week', hint: '' },
  { key: 'later', title: 'Later', hint: '' },
  { key: 'nodate', title: 'Ordered, no date yet', hint: 'Oldest first. These move up the moment Amazon gives a date.' },
];

function OrderCard({ o, today, onChange }: { o: Order; today: string; onChange: () => void }) {
  const toast = useToast();
  const fresh = o.status_changed_at && Date.now() - Date.parse(o.status_changed_at) < 36 * 3600 * 1000 && o.state === 'shipped';
  const waited = o.ordered_at ? daysBetween(o.ordered_at, today) : null;
  const stale = o.state === 'delivered' && o.delivered_on && daysBetween(o.delivered_on, today) >= 2;
  const units = o.lines.reduce((a, l) => a + l.coming, 0);
  const delivered = o.state === 'delivered';
  const hide = async () => {
    await post(`/api/inbound/${o.id}/hide`, { hidden: !o.hidden, remember: false, seen: { hidden: o.hidden } });
    toast(o.hidden ? 'Back on the list' : 'Hidden from Arriving');
    onChange();
  };
  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold">{o.state === 'delivered' ? `Delivered ${o.delivered_on ? fmtDay(o.delivered_on) : ''}` : o.expected_to ? (o.expected_from && o.expected_from !== o.expected_to ? `${fmtDay(o.expected_from)} – ${fmtDay(o.expected_to)}` : fmtDay(o.expected_to)) : 'No date yet'}</span>
          {o.parts > 1 && <Badge variant="outline">{delivered ? 'Delivered part' : 'Still coming'} of a split order ({o.part} of {o.parts})</Badge>}
          {fresh && <Badge>Newly shipped</Badge>}
          {stale && <Badge variant="warning">Not scanned in after {daysBetween(o.delivered_on!, today)} days</Badge>}
          {o.label_scanned && <Badge variant="outline">Box label scanned {when(o.label_scanned.at)}{o.label_scanned.by ? ` by ${o.label_scanned.by}` : ''}</Badge>}
          <span className="text-muted-foreground">“{o.status_text}”</span>
          <span className="ml-auto text-xs text-muted-foreground">{o.source && o.source !== 'amazon' ? `${o.source} ` : ''}#{o.order_id} · ordered {o.ordered_at}{waited !== null && o.state === 'ordered' ? ` (${waited} days ago)` : ''}</span>
        </div>
        {o.tracks?.map((t) => <TrackRow key={t.pkg} t={t} many={(o.tracks?.length ?? 0) > 1} />)}
        {o.lines.map((l) => (
          <div key={l.id} className="flex items-center gap-3">
            <Thumb photoId={null} url={l.image_url} />
            <div className="min-w-0 flex-1">
              <ItemTitle title={l.title} url={l.url} />
              <div className="text-xs text-muted-foreground"><b className="text-foreground">{l.coming}</b> {delivered ? 'to scan in' : 'coming'}{o.parts > 1 && l.coming < l.qty ? ` of ${l.qty}` : ''}{l.received ? ` (${l.received} already in)` : ''}{l.returned ? ` (${l.returned} sent back)` : ''} · {money(l.unit_cost_cents)} each · <span className="font-mono">{l.asin}</span></div>
            </div>
            {l.coming > 0 && (
              <Button size="sm" variant="outline" onClick={() => receiveLine(l, toast)}>
                <PackagePlus className="mr-1 h-3.5 w-3.5" />Receive
              </Button>
            )}
          </div>
        ))}
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>{plural(units, 'unit')}{o.total_cents ? ` · order total ${money(o.total_cents)}` : ''}</span>
          <Button size="sm" variant="ghost" className="h-7" onClick={hide}><EyeOff className="mr-1 h-3.5 w-3.5" />{o.hidden ? 'Show again' : 'Hide'}</Button>
        </div>
      </CardContent>
    </Card>
  );
}

interface StoreLine { id: number; title: string; qty: number; unit_cost_cents: number; image_url: string | null; store_url?: string | null }
interface StoreOrder { id: number; store: string; order_id: string; ordered_at: string | null; total_cents: number; review: string; mail_id: string | null; lines: StoreLine[] }

/** Orders read from store emails wait here until a person checks them against the email. */
function StoreReview({ onChange }: { onChange: () => void }) {
  const toast = useToast();
  const q = useQuery({ queryKey: ['store-review'], queryFn: () => get<{ orders: StoreOrder[] }>('/api/store-orders/review'), refetchInterval: 300000 });
  const [prices, setPrices] = useState<Record<number, string>>({});
  const orders = q.data?.orders ?? [];
  if (!orders.length) return null;
  const done = () => { void q.refetch(); onChange(); };
  const approve = async (o: StoreOrder) => {
    const p: Record<number, number> = {};
    for (const l of o.lines) {
      const v = prices[l.id];
      if (v !== undefined && v.trim() !== '') {
        const n = Math.round(parseFloat(v.replace(/[$,]/g, '')) * 100);
        if (!Number.isFinite(n) || n < 0) { toast(`Price for "${l.title.slice(0, 30)}" is not a number`, { tone: 'error' }); return; }
        p[l.id] = n;
      }
    }
    await post(`/api/store-orders/${o.id}/approve`, { prices: p, seen: { review: o.review, hidden: 0 } });
    toast('Approved: now on Arriving');
    done();
  };
  const notStock = async (o: StoreOrder) => {
    await post(`/api/inbound/${o.id}/hide`, { hidden: true, remember: false, seen: { hidden: 0, review: o.review } });
    toast('Marked not inventory');
    done();
  };
  const skipStore = async (o: StoreOrder) => {
    const r = await post<{ hidden: number }>('/api/store-orders/skip', { store: o.store });
    toast(`${o.store} won't come in again (${r.hidden} hidden)`);
    done();
  };
  return (
    <section className="space-y-2">
      <div>
        <SectionTitle count={orders.length}>Store orders to check</SectionTitle>
        <p className="text-xs text-muted-foreground">Read from order emails paid with the business card. Compare with the email, fill any missing price, then Approve. Personal buys: Not inventory. A service or bill (not a store): Not a store we buy from.</p>
      </div>
      {orders.map((o) => (
        <Card key={o.id}>
          <CardContent className="space-y-3 p-4">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-semibold">{o.store}</span>
              <span className="text-muted-foreground">#{o.order_id} · ordered {o.ordered_at} · total {money(o.total_cents)}</span>
              {o.mail_id && <a className="ml-auto text-xs underline" target="_blank" rel="noreferrer" href={`https://mail.google.com/mail/u/0/#search/rfc822msgid%3A${encodeURIComponent(o.mail_id.replace(/[<>]/g, ''))}`}>Open the email</a>}
            </div>
            <div className="rounded-lg bg-warning/10 px-3 py-2 text-sm">Check: {o.review}</div>
            {o.lines.map((l) => (
              <div key={l.id} className="flex items-center gap-3 text-sm">
                <Thumb photoId={null} url={l.image_url} />
                <div className="min-w-0 flex-1"><ItemTitle title={l.title} url={l.store_url} /><div className="text-xs text-muted-foreground">qty {l.qty}</div></div>
                <Input className="h-8 w-28" inputMode="decimal" placeholder={l.unit_cost_cents ? money(l.unit_cost_cents) : 'price each'}
                  value={prices[l.id] ?? ''} onChange={(e) => setPrices({ ...prices, [l.id]: e.target.value })} aria-label="Cost each" />
              </div>
            ))}
            {!o.lines.length && <div className="text-xs text-muted-foreground">No items could be read from this email. Open it and add the purchase by hand, or mark it not inventory.</div>}
            <div className="flex justify-end gap-2">
              <Button size="sm" variant="ghost" className="mr-auto" onClick={() => void skipStore(o)} title={`Skip every email from ${o.store} and hide its orders here`}>Not a store we buy from</Button>
              <Button size="sm" variant="ghost" onClick={() => void notStock(o)}><EyeOff className="mr-1 h-3.5 w-3.5" />Not inventory</Button>
              <Button size="sm" disabled={!o.lines.length} onClick={() => void approve(o)}><Check className="mr-1 h-3.5 w-3.5" />Approve</Button>
            </div>
          </CardContent>
        </Card>
      ))}
    </section>
  );
}

export function Arriving() {
  const qc = useQueryClient();
  const [showHidden, setShowHidden] = useState(false);
  const q = useQuery({ queryKey: ['arriving', showHidden], queryFn: () => get<Data>(`/api/inbound/arriving${showHidden ? '?hidden=1' : ''}`), refetchInterval: 120000 });
  const d = q.data;
  const syncOld = d?.last_sync ? Date.now() - Date.parse(d.last_sync.at) > 2 * 3600 * 1000 : true;
  const groups = new Map<string, Order[]>();
  for (const o of d?.orders ?? []) {
    const b = (o as Order & { bucket?: string }).bucket ?? bucket(o, d!.today);
    groups.set(b, [...(groups.get(b) ?? []), o]);
  }
  groups.get('nodate')?.sort((a, b) => (a.ordered_at ?? '').localeCompare(b.ordered_at ?? ''));
  const section = (s: typeof TODAY) => groups.get(s.key)?.length ? (
    <section key={s.key} className="space-y-2">
      <SectionTitle count={packagesText(d!.counts?.[s.key] ?? { packages: groups.get(s.key)!.length, orders: groups.get(s.key)!.length })} hint={s.hint}>{s.title}</SectionTitle>
      {groups.get(s.key)!.map((o) => <OrderCard key={o.key} o={o} today={d!.today} onChange={() => qc.invalidateQueries({ queryKey: ['arriving'] })} />)}
    </section>
  ) : null;
  return (
    <div className="max-w-5xl space-y-4">
      <PageHeader title="Arriving" subtitle="Orders on their way to us." about="Amazon orders sync from Alex's Chrome every 30 minutes. Orders from other stores are read from order emails."
        actions={<Button variant="outline" onClick={() => qc.invalidateQueries({ queryKey: ['arriving'] })}><RefreshCw className="mr-1.5 h-4 w-4" />Refresh</Button>} />
      {section(TODAY)}
      <div className={cn('rounded-lg px-4 py-2.5 text-sm', syncOld ? 'bg-warning/10' : 'border')}>
        {d?.last_sync ? <>Last Amazon sync {when(d.last_sync.at)} ({ago(d.last_sync.at)}) from {d.last_sync.device || 'Chrome'}: {d.last_sync.orders} orders read, {d.last_sync.changed} changed.{syncOld && ' Over 2 hours old: check that Chrome is open on the house computer and signed in to Amazon.'}</>
          : 'No sync yet. Install the Boxloft extension and add its token (Settings → Amazon sync).'}
      </div>
      <StoreReview onChange={() => qc.invalidateQueries({ queryKey: ['arriving'] })} />
      <div className="flex items-center gap-2"><Switch id="hid" checked={showHidden} onCheckedChange={setShowHidden} /><Label htmlFor="hid">Show hidden orders</Label></div>
      {d && !d.orders.length && <Empty>Nothing on the way.</Empty>}
      {SECTIONS.map(section)}
    </div>
  );
}
