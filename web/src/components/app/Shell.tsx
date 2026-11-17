import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { startTapProbe } from '@/lib/tapProbe';
import {
  Activity, Boxes, Camera, ClipboardCheck, Home, LogOut, Moon, MoveRight, Package, PackagePlus, Settings, Sun, Warehouse, ScanLine,
  ReceiptText, Truck, Box as BoxIcon, CalendarClock, Menu, ListChecks, Undo2, Store, Printer,
} from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarHeader, SidebarInset, SidebarMenu,
  SidebarMenuButton, SidebarMenuItem, SidebarProvider, SidebarTrigger, useSidebar,
} from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useAuth } from '@/context/AuthContext';
import { useTheme } from '@/hooks/useTheme';
import { Link, navigate, useLocation } from '@/lib/router';
import { useScan, useScanBus } from '@/lib/scan';
import { CameraScanner } from '@/lib/camera';
import { clearCurrentBox, isFresh, readCurrentBox, useCurrentBox } from '@/lib/currentBox';
import { get } from '@/api/client';
import type { ScanResult } from '@/api/types';
import { useToast } from '@/components/app/toast';
import { PrintQueueBar } from '@/components/app/PrintQueue';
import { BoxConfirm } from '@/components/app/BoxConfirm';
import { cn } from '@/lib/utils';
import { APP } from '@/lib/appConfig';

// The menu in groups (flow audit 11-12: a flat list of 17 put Stock audit 7th and Boxes 12th).
const GROUPS = [
  { name: '', items: [{ to: '/', label: 'Home', icon: Home }] },
  { name: 'Floor', items: [
    { to: '/put', label: 'Put away', icon: PackagePlus },
    { to: '/boxes', label: 'Boxes', icon: Boxes },
    { to: '/audit', label: 'Stock audit', icon: ListChecks },
    { to: '/count', label: 'Count', icon: ClipboardCheck },
    { to: '/move', label: 'Move', icon: MoveRight },
    { to: '/receive', label: 'Receive form', icon: ReceiptText },
  ] },
  { name: 'Buying', items: [
    { to: '/arriving', label: 'Arriving', icon: CalendarClock },
    { to: '/purchases', label: 'Purchases', icon: ReceiptText },
    { to: '/returns', label: 'Returns', icon: Undo2 },
  ] },
  { name: 'Selling', items: [
    { to: '/orders', label: 'Orders', icon: Truck },
    { to: '/listings', label: 'Listings', icon: Store },
    { to: '/ship-boxes', label: 'Ship boxes', icon: BoxIcon },
  ] },
  { name: 'Records', items: [
    { to: '/inventory', label: 'Inventory', icon: Package },
    { to: '/locations', label: 'Locations', icon: Warehouse },
    { to: '/print-queue', label: 'Print queue', icon: Printer },
    { to: '/activity', label: 'Activity', icon: Activity },
    { to: '/settings', label: 'Settings', icon: Settings },
  ] },
];
const NAV = GROUPS.flatMap((g) => g.items);

const isActive = (path: string, to: string) => (to === '/' ? path === '/' : path === to || path.startsWith(to + '/'));

function Nav() {
  const { path } = useLocation();
  const { setOpenMobile } = useSidebar();
  return (
    <>{GROUPS.map((g) => (
    <SidebarMenu key={g.name} className={g.name ? 'mt-2' : ''}>
      {g.name && <div className="px-2 pb-0.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground group-data-[collapsible=icon]:hidden">{g.name}</div>}
      {g.items.map((n) => (
        <SidebarMenuItem key={n.to}>
          <SidebarMenuButton asChild isActive={isActive(path, n.to)} className="h-10">
            <Link to={n.to} onClick={() => setOpenMobile(false)}>
              <n.icon className="h-4 w-4" />
              <span>{n.label}</span>
            </Link>
          </SidebarMenuButton>
        </SidebarMenuItem>
      ))}
    </SidebarMenu>
    ))}</>
  );
}

