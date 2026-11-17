"""Business settings that differ from one warehouse to the next.

Every value comes from an environment variable. The defaults are this warehouse's own values, so a server
with no settings behaves as it always has. Another business sets its own in the env file the server loads.

    BOXLOFT_APP_NAME   the app's name in the header, sign-in page and labels   (Boxloft)
    BOXLOFT_SITE_NAME  the building where boxes sit on shelves                  (Main warehouse)
    BOXLOFT_PLACE      the town under the name in the sidebar ('' hides it)     ('')
    BOXLOFT_TZ         the warehouse's time zone (IANA name)                    (America/New_York)
    BOXLOFT_TZ_LABEL   the short label after times ("ET")                       (ET for America/New_York)
    BOXLOFT_PUBLIC_URL the address people open (eBay and Amazon consent redirects) (http://localhost:5101)

The server is on UTC. "Today" and every time shown to a person are in BOXLOFT_TZ."""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

APP_NAME = os.environ.get('BOXLOFT_APP_NAME', '').strip() or 'Boxloft'
SITE_NAME = os.environ.get('BOXLOFT_SITE_NAME', '').strip() or 'Main warehouse'
PLACE = os.environ.get('BOXLOFT_PLACE', '').strip()
TZ_NAME = os.environ.get('BOXLOFT_TZ', '').strip() or 'America/New_York'
TZ = ZoneInfo(TZ_NAME)
_LABELS = {'America/New_York': 'ET', 'America/Chicago': 'CT', 'America/Denver': 'MT', 'America/Phoenix': 'MST',
           'America/Los_Angeles': 'PT', 'UTC': 'UTC'}
TZ_LABEL = (os.environ.get('BOXLOFT_TZ_LABEL', '').strip() or _LABELS.get(TZ_NAME)
            or datetime.now(TZ).tzname() or TZ_NAME)
PUBLIC_URL = os.environ.get('BOXLOFT_PUBLIC_URL', '').strip() or 'http://localhost:5101'


def client() -> dict:
    """What the web app needs before its first request (put into index.html as window.__APP__)."""
    return {'name': APP_NAME, 'site': SITE_NAME, 'place': PLACE, 'tz': TZ_NAME, 'tzLabel': TZ_LABEL}


def client_script() -> bytes:
    # </ can't close the script tag: json.dumps leaves '/' alone, so escape '<'.
    return ('<script>window.__APP__=' + json.dumps(client()).replace('<', '\\u003c') + ';</script>').encode()
