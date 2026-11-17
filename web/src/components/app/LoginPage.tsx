import { useEffect, useState, type FormEvent } from 'react';
import { motion } from 'framer-motion';
import { Package } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useAuth } from '@/context/AuthContext';
import { get, post, ApiError } from '@/api/client';
import { navigate, useQueryParam } from '@/lib/router';
import type { User } from '@/api/types';
import { APP } from '@/lib/appConfig';

function Frame({ subtitle, children }: { subtitle: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, ease: 'easeOut' }} className="w-full max-w-[380px]">
        <Card>
          <CardHeader className="text-center">
            <div className="mx-auto mb-2 flex h-11 w-11 items-center justify-center rounded-xl bg-primary text-primary-foreground">
              <Package className="h-6 w-6" />
            </div>
            <CardTitle className="text-2xl font-bold">{APP.name}</CardTitle>
            <p className="text-sm text-muted-foreground">{subtitle}</p>
          </CardHeader>
          <CardContent>{children}</CardContent>
        </Card>
      </motion.div>
    </div>
  );
}

export function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not reach the server');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Frame subtitle={`${APP.site} · sign in`}>
      <form onSubmit={submit} className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="username">Username</Label>
          <Input id="username" autoComplete="username" autoCapitalize="none" autoCorrect="off" value={username}
            onChange={(e) => setUsername(e.target.value)} required autoFocus className="h-11 text-base" />
        </div>
        <div className="space-y-2">
          <Label htmlFor="password">Password</Label>
          <Input id="password" type="password" autoComplete="current-password" value={password}
            onChange={(e) => setPassword(e.target.value)} required className="h-11 text-base" />
        </div>
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <Button type="submit" className="h-11 w-full text-base" disabled={busy}>{busy ? 'Signing in...' : 'Sign in'}</Button>
        <p className="text-center text-xs text-muted-foreground">You stay signed in on this device for 30 days after your last visit.</p>
      </form>
    </Frame>
  );
}

export function SetupPage() {
  const token = useQueryParam('token') ?? '';
  const { setUser } = useAuth();
  const [who, setWho] = useState<{ username: string; display_name: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pw, setPw] = useState('');
  const [pw2, setPw2] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    get<{ user: { username: string; display_name: string } }>(`/api/auth/setup?token=${encodeURIComponent(token)}`)
      .then((r) => setWho(r.user))
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Could not check the link'));
  }, [token]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (pw !== pw2) return setError('The two passwords do not match');
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ user: User }>('/api/auth/setup', { token, password: pw });
      setUser(r.user);
      navigate('/', true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not save the password');
    } finally {
      setBusy(false);
    }
  };

  if (!who) return <Frame subtitle="Set up your account">{error ? <p className="text-sm text-destructive">{error}</p> : <p className="text-sm">Checking link...</p>}</Frame>;
  return (
    <Frame subtitle={`Welcome, ${who.display_name}. Pick a password.`}>
      <form onSubmit={submit} className="space-y-4">
        <div className="rounded-md bg-muted px-3 py-2 text-sm">Your username is <b>{who.username}</b></div>
        <input type="text" name="username" autoComplete="username" value={who.username} readOnly hidden />
        <div className="space-y-2">
          <Label htmlFor="pw">New password (10+ characters)</Label>
          <Input id="pw" type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} required minLength={10} className="h-11 text-base" />
        </div>
        <div className="space-y-2">
          <Label htmlFor="pw2">Same password again</Label>
          <Input id="pw2" type="password" autoComplete="new-password" value={pw2} onChange={(e) => setPw2(e.target.value)} required className="h-11 text-base" />
        </div>
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <Button type="submit" className="h-11 w-full text-base" disabled={busy}>{busy ? 'Saving...' : 'Save and sign in'}</Button>
      </form>
    </Frame>
  );
}