/** Whatever page is open, scanning a label you are not using opens that thing. */
function useOpenOnScan() {
  const toast = useToast();
  const { feedback } = useScanBus();
  useScan(async (code) => {
    try {
      const r = await get<ScanResult>(`/api/scan?code=${encodeURIComponent(code)}`);
      if (r.kind === 'box') navigate(`/boxes/${r.box.id}`);
      else if (r.kind === 'location') navigate(`/locations/${r.location.id}`);
      else if (r.kind === 'unit') navigate(`/units/${r.unit.id}`);
      else if (r.kind === 'label') {   // a carrier label: what is inside, in the box being filled if there is one
        feedback(true);
        const box = readCurrentBox();
        navigate(isFresh(box) ? `/boxes/${box!.box_id}?put=${encodeURIComponent(code)}` : `/label?code=${encodeURIComponent(code)}`);
      }
      else if ((r.kind === 'product' || (r.kind === 'unknown' && r.value)) && isFresh(readCurrentBox())) {
        // Flow audit 11-12 (redesigns 1 and 2): a box is being filled, so the item goes into it (the box page saves
        // it, or shows the new item card for a code nothing knows).
        if (r.kind === 'unknown' && !r.lines?.length) feedback('next', 'New item: is this it?', true);
        navigate(`/boxes/${readCurrentBox()!.box_id}?put=${encodeURIComponent(code)}`);
        if (r.kind === 'unknown' && !r.lines?.length) return;
      } else if (r.kind === 'product' && r.products.length > 1) navigate(`/inventory?q=${encodeURIComponent(r.value)}`);
      else if (r.kind === 'product' || (r.kind === 'unknown' && r.value)) {
        // No box yet: the item (or the new item card), and the box to put it in as one big button.
        if (r.kind === 'unknown' && !r.lines?.length) feedback('next', 'New item: is this it?', true);   // closes the camera
        navigate(`/put?code=${encodeURIComponent(code)}`);
        if (r.kind === 'unknown' && !r.lines?.length) return;
      } else {
        feedback(false);
        toast(r.message, { tone: 'error' });
        return;
      }
      feedback(true);
    } catch (e) {
      feedback(false);
      toast(e instanceof Error ? e.message : 'Scan failed', { tone: 'error' });
    }
  }, true, true);
}

function ManualScan() {
  const { emit, last } = useScanBus();
  const [v, setV] = useState('');
  const submit = (e: FormEvent) => {
    e.preventDefault();
    emit(v);
    setV('');
  };
  return (
    <form onSubmit={submit} className="relative hidden max-w-sm flex-1 sm:block">
      <ScanLine className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
      <Input
        value={v}
        onChange={(e) => setV(e.target.value)}
        placeholder={last ? `Last scan: ${last.code}` : 'Scan or type a code, then Enter'}
        className={cn('h-9 pl-8', last?.ok === false && 'scan-bad border-destructive', last?.ok === 'next' && 'scan-next border-warning')}
        autoCapitalize="none"
        autoCorrect="off"
      />
    </form>
  );
}

function BottomBar({ path }: { path: string }) {
  const { setOpenMobile } = useSidebar();
  const { user } = useAuth();
  // Tabs by use and by person (flow audit 11-12, finding 10): Alex ships and buys; Sam puts away and audits.
  const tabs = user?.is_admin ? ['/orders', '/arriving', '/put', '/boxes'] : ['/arriving', '/put', '/audit', '/boxes'];
  const items = tabs.map((to) => NAV.find((n) => n.to === to)!);
  return (
    <nav className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur sm:hidden">
      {items.map((n) => (
        <Link key={n.to} to={n.to} className={cn('flex flex-col items-center gap-0.5 py-2.5 text-[11px] font-medium', isActive(path, n.to) ? 'text-primary' : 'text-muted-foreground')}>
          <n.icon className="h-5 w-5" />
          {n.label}
        </Link>
      ))}
      <button onClick={() => setOpenMobile(true)} className="flex flex-col items-center gap-0.5 py-2.5 text-[11px] font-medium text-muted-foreground">
        <Menu className="h-5 w-5" />More
      </button>
    </nav>
  );
}

