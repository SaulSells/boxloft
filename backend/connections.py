"""Marketplace and mailbox connections: where the keys live and a live test for each.

Keys never go in the database (the database is copied to backups and to Alex's Mac).
They live in DATA/secrets/connections.json, mode 600, written atomically. The API
only ever returns the last 4 characters of a secret.

Channels:
  ebay     selling: App ID, Cert ID, RuName; user consent gives the refresh token
  amazon   selling: SP-API private app (LWA client id/secret + self-authorized refresh token)
  walmart  selling: Marketplace Client ID / Client Secret
  gmail    buying from other stores: Gmail address + app password (IMAP, read-only)

Every outbound call is stdlib urllib/imaplib. Tests only read; nothing here changes a listing,
an order or a mailbox.
"""

import base64
import hashlib
import imaplib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

EBAY_API = 'https://api.ebay.com'
EBAY_AUTH = 'https://auth.ebay.com/oauth2/authorize'
EBAY_SCOPES = [
    'https://api.ebay.com/oauth/api_scope',
    'https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly',
    'https://api.ebay.com/oauth/api_scope/sell.finances',
    'https://api.ebay.com/oauth/api_scope/sell.inventory.readonly',
    'https://api.ebay.com/oauth/api_scope/sell.account.readonly',
]
LWA_TOKEN = 'https://api.amazon.com/auth/o2/token'
SPAPI_NA = 'https://sellingpartnerapi-na.amazon.com'
WALMART_API = 'https://marketplace.walmartapis.com'
GMAIL_IMAP = 'imap.gmail.com'
STORE_QUERY = ('subject:(order OR receipt OR invoice OR confirmed OR confirmation OR purchase) '
               '-from:amazon.com -from:ebay.com -category:promotions -category:social')  # = store_mail.ORDER_QUERY

# Fields per channel: (key, label, is_secret). Order is the form order.
FIELDS = {
    'ebay': [('client_id', 'App ID (Client ID)', False), ('client_secret', 'Cert ID (Client Secret)', True),
             ('ru_name', 'RuName (eBay Redirect URL name)', False)],
    'amazon': [('app_id', 'App ID (amzn1.sp.solution...)', False), ('lwa_client_id', 'LWA Client ID', False),
               ('lwa_client_secret', 'LWA Client Secret', True),
               ('refresh_token', 'Refresh token (only if you use Authorize app instead of Connect)', True)],
    'walmart': [('client_id', 'Client ID', False), ('client_secret', 'Client Secret', True)],
    'gmail': [('email', 'Gmail address', False), ('app_password', 'App password (16 letters)', True),
              ('card_last4', 'Business card, last 4 digits', False)],
}
# Values the server sets itself; never accepted from the form.
SERVER_KEYS = {'ebay': {'refresh_token', 'refresh_expires_at', 'oauth_state', 'deletion_token', 'ebay_user'},
               'amazon': {'oauth_state', 'selling_partner_id'}}
AMAZON_CONSENT = 'https://sellercentral.amazon.com/apps/authorize/consent'
AMAZON_CALLBACK_HOSTS = ('sellercentral.amazon.com', 'sellercentral.amazon.ca', 'sellercentral.amazon.com.mx',
                         'sellercentral.amazon.com.br')


