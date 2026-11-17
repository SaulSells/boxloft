/** Business settings from the server (backend/config.py), put into index.html as window.__APP__.
 *  The fallbacks are for the Vite dev server, which serves index.html itself. */
type AppConfig = { name: string; site: string; place: string; tz: string; tzLabel: string };

const w = ((globalThis as { __APP__?: Partial<AppConfig> }).__APP__ ?? {}) as Partial<AppConfig>;

export const APP: AppConfig = {
  name: w.name || 'Boxloft',
  site: w.site || 'Main warehouse',
  place: w.place ?? '',
  tz: w.tz || 'America/New_York',
  tzLabel: w.tzLabel || 'ET',
};
