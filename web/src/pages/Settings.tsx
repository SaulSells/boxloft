import { useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, Download, ShieldCheck } from 'lucide-react';
import { get, post } from '@/api/client';
import type { Integrity, Owner } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { PageHeader } from '@/components/app/common';
import { Connections } from '@/pages/Connections';
import { useToast } from '@/components/app/toast';
import { useAuth } from '@/context/AuthContext';
import { ago, plural, when } from '@/lib/format';
import { Link } from '@/lib/router';
import { LABEL_SIZES, getLabelSize, setLabelSize, printLabels, type Label as LabelT } from '@/lib/labels';

interface UserRow { id: number; username: string; display_name: string; role: string; is_active: boolean; last_login_at: string | null; has_password: boolean; setup_pending: boolean }
interface Session { id: string; last_seen_at: string; ip: string; user_agent: string; current: boolean }

function SetupLink({ path }: { path: string }) {
  const url = `${location.origin}${path}`;
  const toast = useToast();
  return (
    <div className="mt-2 rounded-md border border-primary/40 bg-primary/5 p-3 text-sm">
      <div className="mb-1 font-medium">Setup link (works once, for 48 hours). Send it to them directly:</div>
      <div className="flex gap-2"><Input readOnly value={url} className="h-9 font-mono text-xs" onFocus={(e) => e.target.select()} />
        <Button size="sm" variant="outline" onClick={() => { void navigator.clipboard.writeText(url); toast('Copied'); }}><Copy className="h-3.5 w-3.5" /></Button></div>
    </div>
  );
}

function Users() {
  const qc = useQueryClient();
  const toast = useToast();
  const { user } = useAuth();
  const users = useQuery({ queryKey: ['users'], queryFn: () => get<{ users: UserRow[] }>('/api/users') });
  const [username, setUsername] = useState('');
  const [display, setDisplay] = useState('');
  const [role, setRole] = useState('staff');
  const [link, setLink] = useState<{ id: number | 'new'; path: string } | null>(null);
  const create = useMutation({
    mutationFn: () => post<{ setup_path: string }>('/api/users', { username, display_name: display, role }),
    onSuccess: (r) => { setLink({ id: 'new', path: r.setup_path }); setUsername(''); setDisplay(''); qc.invalidateQueries({ queryKey: ['users'] }); },
    onError: (e: Error) => toast(e.message, { tone: 'error' }),
  });
  const reset = async (id: number) => { const r = await post<{ setup_path: string }>(`/api/users/${id}/reset`); setLink({ id, path: r.setup_path }); qc.invalidateQueries({ queryKey: ['users'] }); };
  const setActive = async (id: number, is_active: boolean) => { try { await post(`/api/users/${id}`, { is_active }); qc.invalidateQueries({ queryKey: ['users'] }); } catch (e) { toast((e as Error).message, { tone: 'error' }); } };
  return (
    <Card>
      <CardHeader className="pb-2"><CardTitle className="text-base">People</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <div className="divide-y text-sm">
          {users.data?.users.map((u) => (
            <div key={u.id} className="py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{u.display_name}</span><span className="font-mono text-xs text-muted-foreground">{u.username}</span>
                <Badge variant={u.role === 'admin' ? 'default' : 'secondary'}>{u.role}</Badge>
                {!u.is_active && <Badge variant="destructive">off</Badge>}
                {u.setup_pending && <Badge variant="outline">setup link sent</Badge>}
                <span className="ml-auto text-xs text-muted-foreground">last sign-in {u.last_login_at ? ago(u.last_login_at) : 'never'}</span>
              </div>
              {u.id !== user?.id && (
                <div className="mt-1 flex gap-2">
                  <Button size="sm" variant="outline" onClick={() => reset(u.id)}>New setup link</Button>
                  <Button size="sm" variant="ghost" onClick={() => setActive(u.id, !u.is_active)}>{u.is_active ? 'Turn off' : 'Turn on'}</Button>
                </div>
              )}
              {link?.id === u.id && <SetupLink path={link.path} />}
            </div>
          ))}
        </div>
        <form className="grid gap-2 sm:grid-cols-4" onSubmit={(e: FormEvent) => { e.preventDefault(); create.mutate(); }}>
          <Input placeholder="username" value={username} onChange={(e) => setUsername(e.target.value.toLowerCase())} className="h-10" autoCapitalize="none" required />
          <Input placeholder="Display name" value={display} onChange={(e) => setDisplay(e.target.value)} className="h-10" required />
          <Select value={role} onValueChange={setRole}><SelectTrigger className="h-10 w-full"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="staff">Staff</SelectItem><SelectItem value="admin">Admin</SelectItem></SelectContent></Select>
          <Button type="submit" className="h-10" disabled={create.isPending}>Add person</Button>
        </form>
        {link?.id === 'new' && <SetupLink path={link.path} />}
      </CardContent>
    </Card>
  );
}