/** The box being filled, on every page (flow audit 11-12): an item scan goes into it. Tap to open it, x to stop. */
function CurrentBoxChip() {
  const cur = useCurrentBox();
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 60_000); return () => clearInterval(t); }, []);
  if (!cur || !isFresh(cur)) return null;
  return (
    <div className="flex h-11 items-center rounded-md border border-primary/60 bg-primary/10 text-sm">
      <Link to={`/boxes/${cur.box_id}`} className="flex h-full items-center px-2.5 font-semibold">Into {cur.name}</Link>
      <button type="button" aria-label={`Stop filling ${cur.name}`} className="flex h-full w-9 items-center justify-center border-l border-primary/40 text-muted-foreground" onClick={clearCurrentBox}>×</button>
    </div>
  );
}

/** Phone without HTTPS (a plain http:// address): the camera can't run, so type the code instead. */
function TypeCode({ onClose }: { onClose: () => void }) {
  const { emit } = useScanBus();
  const [v, setV] = useState('');
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-24" onClick={onClose}>
      <form className="w-full max-w-sm space-y-3 rounded-xl bg-card p-4 shadow-xl" onClick={(e) => e.stopPropagation()}
        onSubmit={(e: FormEvent) => { e.preventDefault(); if (v.trim()) { emit(v); onClose(); } }}>
        <div className="font-semibold">Type a code</div>
        <p className="text-xs text-muted-foreground">The camera scanner needs the HTTPS address. Here, type the box, shelf or unit code, or a UPC. A Bluetooth scanner works too.</p>
        <Input autoFocus value={v} onChange={(e) => setV(e.target.value)} placeholder="SB00000018 or 0123456789012" className="h-12 text-base" autoCapitalize="characters" autoCorrect="off" />
        <div className="flex gap-2"><Button type="submit" className="h-11 flex-1">Go</Button><Button type="button" variant="outline" className="h-11" onClick={onClose}>Cancel</Button></div>
      </form>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const [camera, setCamera] = useState(false);
  // A page's own camera button (box page Add card) opens the same scanner as the header's Scan.
  useEffect(() => {
    const open = () => (window.isSecureContext ? setCamera(true) : setTyping(true));
    window.addEventListener('boxloft:camera', open);
    return () => window.removeEventListener('boxloft:camera', open);
  }, []);
  const [typing, setTyping] = useState(false);
  const { path } = useLocation();
  const qc = useQueryClient();
  useOpenOnScan();
  useEffect(startTapProbe, []);

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon">
        <SidebarHeader>
          <Link to="/" className="flex items-center gap-2 px-2 py-1.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground"><Package className="h-4.5 w-4.5" /></div>
            <div className="leading-tight group-data-[collapsible=icon]:hidden">
              <div className="font-bold">{APP.name}</div>
              {APP.place && <div className="text-xs text-muted-foreground">{APP.place}</div>}
            </div>
          </Link>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupContent><Nav /></SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton onClick={() => { qc.clear(); void logout(); }} className="h-10">
                <LogOut className="h-4 w-4" />
                <span>Sign out {user?.display_name}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarFooter>
      </Sidebar>
      <SidebarInset>
        <header className="app-header sticky top-0 z-30 flex h-14 items-center gap-2 border-b bg-background/90 px-3 backdrop-blur sm:px-4">
          {/* Alex 11-12: in the home-screen app this button stopped working. 44 px is Apple's smallest tap target;
              the old 28 px one sat in the strip iOS covers at the top (globals.css, .app-header). */}
          <SidebarTrigger className="-ml-2 size-11 [&_svg]:size-5 sm:ml-0 sm:size-9 sm:[&_svg]:size-4" />
          <ManualScan />
          <div className="flex-1 sm:hidden" />
          <CurrentBoxChip />
          <div className="ml-auto flex items-center gap-1">
            <Button size="sm" onClick={() => (window.isSecureContext ? setCamera(true) : setTyping(true))} className="h-9 px-4 font-semibold">
              <Camera className="mr-1.5 h-4 w-4" /> Scan
            </Button>
            <Button variant="ghost" size="icon" onClick={toggleTheme} aria-label="Toggle theme">
              {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>
          </div>
        </header>
        <PrintQueueBar />
        <BoxConfirm />
        <main className="min-w-0 flex-1 p-4 pb-28 sm:p-6 sm:pb-6">{children}</main>
        <BottomBar path={path} />
      </SidebarInset>
      {camera && <CameraScanner onClose={() => setCamera(false)} />}
      {typing && <TypeCode onClose={() => setTyping(false)} />}
    </SidebarProvider>
  );
}