class ConnectionError_(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _http(method: str, url: str, headers: dict = None, data=None, timeout: int = 20, raw_text: bool = False):
    """Returns (status, parsed json or text). Never raises on HTTP errors. raw_text: the whole body as text (XML)."""
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        status = e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f'network error: {e}'
    if raw_text:
        return status, raw
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw[:500]


def _err_text(payload) -> str:
    """Pulls the human part out of an API error body, without echoing anything secret."""
    if isinstance(payload, dict):
        for k in ('error_description', 'error', 'message'):
            if isinstance(payload.get(k), str):
                return payload[k][:200]
        errs = payload.get('errors')
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            e = errs[0]
            return str(e.get('message') or e.get('description') or e.get('longMessage') or e)[:200]
    return str(payload)[:200]


class Connections:
    def __init__(self, data_dir: str, public_url: str = 'http://localhost:5101'):
        self.dir = os.path.join(data_dir, 'secrets')
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.path = os.path.join(self.dir, 'connections.json')
        self.public_url = public_url.rstrip('/')
        self.lock = threading.Lock()
        self.deletion_notices = 0  # eBay sends every account deletion on eBay; count, don't log each

    # ------------------------------------------------------------------ storage

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self, data: dict):
        tmp = self.path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def get(self, channel: str) -> dict:
        with self.lock:
            return dict(self._load().get(channel) or {})

    def _update(self, channel: str, **values):
        with self.lock:
            data = self._load()
            cur = data.get(channel) or {}
            for k, v in values.items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v
            data[channel] = cur
            self._save(data)
            return dict(cur)

    # ------------------------------------------------------------------ public view

    @property
    def ebay_deletion_url(self) -> str:
        return f'{self.public_url}/api/ebay/account-deletion'

    @property
    def ebay_accept_url(self) -> str:
        return f'{self.public_url}/api/ebay/callback'

    def ebay_deletion_token(self) -> str:
        tok = self.get('ebay').get('deletion_token')
        if not tok:
            tok = 'bl_' + secrets.token_urlsafe(36).replace('-', '_')[:60]  # 32-80 chars: letters, digits, _ and -
            self._update('ebay', deletion_token=tok)
        return tok

    def summary(self) -> dict:
        data = self._load()
        out = {}
        for ch, fields in FIELDS.items():
            cur = data.get(ch) or {}
            vals = {}
            for key, label, is_secret in fields:
                v = cur.get(key) or ''
                vals[key] = {'label': label, 'secret': is_secret, 'set': bool(v),
                             'shown': ('••••' + v[-4:]) if (is_secret and v) else v}
            row = {'fields': vals, 'last_test': cur.get('last_test'), 'saved_at': cur.get('saved_at')}
            if ch == 'amazon':
                row['connected'] = bool(cur.get('refresh_token'))
                row['selling_partner_id'] = cur.get('selling_partner_id', '')
                row['login_url'] = self.amazon_login_url
                row['accept_url'] = self.amazon_redirect_url
            if ch == 'ebay':
                row['connected'] = bool(cur.get('refresh_token'))
                row['ebay_user'] = cur.get('ebay_user', '')
                row['refresh_expires_at'] = cur.get('refresh_expires_at')
                row['deletion_url'] = self.ebay_deletion_url
                row['deletion_token'] = self.ebay_deletion_token()
                row['accept_url'] = self.ebay_accept_url
                row['deletion_notices'] = self.deletion_notices
            out[ch] = row
        return out

    def save(self, channel: str, values: dict) -> dict:
        if channel not in FIELDS:
            raise ConnectionError_('Unknown connection')
        allowed = {k for k, _, _ in FIELDS[channel]}
        clean = {}
        for k, v in (values or {}).items():
            if k not in allowed or not isinstance(v, str):
                continue
            v = v.strip()
            if channel == 'gmail' and k == 'app_password':
                v = v.replace(' ', '')  # Google shows it as 4 groups of 4
            if v:  # a blank field keeps what is saved; Clear removes the whole connection
                clean[k] = v[:4000]
        if not clean:
            raise ConnectionError_('Nothing to save')
        clean['saved_at'] = _now()
        self._update(channel, **clean)
        return self.summary()[channel]

    def clear(self, channel: str) -> dict:
        if channel not in FIELDS:
            raise ConnectionError_('Unknown connection')
        keep = {}
        if channel == 'ebay':
            keep['deletion_token'] = self.get('ebay').get('deletion_token')  # eBay already holds it
        with self.lock:
            data = self._load()
            data[channel] = {k: v for k, v in keep.items() if v}
            self._save(data)
        return self.summary()[channel]

    # ------------------------------------------------------------------ tests (read-only, live)

    def test(self, channel: str) -> dict:
        fn = {'ebay': self._test_ebay, 'amazon': self._test_amazon, 'walmart': self._test_walmart,
              'gmail': self._test_gmail}.get(channel)
        if not fn:
            raise ConnectionError_('Unknown connection')
        try:
            ok, detail = fn(self.get(channel))
        except ConnectionError_ as e:
            ok, detail = False, str(e)
        result = {'ok': ok, 'detail': detail, 'at': _now()}
        self._update(channel, last_test=result)
        return result

    def _need(self, cur: dict, *keys):
        missing = [k for k in keys if not cur.get(k)]
        if missing:
            raise ConnectionError_('Missing: ' + ', '.join(missing))

    def _ebay_basic(self, cur: dict) -> str:
        return 'Basic ' + base64.b64encode(f"{cur['client_id']}:{cur['client_secret']}".encode()).decode()

    def ebay_user_token(self) -> str:
        """Fresh eBay user access token (2 hours) from the stored refresh token."""
        cur = self.get('ebay')
        self._need(cur, 'client_id', 'client_secret', 'refresh_token')
        st, p = _http('POST', f'{EBAY_API}/identity/v1/oauth2/token',
                      {'Authorization': self._ebay_basic(cur), 'Content-Type': 'application/x-www-form-urlencoded'},
                      {'grant_type': 'refresh_token', 'refresh_token': cur['refresh_token'], 'scope': ' '.join(EBAY_SCOPES)})
        if st != 200 or not isinstance(p, dict) or 'access_token' not in p:
            raise ConnectionError_(f'eBay refused the refresh token ({st}): {_err_text(p)}. Press Connect eBay again.')
        return p['access_token']

    def _test_ebay(self, cur: dict):
        self._need(cur, 'client_id', 'client_secret')
        st, p = _http('POST', f'{EBAY_API}/identity/v1/oauth2/token',
                      {'Authorization': self._ebay_basic(cur), 'Content-Type': 'application/x-www-form-urlencoded'},
                      {'grant_type': 'client_credentials', 'scope': EBAY_SCOPES[0]})
        if st != 200:
            return False, f'eBay refused the App ID / Cert ID ({st}): {_err_text(p)}'
        if not cur.get('refresh_token'):
            return True, 'App keys work. Next: press Connect eBay and approve with your seller account.'
        token = self.ebay_user_token()
        st, p = _http('GET', f'{EBAY_API}/sell/fulfillment/v1/order?limit=1',
                      {'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
        if st != 200:
            return False, f'Connected, but reading orders failed ({st}): {_err_text(p)}'
        return True, f"Connected. eBay reports {p.get('total', 0)} orders in the last 90 days."

    def _test_amazon(self, cur: dict):
        self._need(cur, 'lwa_client_id', 'lwa_client_secret', 'refresh_token')
        token = self.amazon_access_token(cur)
        st, p = _http('GET', f'{SPAPI_NA}/sellers/v1/marketplaceParticipations',
                      {'x-amz-access-token': token, 'Accept': 'application/json',
                       'User-Agent': 'Boxloft/1.0 (Language=Python)'})
        if st != 200:
            return False, f'Token works, but SP-API refused ({st}): {_err_text(p)}'
        names = [m.get('marketplace', {}).get('name', '?') for m in p.get('payload', [])
                 if m.get('participation', {}).get('isParticipating')]
        return True, 'Connected. Selling in: ' + (', '.join(names) or 'no marketplaces listed')

    def amazon_access_token(self, cur: dict = None) -> str:
        cur = cur or self.get('amazon')
        self._need(cur, 'lwa_client_id', 'lwa_client_secret', 'refresh_token')
        st, p = _http('POST', LWA_TOKEN, {'Content-Type': 'application/x-www-form-urlencoded'},
                      {'grant_type': 'refresh_token', 'refresh_token': cur['refresh_token'],
                       'client_id': cur['lwa_client_id'], 'client_secret': cur['lwa_client_secret']})
        if st != 200 or not isinstance(p, dict) or 'access_token' not in p:
            raise ConnectionError_(f'Amazon refused the LWA keys or refresh token ({st}): {_err_text(p)}')
        return p['access_token']

    def walmart_access_token(self, cur: dict = None) -> str:
        cur = cur or self.get('walmart')
        self._need(cur, 'client_id', 'client_secret')
        basic = base64.b64encode(f"{cur['client_id']}:{cur['client_secret']}".encode()).decode()
        st, p = _http('POST', f'{WALMART_API}/v3/token',
                      {'Authorization': f'Basic {basic}', 'Accept': 'application/json',
                       'Content-Type': 'application/x-www-form-urlencoded',
                       'WM_SVC.NAME': 'Walmart Marketplace', 'WM_QOS.CORRELATION_ID': str(uuid.uuid4())},
                      {'grant_type': 'client_credentials'})
        if st != 200 or not isinstance(p, dict) or 'access_token' not in p:
            raise ConnectionError_(f'Walmart refused the Client ID / Secret ({st}): {_err_text(p)}')
        return p['access_token']

    def _test_walmart(self, cur: dict):
        self.walmart_access_token(cur)
        return True, 'Connected. Walmart accepted the Client ID and Secret.'

    def _test_gmail(self, cur: dict):
        self._need(cur, 'email', 'app_password')
        try:
            m = imaplib.IMAP4_SSL(GMAIL_IMAP, timeout=20)
        except OSError as e:
            return False, f'Could not reach Gmail: {e}'
        try:
            try:
                m.login(cur['email'], cur['app_password'])
            except imaplib.IMAP4.error as e:
                return False, f'Gmail refused the sign-in: {str(e)[:150]}. Check the app password.'
            m.select('"[Gmail]/All Mail"', readonly=True)
            typ, d = m.search(None, 'X-GM-RAW', f'"{STORE_QUERY} newer_than:180d"')
            n = len(d[0].split()) if typ == 'OK' and d and d[0] else 0
            return True, f'Connected, read-only. {n} order emails from stores in the last 6 months.'
        finally:
            try:
                m.logout()
            except Exception:
                pass

    # ------------------------------------------------------------------ eBay consent + deletion notices

    # ------------------------------------------------------------------ Amazon website authorization (consent from Seller Central)

    @property
    def amazon_login_url(self) -> str:
        return f'{self.public_url}/api/amazon/login'

    @property
    def amazon_redirect_url(self) -> str:
        return f'{self.public_url}/api/amazon/callback'

    def amazon_consent_url(self) -> str:
        """Seller Central consent page for this app. version=beta lets a Draft (private) app be authorized."""
        cur = self.get('amazon')
        self._need(cur, 'app_id', 'lwa_client_id', 'lwa_client_secret')
        state = secrets.token_urlsafe(24)
        self._update('amazon', oauth_state={'value': state, 'at': time.time()})
        return AMAZON_CONSENT + '?' + urllib.parse.urlencode({'application_id': cur['app_id'], 'state': state, 'version': 'beta'})

    def amazon_login(self, callback_uri: str, amazon_state: str) -> str:
        """Step 2: Amazon sends the seller to our Login URI; we send them straight back with our redirect URI."""
        u = urllib.parse.urlparse(callback_uri or '')
        if u.scheme != 'https' or u.hostname not in AMAZON_CALLBACK_HOSTS:
            raise ConnectionError_('That sign-in did not come from Seller Central')
        saved = (self.get('amazon').get('oauth_state') or {}).get('value', '')
        if not saved:
            raise ConnectionError_('Press Connect Amazon in Boxloft first')
        # No redirect_uri: Amazon then uses the app's first registered one. Sending ours (an exact match)
        # got MD5101 "Invalid redirect URL" on 2026-11-10, so we let Amazon pick it.
        q = urllib.parse.urlencode({'amazon_state': amazon_state or '', 'state': saved, 'version': 'beta'})
        return f'{callback_uri}{"&" if u.query else "?"}{q}'

    def amazon_callback(self, code: str, state: str, selling_partner_id: str) -> str:
        cur = self.get('amazon')
        saved = cur.get('oauth_state') or {}
        self._update('amazon', oauth_state=None)  # one use
        if not code:
            raise ConnectionError_('Amazon did not send a code (consent declined?)')
        if not state or not secrets.compare_digest(str(saved.get('value', '')), state) \
                or time.time() - float(saved.get('at', 0)) > 900:
            raise ConnectionError_('This Amazon sign-in link expired. Press Connect Amazon again.')
        st, p = _http('POST', LWA_TOKEN, {'Content-Type': 'application/x-www-form-urlencoded'},
                      {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.amazon_redirect_url,
                       'client_id': cur['lwa_client_id'], 'client_secret': cur['lwa_client_secret']})
        if st != 200 or not isinstance(p, dict) or 'refresh_token' not in p:
            raise ConnectionError_(f'Amazon refused the code ({st}): {_err_text(p)}')
        self._update('amazon', refresh_token=p['refresh_token'], selling_partner_id=(selling_partner_id or '')[:40],
                     saved_at=_now())
        return selling_partner_id or ''

    def ebay_consent_url(self) -> str:
        cur = self.get('ebay')
        self._need(cur, 'client_id', 'client_secret', 'ru_name')
        state = secrets.token_urlsafe(24)
        self._update('ebay', oauth_state={'value': state, 'at': time.time()})
        q = urllib.parse.urlencode({'client_id': cur['client_id'], 'response_type': 'code',
                                    'redirect_uri': cur['ru_name'], 'scope': ' '.join(EBAY_SCOPES),
                                    'state': state, 'prompt': 'login'})
        return f'{EBAY_AUTH}?{q}'

    def ebay_callback(self, code: str, state: str) -> str:
        cur = self.get('ebay')
        saved = cur.get('oauth_state') or {}
        self._update('ebay', oauth_state=None)  # one use
        if not code:
            raise ConnectionError_('eBay did not send a code (consent declined?)')
        if not state or not secrets.compare_digest(str(saved.get('value', '')), state) \
                or time.time() - float(saved.get('at', 0)) > 900:
            raise ConnectionError_('This eBay sign-in link expired. Press Connect eBay again.')
        st, p = _http('POST', f'{EBAY_API}/identity/v1/oauth2/token',
                      {'Authorization': self._ebay_basic(cur), 'Content-Type': 'application/x-www-form-urlencoded'},
                      {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': cur['ru_name']})
        if st != 200 or not isinstance(p, dict) or 'refresh_token' not in p:
            raise ConnectionError_(f'eBay refused the code ({st}): {_err_text(p)}')
        expires = datetime.fromtimestamp(time.time() + int(p.get('refresh_token_expires_in', 0)), timezone.utc)
        user = ''
        st2, me = _http('GET', 'https://apiz.ebay.com/commerce/identity/v1/user/',
                        {'Authorization': f"Bearer {p['access_token']}", 'Accept': 'application/json'})
        if st2 == 200 and isinstance(me, dict):
            user = me.get('username', '')
        self._update('ebay', refresh_token=p['refresh_token'], ebay_user=user,
                     refresh_expires_at=expires.strftime('%Y-%m-%dT%H:%M:%SZ'), saved_at=_now())
        return user

    def ebay_challenge(self, challenge_code: str) -> str:
        """eBay's endpoint check: sha256(challengeCode + verificationToken + endpoint), hex."""
        h = hashlib.sha256()
        h.update(challenge_code.encode())
        h.update(self.ebay_deletion_token().encode())
        h.update(self.ebay_deletion_url.encode())
        return h.hexdigest()

    def ebay_deletion_notice(self, payload: dict):
        """eBay asks every app to forget a closed eBay account. Boxloft stores no buyer
        usernames or ids from eBay (orders keep only the eBay order number and item lines),
        so there is nothing to erase; we acknowledge and count."""
        self.deletion_notices += 1
