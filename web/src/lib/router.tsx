import { useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from 'react';

// A small history-API router: the app has a dozen flat pages, no nesting.

export function navigate(to: string, replace = false) {
  if (to === location.pathname + location.search) return;
  if (replace) history.replaceState(null, '', to);
  else history.pushState(null, '', to);
  window.dispatchEvent(new Event('boxloft:navigate'));
  window.scrollTo(0, 0);
}

export function useLocation() {
  const [loc, setLoc] = useState({ path: location.pathname, search: location.search });
  useEffect(() => {
    const update = () => setLoc({ path: location.pathname, search: location.search });
    window.addEventListener('popstate', update);
    window.addEventListener('boxloft:navigate', update);
    return () => {
      window.removeEventListener('popstate', update);
      window.removeEventListener('boxloft:navigate', update);
    };
  }, []);
  return loc;
}

export function useQueryParam(name: string): string | null {
  const { search } = useLocation();
  return new URLSearchParams(search).get(name);
}

export function match(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split('/');
  const a = path.replace(/\/+$/, '').split('/');
  if (p.length !== a.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < p.length; i++) {
    const seg = p[i]!;
    if (seg.startsWith(':')) params[seg.slice(1)] = decodeURIComponent(a[i]!);
    else if (seg !== a[i]) return null;
  }
  return params;
}

export function Link({ to, onClick, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) {
  const handle = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    navigate(to);
  };
  return <a href={to} onClick={handle} {...rest} />;
}
