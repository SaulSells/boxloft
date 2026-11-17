import { useEffect, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, Copy, ExternalLink, XCircle } from 'lucide-react';
import { get, post } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useToast } from '@/components/app/toast';
import { ago } from '@/lib/format';

interface Field { label: string; secret: boolean; set: boolean; shown: string }
interface TestResult { ok: boolean; detail: string; at: string }
interface Conn {
  fields: Record<string, Field>;
  last_test: TestResult | null;
  saved_at: string | null;
  connected?: boolean;
  ebay_user?: string;
  refresh_expires_at?: string | null;
  deletion_url?: string;
  deletion_token?: string;
  login_url?: string;
  selling_partner_id?: string;
  accept_url?: string;
  sync?: { running?: boolean; text?: string; last_ok_at?: string | null; last_run_at?: string | null; last_error?: string | null; last_result?: string | null };
}
type Channel = 'ebay' | 'amazon' | 'walmart' | 'gmail';

const INFO: Record<Channel, { title: string; what: string; steps: string[]; link?: [string, string] }> = {
  ebay: {
    title: 'eBay (selling)',
    what: 'Brings in eBay sales, payouts and returns. Read-only: Boxloft never changes a listing.',
    link: ['https://developer.ebay.com/my/keys', 'eBay developer keys'],
    steps: [
      'Sign in at developer.ebay.com with your eBay seller account. Make a Production keyset.',
      'Alerts & Notifications: choose Marketplace Account Deletion. Paste the endpoint and the token below, then press Save. eBay checks the endpoint at once.',
      'User Tokens: "Get a Token from eBay via Your Application", add an eBay Redirect URL. Set its auth accepted URL to the accepted URL below. Copy its RuName.',
      'Paste the App ID, Cert ID and RuName here. Press Save, then Test, then Connect eBay.',
    ],
  },
  amazon: {
    title: 'Amazon Seller Central (SP-API)',
    what: 'Brings in Amazon orders (FBA and FBM), listings, FBA stock, returns and fees. Read-only.',
    link: ['https://sellercentral.amazon.com/sellingpartner/developerconsole', 'Seller Central developer console'],
    steps: [
      'Seller Central: Apps and Services, Develop Apps. Make a developer profile as a private developer (apps for your own company).',
      'Roles: Inventory and Order Tracking, Product Listing, Pricing, Amazon Fulfillment, Finance and Accounting, Selling Partner Insights. No restricted roles.',
      'After Amazon approves: Add new app client (SP API, Production), Sellers only. Put the 2 URIs below in its OAuth Login URI and OAuth Redirect URI.',
      'Paste the App ID (amzn1.sp.solution...) and the LWA Client ID and Secret ("View"). Save, then press Connect Amazon while signed in to Seller Central.',
    ],
  },
  walmart: {
    title: 'Walmart Marketplace (selling)',
    what: 'Brings in Walmart Marketplace orders and returns.',
    link: ['https://seller.walmart.com/', 'Walmart Seller Center'],
    steps: [
      'Seller Center: Settings, API Integration, API Key Management. It opens the Developer Portal.',
      'Copy your own Client ID and Client Secret (the pair with the lock icon). Paste them here, Save, Test.',
    ],
  },
  gmail: {
    title: 'Gmail (buying from any store)',
    what: 'Reads order confirmation emails from any store (not Amazon or eBay), read-only. Orders paid with the business card wait on Arriving under "Store orders to check" until someone approves them.',
    link: ['https://myaccount.google.com/apppasswords', 'Google app passwords'],
    steps: [
      'Your Google account needs 2-Step Verification on.',
      'Google account: App passwords. Name it Boxloft and press Create. Copy the 16 letters.',
      'Paste your Gmail address, the app password and the business card\'s last 4 digits here, Save, Test. Orders paid with other cards are skipped. To stop it, delete the app password at Google.',
    ],
  },
};

function CopyRow({ label, value }: { label: string; value: string }) {
  const toast = useToast();
  return (
    <div className="space-y-1">
      <Label className="text-xs">{label}</Label>
      <div className="flex gap-2">
        <Input readOnly value={value} className="h-9 font-mono text-xs" onFocus={(e) => e.target.select()} />
        <Button type="button" size="sm" variant="outline" onClick={() => { void navigator.clipboard.writeText(value); toast('Copied'); }}><Copy className="h-3.5 w-3.5" /></Button>
      </div>
    </div>
  );
}

