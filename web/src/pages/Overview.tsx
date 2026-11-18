import { useQuery } from '@tanstack/react-query';
import { CheckCircle2, AlertTriangle, PackagePlus, MoveRight, Truck, CalendarClock, ListChecks, ChevronRight, Boxes, Box as BoxIcon, Printer } from 'lucide-react';
import { get } from '@/api/client';
import type { Summary } from '@/api/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { PageHeader, Stat } from '@/components/app/common';
import { ActivityList } from '@/pages/Activity';
import { Link, navigate } from '@/lib/router';
import { ago, money, packagesText, plural, when } from '@/lib/format';
import { useAuth } from '@/context/AuthContext';
import { clearCurrentBox, curPath, isFresh, useCurrentBox } from '@/lib/currentBox';
import type { Queue } from '@/components/app/PrintQueue';
import { APP } from '@/lib/appConfig';

/** The jobs waiting, each a link to the page that does it. Shown above the numbers so the next step is the first thing read. */
function DoNext({ s, staff }: { s: Summary; staff?: boolean }) {
  const empty = s.low_ship_boxes.filter((b) => b.stock <= 0).length;
  const rows = [
    s.arriving.delivered_not_in > 0 && { to: '/arriving', text: `Scan in delivered: ${packagesText(s.arriving.counts.delivered)}` },
    s.needs_attention.loose_on_dock > 0 && { to: '/move', text: `Put away ${plural(s.needs_attention.loose_on_dock, 'unit')} loose on the receiving dock` },
    s.boxes_never_counted > 0 && { to: '/count', text: `Count ${plural(s.boxes_never_counted, 'box', 'boxes')} never counted` },
    s.needs_attention.no_photo > 0 && { to: '/inventory', text: `Photograph ${plural(s.needs_attention.no_photo, 'SKU')} with no photo` },
    !staff && s.low_ship_boxes.length > 0 && { to: '/ship-boxes', text: empty === s.low_ship_boxes.length
      ? `Count ship boxes: ${plural(empty, 'size')} show 0 in stock`
      : `Ship boxes low: ${s.low_ship_boxes.slice(0, 3).map((b) => `${b.name} (${b.stock})`).join(', ')}${s.low_ship_boxes.length > 3 ? ` +${s.low_ship_boxes.length - 3} more` : ''}` },
  ].filter(Boolean) as { to: string; text: string }[];
  if (!rows.length) return null;
  return (
    <Card>
      {/* Alex 11-16: the title sat 20 px above its list (CardHeader's empty description row adds an 8 px gap), as far
          as the rows sit from each other, and each row's divider curved up at its rounded corners. Now: title 12 px
          above the first row, square rows so dividers run straight, hover shading only where there is a mouse
          (on an iPhone a tapped row kept the grey band). */}
      <CardHeader className="gap-0 pb-0"><CardTitle className="text-base">Do next</CardTitle></CardHeader>
      <CardContent className="-mt-1.5 divide-y px-2 text-sm">
        {rows.map((r) => (
          <Link key={r.to} to={r.to} className="flex items-center justify-between gap-2 px-2 py-2.5 active:bg-muted [@media(hover:hover)]:hover:bg-muted">
            <span className="min-w-0">{r.text}</span><ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
          </Link>
        ))}
      </CardContent>
    </Card>
  );
}

/* Staff Home (flow audit 11-12, finding 10). Sam's Home was 3.7 screens with cost totals, the ledger check and owner
 * value: Alex's facts. Hers: the box being filled, her last 5 saves with Undo, labels waiting, and the jobs. */
