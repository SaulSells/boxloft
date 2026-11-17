"""Fetch a URL a person (or a marketplace) gave us, only from the public internet.

A signed-in user can type any photo link on a product (POST /api/products image_url). Without this guard the
server would fetch http://169.254.169.254/ (the cloud metadata service), 127.0.0.1:5101 or a router on the LAN
for them (SSRF, black-hat review 11-17). The check runs on the address the socket really connected to, before
TLS or any request byte goes out, so a name that resolves to a private address (or changes its answer between a
check and the connect) is refused too. Redirects are followed only to https and pass the same check.
"""
import http.client
import ipaddress
import urllib.error
import urllib.request


class BlockedAddress(OSError):
    pass


def is_public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split('%', 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        http.client.HTTPConnection.connect(self)   # plain TCP connect (and proxy tunnel, which we never set)
        peer = self.sock.getpeername()[0]
        if not is_public_ip(peer):
            self.sock.close()
            self.sock = None
            raise BlockedAddress(f'{self.host} is not a public address')
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith('https://'):
            raise urllib.error.URLError('redirect away from https refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    # ProxyHandler({}) = no proxy from the environment: the peer we check must be the real host.
    # The plain HTTPHandler stays out, so an http:// URL (or redirect) has nothing to open it.
    o = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler({}), _PublicHTTPSHandler(), _HttpsOnlyRedirects(),
              urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        o.add_handler(h)
    return o


def fetch_public(url: str, limit: int, timeout: float = 20) -> bytes:
    """The first `limit` bytes of an https URL on a public address. Raises urllib.error.URLError otherwise."""
    if not (url or '').lower().startswith('https://'):
        raise urllib.error.URLError('only https links are fetched')
    with _opener().open(url, timeout=timeout) as r:
        return r.read(limit)