function ConnectionCard({ ch, conn }: { ch: Channel; conn: Conn }) {
  const qc = useQueryClient();
  const toast = useToast();
  const info = INFO[ch];
  const [vals, setVals] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const anySet = Object.values(conn.fields).some((f) => f.set);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); } catch (e) { toast((e as Error).message, { tone: 'error' }); } finally { setBusy(false); qc.invalidateQueries({ queryKey: ['connections'] }); }
  };
  const save = (e: FormEvent) => { e.preventDefault(); void run(async () => { await post(`/api/connections/${ch}`, vals); setVals({}); toast('Saved'); }); };
  const test = () => run(async () => { const r = await post<TestResult>(`/api/connections/${ch}/test`); toast(r.detail, { tone: r.ok ? 'ok' : 'error' }); });
  const connect = () => run(async () => { const r = await post<{ url: string }>(`/api/${ch}/connect`); window.location.href = r.url; });
  const clear = () => run(async () => { await post(`/api/connections/${ch}/clear`); toast('Removed'); });
  const syncNow = () => run(async () => { await post(`/api/connections/${ch}/sync`); toast('Order sync started'); });
  const sync = conn.sync;
  const res = sync?.last_result ? (JSON.parse(sync.last_result) as Record<string, number>) : null;

  const t = conn.last_test;
  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{info.title}</span>
        {t ? (t.ok ? <Badge><CheckCircle2 className="mr-1 h-3 w-3" />works</Badge> : <Badge variant="destructive"><XCircle className="mr-1 h-3 w-3" />not working</Badge>)
          : <Badge variant="outline">{anySet ? 'saved, not tested' : 'not set up'}</Badge>}
        {ch === 'ebay' && conn.connected && <Badge variant="secondary">connected{conn.ebay_user ? ` as ${conn.ebay_user}` : ''}</Badge>}
      </div>
      <p className="text-muted-foreground">{info.what}</p>
      {t && <p className={`text-xs ${t.ok ? 'text-muted-foreground' : 'text-destructive'}`}>Last test {ago(t.at)}: {t.detail}</p>}
      <details className="text-xs" open={!anySet}>
        <summary className="cursor-pointer font-medium">How to get the keys</summary>
        <ol className="mt-1 list-decimal space-y-1 pl-5 text-muted-foreground">{info.steps.map((s) => <li key={s}>{s}</li>)}</ol>
        {info.link && <a className="mt-1 inline-flex items-center gap-1 text-primary underline" href={info.link[0]} target="_blank" rel="noreferrer">{info.link[1]}<ExternalLink className="h-3 w-3" /></a>}
      </details>
      {ch === 'amazon' && conn.login_url && (
        <div className="grid gap-2 sm:grid-cols-2">
          <CopyRow label="OAuth Login URI (on the app)" value={conn.login_url} />
          <CopyRow label="OAuth Redirect URI (on the app)" value={conn.accept_url ?? ''} />
        </div>
      )}
      {ch === 'amazon' && conn.selling_partner_id && <p className="text-xs text-muted-foreground">Connected to seller {conn.selling_partner_id}.</p>}
      {ch === 'ebay' && conn.deletion_url && (
        <div className="grid gap-2 sm:grid-cols-2">
          <CopyRow label="Account deletion endpoint" value={conn.deletion_url} />
          <CopyRow label="Verification token" value={conn.deletion_token ?? ''} />
          <CopyRow label="Auth accepted URL (for the RuName)" value={conn.accept_url ?? ''} />
        </div>
      )}
      <form onSubmit={save} className="space-y-2">
        <div className="grid gap-2 sm:grid-cols-2">
          {Object.entries(conn.fields).map(([k, f]) => (
            <div key={k} className="space-y-1">
              <Label className="text-xs" htmlFor={`conn-${ch}-${k}`}>{f.label}</Label>
              <Input id={`conn-${ch}-${k}`} type={f.secret ? 'password' : 'text'} autoComplete="off" spellCheck={false} className="h-9 font-mono text-xs"
                placeholder={f.set ? `saved ${f.shown}` : ''} value={vals[k] ?? ''} onChange={(e) => setVals({ ...vals, [k]: e.target.value })} />
            </div>
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" size="sm" disabled={busy || !Object.values(vals).some((v) => v.trim())}>Save</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy || !anySet} onClick={test}>Test</Button>
          {ch === 'ebay' && <Button type="button" size="sm" variant="outline" disabled={busy || !conn.fields.ru_name?.set} onClick={connect}>{conn.connected ? 'Reconnect eBay' : 'Connect eBay'}</Button>}
          {ch === 'amazon' && <Button type="button" size="sm" variant="outline" disabled={busy || !conn.fields.app_id?.set || !conn.fields.lwa_client_secret?.set} onClick={connect}>{conn.connected ? 'Reconnect Amazon' : 'Connect Amazon'}</Button>}
          {anySet && <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={clear}>Remove keys</Button>}
        </div>
        {sync && (
          <div className="flex flex-wrap items-center gap-2 rounded-md bg-muted/50 px-3 py-2 text-xs">
            <span className="font-medium">Orders:</span>
            {sync.running ? <span>syncing… {sync.text}</span>
              : sync.last_ok_at && ch === 'gmail' ? <span>last read {ago(sync.last_ok_at)}{res ? ` · ${res.emails} new emails, ${res.order} orders${res.other_card ? `, ${res.other_card} on other cards` : ''}${res.cancelled ? `, ${res.cancelled} cancelled by the store` : ''}${res.error ? `, ${res.error} failed (retried next time)` : ''}` : ''}</span>
              : sync.last_ok_at ? <span>last synced {ago(sync.last_ok_at)}{res ? ` · ${res.orders} changed, ${res.new_orders} new${res.fba ? ` (${res.fba} FBA)` : ''}${res.unmatched ? `, ${res.unmatched} lines need a product` : ''}${res.review ? `, ${res.review} to check` : ''}` : ''}</span>
              : <span className="text-muted-foreground">not synced yet (runs every 15 min once Test passes)</span>}
            {!sync.running && sync.last_error && <span className="text-destructive">last try failed: {sync.last_error}</span>}
            <Button type="button" size="sm" variant="outline" className="ml-auto h-7" disabled={busy || sync.running || !t?.ok} onClick={syncNow}>Sync now</Button>
          </div>
        )}
        {ch === 'ebay' && conn.refresh_expires_at && <p className="text-xs text-muted-foreground">eBay consent lasts until {new Date(conn.refresh_expires_at).toLocaleDateString()}. Press Reconnect before then.</p>}
      </form>
    </div>
  );
}

export function Connections() {
  const toast = useToast();
  const conns = useQuery({
    queryKey: ['connections'],
    queryFn: () => get<{ connections: Record<Channel, Conn> }>('/api/connections'),
    refetchInterval: (q) => (Object.values(q.state.data?.connections ?? {}).some((c) => c.sync?.running) ? 3000 : false),
  });
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    if (q.get('ebay') === 'connected') toast(`eBay connected${q.get('user') ? ` as ${q.get('user')}` : ''}`, { tone: 'ok' });
    if (q.get('ebay_error')) toast(q.get('ebay_error') ?? '', { tone: 'error' });
    if (q.get('ebay') === 'declined') toast('eBay consent was declined', { tone: 'error' });
    if (q.get('amazon') === 'connected') toast(`Amazon connected${q.get('seller') ? ` (seller ${q.get('seller')})` : ''}`, { tone: 'ok' });
    if (q.get('amazon_error')) toast(q.get('amazon_error') ?? '', { tone: 'error' });
    if (q.has('ebay') || q.has('ebay_error') || q.has('amazon') || q.has('amazon_error')) history.replaceState(null, '', location.pathname);
  }, [toast]);
  const order: Channel[] = ['ebay', 'amazon', 'walmart', 'gmail'];
  return (
    <Card>
      <CardHeader className="pb-2"><CardTitle className="text-base">Connections</CardTitle></CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-muted-foreground">Keys stay on the server, outside the database and its backups. This page only shows the last 4 characters. Test makes one read-only call.</p>
        {conns.data && order.map((ch) => <ConnectionCard key={ch} ch={ch} conn={conns.data.connections[ch]} />)}
      </CardContent>
    </Card>
  );
}