function Owners() {
  const qc = useQueryClient();
  const toast = useToast();
  const owners = useQuery({ queryKey: ['owners'], queryFn: () => get<{ owners: Owner[] }>('/api/owners') });
  const [name, setName] = useState('');
  const add = async (e: FormEvent) => {
    e.preventDefault();
    try { await post('/api/owners', { name, is_client: true }); setName(''); qc.invalidateQueries({ queryKey: ['owners'] }); } catch (err) { toast((err as Error).message, { tone: 'error' }); }
  };
  return (
    <Card>
      <CardHeader className="pb-2"><CardTitle className="text-base">Stock owners</CardTitle></CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-muted-foreground">Every product belongs to one owner, so Boxloft's stock and 3PL client stock never mix in counts or value.</p>
        <div className="flex flex-wrap gap-2">{owners.data?.owners.map((o) => <Badge key={o.id} variant={o.is_client ? 'secondary' : 'default'}>{o.name}{o.is_client ? ' · client' : ''}</Badge>)}</div>
        <form onSubmit={add} className="flex gap-2"><Input placeholder="New 3PL client, e.g. Acme Goods" value={name} onChange={(e) => setName(e.target.value)} className="h-10" required /><Button type="submit" className="h-10">Add</Button></form>
      </CardContent>
    </Card>
  );
}

interface TokenRow { id: number; name: string; created_at: string; last_used_at: string | null; revoked: number }

