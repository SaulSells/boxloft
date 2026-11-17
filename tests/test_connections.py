import hashlib
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from connections import Connections, ConnectionError_  # noqa: E402


def test_secrets_masked_and_file_private(tmp_path):
    c = Connections(str(tmp_path))
    c.save('walmart', {'client_id': 'abc-123', 'client_secret': 'supersecretvalue9876', 'junk': 'x'})
    row = c.summary()['walmart']
    assert row['fields']['client_id']['shown'] == 'abc-123'
    assert row['fields']['client_secret']['shown'] == '••••9876'
    assert 'supersecret' not in json.dumps(c.summary())
    mode = stat.S_IMODE(os.stat(os.path.join(tmp_path, 'secrets', 'connections.json')).st_mode)
    assert mode == 0o600
    assert 'junk' not in c.get('walmart')


def test_blank_keeps_saved_and_server_keys_refused(tmp_path):
    c = Connections(str(tmp_path))
    c.save('ebay', {'client_id': 'app', 'client_secret': 'cert1234'})
    c.save('ebay', {'client_id': '', 'client_secret': '', 'ru_name': 'Alex-Boxloft-abc', 'refresh_token': 'forged'})
    cur = c.get('ebay')
    assert cur['client_secret'] == 'cert1234' and cur['ru_name'] == 'Alex-Boxloft-abc'
    assert 'refresh_token' not in cur  # only eBay's consent callback sets it
    with pytest.raises(ConnectionError_):
        c.save('ebay', {'client_id': '   '})


def test_gmail_app_password_spaces_removed(tmp_path):
    c = Connections(str(tmp_path))
    c.save('gmail', {'email': 'x@example.com', 'app_password': 'abcd efgh ijkl mnop'})
    assert c.get('gmail')['app_password'] == 'abcdefghijklmnop'


def test_ebay_challenge_matches_spec(tmp_path):
    c = Connections(str(tmp_path), 'http://localhost:5101')
    tok = c.ebay_deletion_token()
    assert 32 <= len(tok) <= 80 and all(ch.isalnum() or ch in '_-' for ch in tok)
    assert c.ebay_deletion_token() == tok  # stable once made
    want = hashlib.sha256(('123' + tok + 'http://localhost:5101/api/ebay/account-deletion').encode()).hexdigest()
    assert c.ebay_challenge('123') == want
    c.clear('ebay')
    assert c.ebay_deletion_token() == tok  # eBay holds it, so Clear keeps it


def test_ebay_callback_rejects_bad_state(tmp_path):
    c = Connections(str(tmp_path))
    c.save('ebay', {'client_id': 'a', 'client_secret': 'b', 'ru_name': 'r'})
    url = c.ebay_consent_url()
    assert 'auth.ebay.com' in url and 'sell.fulfillment.readonly' in url
    with pytest.raises(ConnectionError_):
        c.ebay_callback('code', 'wrong-state')
    with pytest.raises(ConnectionError_):  # state is one use, even after a failure
        c.ebay_callback('code', url.split('state=')[1].split('&')[0])


def test_amazon_website_authorization(tmp_path):
    c = Connections(str(tmp_path), 'http://localhost:5101')
    c.save('amazon', {'app_id': 'amzn1.sp.solution.abc', 'lwa_client_id': 'cid', 'lwa_client_secret': 'secret'})
    url = c.amazon_consent_url()
    assert url.startswith('https://sellercentral.amazon.com/apps/authorize/consent?') and 'version=beta' in url
    state = url.split('state=')[1].split('&')[0]
    with pytest.raises(ConnectionError_):  # never bounce to a host that is not Seller Central
        c.amazon_login('https://evil.example.com/cb', 'x')
    back = c.amazon_login('https://sellercentral.amazon.com/apps/authorize/confirm/amzn1.sp.solution.abc', 'AMZSTATE')
    assert back.startswith('https://sellercentral.amazon.com/apps/authorize/confirm/')
    assert 'redirect_uri' not in back and 'amazon_state=AMZSTATE' in back and 'version=beta' in back
    assert f'state={state}' in back
    with pytest.raises(ConnectionError_):
        c.amazon_callback('code', 'wrong', 'A1SELLER')
    with pytest.raises(ConnectionError_):  # state was used up by the failed try
        c.amazon_callback('code', state, 'A1SELLER')
    assert c.summary()['amazon']['login_url'] == 'http://localhost:5101/api/amazon/login'


def test_test_reports_missing_keys(tmp_path):
    c = Connections(str(tmp_path))
    r = c.test('amazon')
    assert r['ok'] is False and 'Missing' in r['detail']
    assert c.summary()['amazon']['last_test']['ok'] is False


def test_server_webhook_without_header(tmp_path, monkeypatch):
    """eBay posts deletion notices without our X-Boxloft header; other POSTs still need it."""
    monkeypatch.setenv('BOXLOFT_DATA', str(tmp_path))
    import importlib
    import server
    importlib.reload(server)
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    try:
        body = json.dumps({'metadata': {'topic': 'MARKETPLACE_ACCOUNT_DELETION'},
                           'notification': {'data': {'username': 'u', 'userId': 'x'}}}).encode()
        r = urllib.request.urlopen(urllib.request.Request(base + '/api/ebay/account-deletion', data=body, method='POST',
                                                          headers={'Content-Type': 'application/json'}))
        assert r.status == 204
        r = urllib.request.urlopen(base + '/api/ebay/account-deletion?challenge_code=abc')
        got = json.loads(r.read())['challengeResponse']
        assert got == server.connections.ebay_challenge('abc')
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + '/api/connections/ebay/test', data=b'{}', method='POST'))
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + '/api/connections')
        assert e.value.code == 401
    finally:
        httpd.shutdown()
