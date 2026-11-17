import { PutPage } from '@/pages/Put';
import { LabelPage } from '@/pages/LabelPage';
import { useEffect, useRef } from 'react';
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { get } from '@/api/client';
import { ThemeProvider } from '@/context/ThemeContext';
import { AuthProvider, useAuth } from '@/context/AuthContext';
import { TooltipProvider } from '@/components/ui/tooltip';
import { Skeleton } from '@/components/ui/skeleton';
import { ToastProvider } from '@/components/app/toast';
import { LoginPage, SetupPage } from '@/components/app/LoginPage';
import { Shell } from '@/components/app/Shell';
import { Empty } from '@/components/app/common';
import { ScanProvider } from '@/lib/scan';
import { match, useLocation } from '@/lib/router';
import { Overview } from '@/pages/Overview';
import { Inventory } from '@/pages/Inventory';
import { NewProduct, ProductPage } from '@/pages/Product';
import { Receive } from '@/pages/Receive';
import { Move } from '@/pages/Move';
import { Count } from '@/pages/Count';
import { BoxPage, Boxes } from '@/pages/Boxes';
import { LocationPage, Locations } from '@/pages/Locations';
import { UnitPage } from '@/pages/Unit';
import { ActivityPage } from '@/pages/Activity';
import { Settings } from '@/pages/Settings';
import { Purchases } from '@/pages/Purchases';
import { Orders } from '@/pages/Orders';
import { ShipBoxes } from '@/pages/ShipBoxes';
import { Arriving } from '@/pages/Arriving';
import { Audit } from '@/pages/Audit';
import { Returns } from '@/pages/Returns';
import { Listings } from '@/pages/Listings';
import { PrintQueuePage } from '@/pages/PrintQueue';
import { QueueToaster } from '@/components/app/PrintQueue';

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true, staleTime: 5000 } } });

function Page() {
  const { path } = useLocation();
  const id = (p: string) => Number(match(p, path)!.id);
  if (path === '/') return <Overview />;
  if (path === '/inventory') return <Inventory />;
  if (path === '/inventory/new') return <NewProduct />;
  if (match('/inventory/:id', path)) return <ProductPage key={path} id={id('/inventory/:id')} />;
  if (path === '/receive') return <Receive />;
  if (path === '/put') return <PutPage />;
  if (path === '/label') return <LabelPage />;
  if (path === '/move') return <Move />;
  if (path === '/count') return <Count />;
  if (path === '/boxes') return <Boxes />;
  if (match('/boxes/:id', path)) return <BoxPage key={path} id={id('/boxes/:id')} />;
  if (path === '/locations') return <Locations />;
  if (match('/locations/:id', path)) return <LocationPage key={path} id={id('/locations/:id')} />;
  if (match('/units/:id', path)) return <UnitPage key={path} id={id('/units/:id')} />;
  if (path === '/activity') return <ActivityPage />;
  if (path === '/purchases') return <Purchases />;
  if (path === '/orders') return <Orders />;
  if (path === '/ship-boxes') return <ShipBoxes />;
  if (path === '/arriving') return <Arriving />;
  if (path === '/audit') return <Audit />;
  if (path === '/returns') return <Returns />;
  if (path === '/listings') return <Listings />;
  if (path === '/settings') return <Settings />;
  if (path === '/print-queue') return <PrintQueuePage />;
  return <Empty>Page not found</Empty>;
}

/** Two people at once (Alex 11-11): when the other person saves something, this screen reloads its data within
 *  10 seconds, and right away after a save is refused because someone else saved first (409). Checks a tiny
 *  change token only while the tab is visible. */
// The app's own main script (index-<hash>.js). The server says which one it serves now; a phone still on an older
// one (a home-screen app resumes without reloading) reloads at a safe moment: when it comes back on screen, or on
// its next page change. Never while someone is on a page, so nothing typed is lost. Alex 11-12: an old Stock
// audit kept sending one request per card after the fix and slowed every screen.
const myBuild = (document.querySelector('script[src*="/assets/index-"]')?.getAttribute('src') ?? '').split('/').pop() ?? '';
// At most one reload per new build, so a cache that keeps serving the old app can't loop the page.
const reloadFor = (build: string) => {
  try {
    if (sessionStorage.getItem('boxloft-reloaded-for') === build) return;
    sessionStorage.setItem('boxloft-reloaded-for', build);
  } catch { /* storage blocked: still reload once */ }
  window.location.reload();
};

function LiveSync({ me }: { me: number }) {
  const qc = useQueryClient();
  const last = useRef('');
  const stale = useRef('');
  const { path } = useLocation();
  const first = useRef(path);
  useEffect(() => {
    if (stale.current && path !== first.current) reloadFor(stale.current);
    first.current = path;
  }, [path]);
  useEffect(() => {
    const reload = () => void qc.invalidateQueries();
    const check = async (back = false) => {
      if (document.visibilityState !== 'visible') return;
      try {
        const c = await get<{ token: string; by: number | null; build?: string }>('/api/changes');
        if (c.build && myBuild && c.build !== myBuild) {
          stale.current = c.build;
          if (back) { reloadFor(c.build); return; }
        }
        if (last.current && c.token !== last.current && c.by !== me) reload();
        last.current = c.token;
      } catch { /* offline or signed out: the next check tries again */ }
    };
    const onShow = () => { if (document.visibilityState === 'visible') void check(true); };
    void check(true);
    const t = window.setInterval(() => void check(), 10000);
    window.addEventListener('boxloft:changed', reload);
    document.addEventListener('visibilitychange', onShow);
    return () => {
      window.clearInterval(t);
      window.removeEventListener('boxloft:changed', reload);
      document.removeEventListener('visibilitychange', onShow);
    };
  }, [qc, me]);
  return null;
}

function Gate() {
  const { user, loading } = useAuth();
  const { path } = useLocation();
  if (path === '/setup') return <SetupPage />;
  if (loading) {
    return <div className="flex min-h-screen items-center justify-center"><Skeleton className="h-10 w-40" /></div>;
  }
  if (!user) return <LoginPage />;
  return (
    <ScanProvider>
      <LiveSync me={user.id} />
      <QueueToaster />
      <Shell><Page /></Shell>
    </ScanProvider>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <ToastProvider>
            <AuthProvider><Gate /></AuthProvider>
          </ToastProvider>
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