function AmazonSync() {
  const qc = useQueryClient();
  const toast = useToast();
  const tokens = useQuery({ queryKey: ['tokens'], queryFn: () => get<{ tokens: TokenRow[] }>('/api/tokens') });
  const [fresh, setFresh] = useState<string | null>(null);
  const make = async () => {
    const r = await post<{ token: string }>('/api/tokens', { name: 'Chrome extension' });
    setFresh(r.token);
    qc.invalidateQueries({ queryKey: ['tokens'] });
  };
  return (
    <Card>
      <CardHeader className="pb-2"><CardTitle className="text-base">Amazon sync (Chrome extension)</CardTitle></CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-muted-foreground">The extension reads your Amazon orders in Chrome and sends them here every 30 minutes. It signs in with a token, which can only send orders, not read anything else.</p>
        <Button onClick={make}>Make a new token</Button>
        {fresh && (
          <div className="rounded-md border border-primary/40 bg-primary/5 p-3">
            <div className="mb-1 font-medium">Copy it now. It won't be shown again.</div>
            <div className="flex gap-2"><Input readOnly value={fresh} className="h-9 font-mono text-xs" onFocus={(e) => e.target.select()} />
              <Button size="sm" variant="outline" onClick={() => { void navigator.clipboard.writeText(fresh); toast('Copied'); }}><Copy className="h-3.5 w-3.5" /></Button></div>
          </div>
        )}
        <div className="divide-y text-xs">
          {tokens.data?.tokens.map((t) => (
            <div key={t.id} className="flex items-center justify-between gap-2 py-1.5">
              <span>{t.name} · made {ago(t.created_at)} · used {t.last_used_at ? ago(t.last_used_at) : 'never'}</span>
              {t.revoked ? <Badge variant="outline">revoked</Badge> : <Button size="sm" variant="ghost" className="h-7" onClick={async () => { await post(`/api/tokens/${t.id}/revoke`); qc.invalidateQueries({ queryKey: ['tokens'] }); }}>Revoke</Button>}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

export function Settings() {
  const { user } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const [device, setDevice] = useState(() => { try { return localStorage.getItem('boxloft-device') || ''; } catch { return ''; } });
  const [cur, setCur] = useState('');
  const [nw, setNw] = useState('');
  const [sizes, setSizes] = useState(() => ({ box: getLabelSize('box'), location: getLabelSize('location'), unit: getLabelSize('unit'), fnsku: getLabelSize('fnsku') }));
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => get<{ sessions: Session[] }>('/api/auth/sessions') });
  const integrity = useQuery({ queryKey: ['integrity'], queryFn: () => get<Integrity>('/api/integrity') });

  const changePw = async (e: FormEvent) => {
    e.preventDefault();
    try { await post('/api/auth/password', { current: cur, new: nw }); setCur(''); setNw(''); toast('Password changed. Other devices are signed out.'); qc.invalidateQueries({ queryKey: ['sessions'] }); }
    catch (err) { toast((err as Error).message, { tone: 'error' }); }
  };
  const runCheck = async () => { const r = await post<Integrity>('/api/integrity/run'); qc.setQueryData(['integrity'], r); qc.invalidateQueries({ queryKey: ['summary'] }); toast(r.ok ? 'Ledger check passed' : `${r.problem_count} problems found`, { tone: r.ok ? 'ok' : 'error' }); };
  const testLabel = (kind: LabelT['kind']) => printLabels([{ kind, code: kind === 'fnsku' ? 'X00TESTLBL' : 'SB00000000', big: 'TEST', lines: ['Boxloft printer test', new Date().toLocaleString()] }]);

  return (
    <div className="max-w-3xl space-y-4 sm:space-y-6">
      <PageHeader title="Settings" subtitle={`Signed in as ${user?.display_name} (${user?.username})`} />

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">This device</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5"><Label>Device name (shows in the activity log)</Label>
            <Input value={device} placeholder="e.g. Sam's phone, Packing desk" className="h-10"
              onChange={(e) => { setDevice(e.target.value); try { localStorage.setItem('boxloft-device', e.target.value); } catch { /* ignore */ } }} /></div>
          <div className="space-y-2">
            <Label>Label sizes on this device's printer</Label>
            {(['box', 'location', 'unit', 'fnsku'] as const).map((k) => (
              <div key={k} className="flex items-center gap-2">
                <span className="w-24 shrink-0 text-sm capitalize">{k === 'fnsku' ? 'FNSKU' : k}</span>
                <Select value={sizes[k]} onValueChange={(v) => { setLabelSize(k, v as typeof sizes.box); setSizes({ ...sizes, [k]: v }); }}>
                  <SelectTrigger className="h-9 flex-1"><SelectValue /></SelectTrigger>
                  <SelectContent>{LABEL_SIZES.map((s) => <SelectItem key={s.value} value={s.value}>{s.label}</SelectItem>)}</SelectContent>
                </Select>
                <Button size="sm" variant="outline" onClick={() => testLabel(k)}>Test</Button>
              </div>
            ))}
            <p className="text-xs text-muted-foreground">In the print dialog, pick the label printer, set paper to the same size, margins none, scale 100%. Chrome remembers it.</p>
            <p className="text-xs text-muted-foreground">Phones send labels to the <Link to="/print-queue" className="underline">Print queue</Link>; the computer with the printer prints them. Set which device has the printer there.</p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">Your account</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <form onSubmit={changePw} className="grid gap-2 sm:grid-cols-3">
            <input type="text" autoComplete="username" value={user?.username ?? ''} readOnly hidden />
            <Input type="password" autoComplete="current-password" placeholder="Current password" value={cur} onChange={(e) => setCur(e.target.value)} className="h-10" required />
            <Input type="password" autoComplete="new-password" placeholder="New password (10+)" value={nw} onChange={(e) => setNw(e.target.value)} className="h-10" required minLength={10} />
            <Button type="submit" className="h-10">Change password</Button>
          </form>
          <div>
            <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-sm font-medium">Signed-in devices
              <Button size="sm" variant="outline" onClick={async () => { const r = await post<{ signed_out: number }>('/api/auth/sessions/logout-others'); toast(`Signed out ${plural(r.signed_out, 'other device')}`); qc.invalidateQueries({ queryKey: ['sessions'] }); }}>Sign out others</Button>
            </div>
            <div className="divide-y text-xs">
              {sessions.data?.sessions.map((s) => (
                <div key={s.id} className="flex items-center justify-between gap-2 py-1.5"><span className="min-w-0 truncate">{s.user_agent.slice(0, 70)}</span>{s.current && <Badge className="shrink-0">this one</Badge>}<span className="shrink-0 text-muted-foreground">{ago(s.last_seen_at)}</span></div>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      {user?.is_admin && <Users />}
      {user?.is_admin && <Owners />}
      {user?.is_admin && <Connections />}
      {user?.is_admin && <AmazonSync />}

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">Your data</CardTitle></CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="flex flex-wrap gap-2">
            {[['inventory', 'Stock by box'], ['products', 'Products'], ['ledger', 'Full ledger']].map(([k, l]) => (
              <Button key={k} variant="outline" asChild><a href={`/api/export/${k}.csv`}><Download className="mr-1.5 h-4 w-4" />{l} CSV</a></Button>
            ))}
          </div>
          <div className="flex items-start gap-3 rounded-md border p-3">
            <ShieldCheck className={`mt-0.5 h-5 w-5 ${integrity.data?.ok === false ? 'text-destructive' : 'text-primary'}`} />
            <div className="flex-1">
              <div className="font-medium">Ledger check {integrity.data?.at ? (integrity.data.ok ? 'passed' : `found ${integrity.data.problem_count} problems`) : ''}</div>
              <div className="text-muted-foreground">Rebuilds every count from the move history and compares it with what the app shows. Also checks that no shelf holds a negative count and each one-off unit is in exactly one place. {integrity.data?.at && `Last run ${when(integrity.data.at)}.`}</div>
              {integrity.data && !integrity.data.ok && <pre className="mt-2 max-h-40 overflow-auto rounded bg-muted p-2 text-xs">{JSON.stringify(integrity.data.problems, null, 1)}</pre>}
            </div>
            <Button size="sm" variant="outline" onClick={runCheck}>Run now</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
