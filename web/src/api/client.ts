export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export function deviceName(): string {
  try {
    return localStorage.getItem('boxloft-device') || '';
  } catch {
    return '';
  }
}

async function parse<T>(res: Response): Promise<T> {
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError(res.ok ? 'Bad response from server' : `Server error (${res.status})`, res.status);
  }
  if (!res.ok) {
    const msg = (data as { error?: string } | null)?.error || `Request failed (${res.status})`;
    if (res.status === 401) window.dispatchEvent(new Event('boxloft:signed-out'));
    // Someone else saved first (two people on one item): every screen reloads its data (App.tsx).
    if (res.status === 409) window.dispatchEvent(new Event('boxloft:changed'));
    throw new ApiError(msg, res.status);
  }
  return data as T;
}

const headers = () => ({ Accept: 'application/json', 'X-Boxloft': '1', 'X-Device': deviceName() });

export async function get<T>(path: string): Promise<T> {
  return parse<T>(await fetch(path, { credentials: 'same-origin', headers: headers() }));
}

export async function post<T>(path: string, body?: unknown): Promise<T> {
  return parse<T>(
    await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

export async function upload<T>(path: string, blob: Blob): Promise<T> {
  return parse<T>(
    await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { ...headers(), 'Content-Type': blob.type },
      body: blob,
    }),
  );
}
