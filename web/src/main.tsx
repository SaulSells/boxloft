import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './globals.css';

// Alex 11-17: opening the scanner stopped the music or video he was listening to. Safari on iPhone takes the phone's
// audio for a page that uses the camera or plays a sound (the scan beep). 'ambient' (Audio Session API, Safari 16.4+)
// says Boxloft's sounds mix with other apps' audio. Ambient sounds follow the silent switch: on silent, no beep.
try {
  const session = (navigator as Navigator & { audioSession?: { type: string } }).audioSession;
  if (session) session.type = 'ambient';
} catch {
  // older Safari or another browser: nothing to set
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