function StaffHome({ name }: { name: string }) {
  const q = useQuery({ queryKey: ['summary'], queryFn: () => get<Summary>('/api/summary'), refetchInterval: 60000 });
  const pq = useQuery({ queryKey: ['print-jobs'], queryFn: () => get<Queue>('/api/print-jobs'), refetchInterval: 60000 });
  const cur = useCurrentBox();
  const s = q.data;
  const fresh = isFresh(cur);
  return (
    <div className="max-w-xl space-y-4">
      <PageHeader title={`Hi, ${name}`} />
      <Card><CardContent className="space-y-3 p-4">
        {cur && fresh ? (
          <>
            <div className="text-sm text-muted-foreground">Filling</div>
            <div className="text-3xl font-bold">{cur.name}</div>
            <div className="flex gap-2">
              <Button className="h-12 flex-1 text-base" onClick={() => navigate(curPath(cur))}><BoxIcon className="mr-1.5 h-4 w-4" />Keep filling {cur.name}</Button>
              <Button variant="outline" className="h-12" onClick={clearCurrentBox}>Done</Button>
            </div>
            <p className="text-sm text-muted-foreground">Scan an item and it goes in. Scan another box label to switch.</p>
          </>
        ) : (
          <>
            <div className="text-base font-medium">Scan a box label, then scan items into it. No box? Put away has the places.</div>
            <Button className="h-12 w-full text-base" onClick={() => navigate('/put')}><PackagePlus className="mr-1.5 h-4 w-4" />Put away</Button>
          </>
        )}
      </CardContent></Card>
      <div className="grid grid-cols-2 gap-3">
        {[
          { to: '/audit', label: 'Stock audit', icon: ListChecks },
          { to: '/arriving', label: s?.arriving.delivered_not_in ? `${s.arriving.delivered_not_in} delivered` : 'Arriving', icon: CalendarClock },
          { to: '/boxes', label: 'Boxes', icon: Boxes },
          { to: '/orders', label: s?.open_orders ? `${s.open_orders} to ship` : 'Orders', icon: Truck },
        ].map((a) => (
          <Button key={a.to} variant="outline" className="h-14 justify-start gap-3 text-base" onClick={() => navigate(a.to)}>
            <a.icon className="h-5 w-5 text-primary" /> {a.label}
          </Button>
        ))}
      </div>
      {(pq.data?.labels_waiting ?? 0) > 0 && (
        <Link to="/print-queue" className="flex items-center gap-2 rounded-lg border px-3 py-2.5 text-sm hover:bg-muted">
          <Printer className="h-4 w-4" /><span className="flex-1">{plural(pq.data!.labels_waiting, 'label')} waiting to print</span><ChevronRight className="h-4 w-4 text-muted-foreground" />
        </Link>
      )}
      {s && <DoNext s={s} staff />}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardTitle className="text-base">Your last saves</CardTitle>
          <Link to="/activity" className="text-sm text-primary hover:underline">All</Link>
        </CardHeader>
        <CardContent>{s ? <ActivityList actions={s.mine ?? []} compact /> : <Skeleton className="h-24" />}</CardContent>
      </Card>
    </div>
  );
}

export function Overview() {
  const { user } = useAuth();
  if (user && !user.is_admin) return <StaffHome name={user.display_name} />;
  return <AdminHome />;
}

