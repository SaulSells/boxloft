import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import { get, post, ApiError } from '@/api/client';
import type { User } from '@/api/types';

interface AuthValue {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  setUser: (u: User) => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    get<{ user: User }>('/api/auth/me')
      .then((r) => setUser(r.user))
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) console.error(e);
      })
      .finally(() => setLoading(false));
    const out = () => setUser(null);
    window.addEventListener('boxloft:signed-out', out);
    return () => window.removeEventListener('boxloft:signed-out', out);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const r = await post<{ user: User }>('/api/auth/login', { username, password });
    setUser(r.user);
  }, []);

  const logout = useCallback(async () => {
    try {
      await post('/api/auth/logout');
    } finally {
      setUser(null);
    }
  }, []);

  return <AuthContext.Provider value={{ user, loading, login, logout, setUser }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth outside AuthProvider');
  return ctx;
}