function AdminHome() {
  const { user } = useAuth();
  const q = useQuery({ queryKey: ['summary'], queryFn: () => get<Summary>('/api/summary'), refetchInterval: 60000 });
  const s = q.data;
  const hour = Number(new Date().toLocaleString('en-US', { timeZone: APP.tz, hour: 'numeric', hour12: false }));
  const greet = hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';

  return (
    <div className="max-w-5xl space-y-4 sm:space-y-6">
      <PageHeader title={`${greet}, ${user?.display_name}`} subtitle={APP.place ? `${APP.name}, ${APP.place}` : APP.name} />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Button variant="default" className="col-span-2 h-16 justify-start gap-3 text-base md:col-span-4" onClick={() => navigate('/audit')}>
          <ListChecks className="h-5 w-5" /> Stock audit: scan items into boxes
        </Button>
        {[
          { to: '/orders', label: s?.open_orders ? `Ship ${s.open_orders} order${s.open_orders === 1 ? '' : 's'}` : 'Orders', icon: Truck },
          { to: '/receive', label: 'Receive', icon: PackagePlus },
          { to: '/move', label: 'Move', icon: MoveRight },
          { to: '/arriving', label: s?.arriving.today ? `${s.arriving.today} arriving today` : 'Arriving', icon: CalendarClock },
        ].map((a) => (
          <Button key={a.to} variant="outline" className="h-16 justify-start gap-3 text-base" onClick={() => navigate(a.to)}>
            <a.icon className="h-5 w-5 text-primary" /> {a.label}
          </Button>
        ))}
      </div>

      {!s ? (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-24" />)}</div>
      ) : (
        <>
          <DoNext s={s} />
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <Stat label="Units on hand" value={s.units.toLocaleString()} hint={plural(s.skus_in_stock, 'SKU')} />
            <Stat label="Inventory at cost" value={money(s.value_cents)} hint={s.needs_attention.missing_cost ? `${s.needs_attention.missing_cost} SKUs have no cost` : 'All stock has a cost'} tone={s.needs_attention.missing_cost ? 'warn' : undefined} />
            <Stat label="Boxes in use" value={(s.boxes - s.empty_boxes).toLocaleString()} hint={`${s.empty_boxes} empty · ${s.locations} shelves`} />
            <Stat label="Amazon orders on the way" value={s.arriving.open} hint={s.arriving.delivered_not_in ? `Delivered, not scanned in: ${packagesText(s.arriving.counts.delivered)}` : s.arriving.last_sync ? 'all delivered ones scanned in' : 'extension not synced yet'} tone={s.arriving.delivered_not_in ? 'warn' : undefined} />
          </div>

          <div className="grid gap-4 lg:grid-cols-3 sm:gap-6 [&>*]:min-w-0">
            <Card className="lg:col-span-2">
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-base">Recent activity</CardTitle>
                <Link to="/activity" className="text-sm text-primary hover:underline">All activity</Link>
              </CardHeader>
              <CardContent><ActivityList actions={s.recent} compact /></CardContent>
            </Card>
            <div className="space-y-4">
              <Card>
                <CardHeader className="pb-2"><CardTitle className="text-base">Ledger check</CardTitle></CardHeader>
                <CardContent className="text-sm">
                  {s.integrity ? (
                    <div className="flex gap-3">
                      {s.integrity.ok ? <CheckCircle2 className="h-5 w-5 shrink-0 text-primary" /> : <AlertTriangle className="h-5 w-5 shrink-0 text-destructive" />}
                      <div>
                        <div className="font-medium">{s.integrity.ok ? 'Counts match the ledger' : `${s.integrity.problem_count} problems found`}</div>
                        <div className="text-muted-foreground">
                          Rebuilt every count from {plural(s.integrity.moves_checked, 'move')} and compared. Last run {when(s.integrity.at)} ({ago(s.integrity.at)}). Runs nightly at 3:15 AM ET.
                        </div>
                      </div>
                    </div>
                  ) : 'Not run yet'}
                  <div className="mt-3 flex gap-3 border-t pt-3">
                    {s.unaccounted.lines === 0 ? <CheckCircle2 className="h-5 w-5 shrink-0 text-primary" /> : <AlertTriangle className="h-5 w-5 shrink-0 text-destructive" />}
                    <div className="min-w-0">
                      <div className="font-medium">{s.unaccounted.lines === 0 ? 'Every purchase has a place'
                        : `${plural(s.unaccounted.units, 'unit')} bought but on no list (${money(s.unaccounted.cost_cents)})`}</div>
                      <div className="text-muted-foreground">Each unit still expected is in Store orders to check, on Arriving or in the Stock audit.</div>
                      {s.unaccounted.items.slice(0, 5).map((i) => (
                        <div key={i.id} className="truncate text-xs text-muted-foreground">{i.source} #{i.order_id} · {i.state} · {i.units} × {i.title}</div>
                      ))}
                    </div>
                  </div>
                  {s.refused && (
                    <div className="mt-3 flex gap-3 border-t pt-3">
                      {s.refused.count === 0 ? <CheckCircle2 className="h-5 w-5 shrink-0 text-primary" /> : <AlertTriangle className="h-5 w-5 shrink-0 text-warning" />}
                      <div className="min-w-0">
                        <div className="font-medium">{s.refused.count === 0 ? 'No saves refused today'
                          : `${plural(s.refused.count, 'save')} refused today` + (s.refused.conflicts ? ` (${s.refused.conflicts} were someone else saving first)` : '')}</div>
                        <div className="text-muted-foreground">An error message someone got on a save. One that repeats, or that makes no sense for what they did, is a bug.</div>
                        {s.refused.recent.map((r, i) => (
                          <div key={i} className="truncate text-xs text-muted-foreground" title={`${r.method} ${r.path}`}>{when(r.at)} · {r.user} · {r.message}</div>
                        ))}
                      </div>
                    </div>
                  )}
                  {s.ebay && (
                    <div className="mt-3 flex gap-3 border-t pt-3">
                      {s.ebay.unmatched + s.ebay.shipped_open === 0 ? <CheckCircle2 className="h-5 w-5 shrink-0 text-primary" /> : <AlertTriangle className="h-5 w-5 shrink-0 text-warning" />}
                      <div className="min-w-0">
                        <div className="font-medium">{s.ebay.unmatched + s.ebay.shipped_open === 0 ? 'Every eBay listing and order has its item'
                          : [s.ebay.unmatched ? `${plural(s.ebay.unmatched, 'eBay listing')} to match` : '',
                            s.ebay.shipped_open ? `${plural(s.ebay.shipped_open, 'eBay order')} shipped on eBay, open here` : ''].filter(Boolean).join(' · ')}</div>
                        <div className="text-muted-foreground">
                          {s.ebay.unmatched ? <Link to="/listings" className="underline">Listings, eBay</Link> : null}
                          {s.ebay.unmatched && s.ebay.shipped_open ? ' · ' : ''}
                          {s.ebay.shipped_open ? <Link to="/orders" className="underline">Orders</Link> : null}
                          {s.ebay.unmatched + s.ebay.shipped_open === 0 ? 'Each listing has an item, and each order eBay shipped is closed here.' : ''}
                        </div>
                      </div>
                    </div>
                  )}
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-2"><CardTitle className="text-base">By owner</CardTitle></CardHeader>
                <CardContent className="space-y-2 text-sm">
                  {s.by_owner.length ? s.by_owner.map((o) => (
                    <div key={o.owner} className="flex justify-between gap-2">
                      <span className="font-medium">{o.owner}</span>
                      <span className="tabular-nums text-muted-foreground">{o.units.toLocaleString()} units · {money(o.value_cents)}</span>
                    </div>
                  )) : <span className="text-muted-foreground">No stock yet. Start with Receive.</span>}
                </CardContent>
              </Card>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
