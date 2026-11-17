"""Store-email item links (Alex 11-13: "grab the link wherever possible").

Shapes from live emails 11-13: Woot links straight to its offer page; Mattel and Stanley go through click trackers
(trk-creations.mattel.com/ls/click, cta.narvar.com) that redirect to /products/...; Mattel's landing URL carries a
Klaviyo `_kx` token and utm tags, which never get saved. No network here: the redirects are a fake.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from inbound import store_url  # noqa: E402
from store_mail import clean_link, item_links, resolve_link  # noqa: E402

MATTEL = """<table><tr><td><a href="https://trk-creations.mattel.com/ls/click?upn=AAA111">
  <img src="https://cdn.shopify.com/s/files/stego.jpg" alt="Image of Jurassic World Hammond Collection Stegosaurus" width="120"></a></td>
  <td><a href="https://trk-creations.mattel.com/ls/click?upn=BBB222">Jurassic World Hammond Collection Stegosaurus</a></td></tr>
  <tr><td><a href="https://trk-creations.mattel.com/ls/click?upn=CCC333">Shop more</a></td></tr></table>"""
WOOT = """<p><a href="https://electronics.woot.com/offers/sony-mdr-zx770ap-wired-over-ear-headphones?ref=w_em_od_1&utm_source=x">
  Sony MDR-ZX770AP Wired Over-Ear Headphones</a></p><p><a href="https://www.woot.com/">Woot!</a></p>"""

HOPS = {
    'https://trk-creations.mattel.com/ls/click?upn=AAA111':
        'https://prod-collectorshub.myshopify.com/products/jurassic-world-hammond-collection-stegosaurus-jch08?_kx=TOKEN&utm_source=klaviyo',
    'https://prod-collectorshub.myshopify.com/products/jurassic-world-hammond-collection-stegosaurus-jch08?_kx=TOKEN&utm_source=klaviyo':
        'https://creations.mattel.com/products/jurassic-world-hammond-collection-stegosaurus-jch08?_kx=TOKEN&utm_source=klaviyo',
    'https://cta.narvar.com/f/a/xyz~~/AAG0DBA~/abc':
        'https://stanley-pmi.myshopify.com/products/classic-easy-pour-growler-64-oz-last-call-1',
    'https://stanley-pmi.myshopify.com/products/classic-easy-pour-growler-64-oz-last-call-1':
        'https://www.stanley1913.com/products/classic-easy-pour-growler-64-oz-last-call-1',
}
calls = []


def fake(url):
    calls.append(url)
    return HOPS.get(url)


def test_the_link_on_the_items_photo_or_name_is_picked():
    names = ['Jurassic World Hammond Collection Lost World Jurassic Park Stegosaurus Action Figure']
    assert item_links(MATTEL, names, ['https://cdn.shopify.com/s/files/stego.jpg']) == ['https://trk-creations.mattel.com/ls/click?upn=AAA111']
    by_name = item_links(MATTEL, ['Jurassic World Hammond Collection Stegosaurus'], [None])[0]   # photo alt or name text
    assert by_name in ('https://trk-creations.mattel.com/ls/click?upn=AAA111', 'https://trk-creations.mattel.com/ls/click?upn=BBB222')
    assert item_links(MATTEL, ['Monster High Skullector Lost Boys Doll'], [None]) == ['']        # "Shop more" is no item
    assert item_links(WOOT, ['Sony MDR-ZX770AP Wired Over-Ear Headphones'])[0].startswith('https://electronics.woot.com/offers/')


def test_trackers_are_followed_and_tokens_never_saved():
    calls.clear()
    got = resolve_link('https://trk-creations.mattel.com/ls/click?upn=AAA111', follow=fake)
    assert got == 'https://creations.mattel.com/products/jurassic-world-hammond-collection-stegosaurus-jch08'
    assert all('creations.mattel.com/products' not in c or 'myshopify' in c for c in calls)   # the product page is never fetched
    assert resolve_link('https://cta.narvar.com/f/a/xyz~~/AAG0DBA~/abc', follow=fake) == \
        'https://www.stanley1913.com/products/classic-easy-pour-growler-64-oz-last-call-1'
    woot = 'https://electronics.woot.com/offers/sony-mdr-zx770ap-wired-over-ear-headphones?ref=w_em_od_1&utm_source=x'
    n = len(calls)
    assert resolve_link(woot, follow=fake) == 'https://electronics.woot.com/offers/sony-mdr-zx770ap-wired-over-ear-headphones'
    assert len(calls) == n                                                                     # a direct link costs no request
    assert resolve_link('https://click.example.com/dead', follow=fake) == ''                   # a tracker that leads nowhere


def test_clean_link_keeps_only_what_finds_the_item():
    # Bookmanager (live 11-13): the home page plus a book key; every key is needed to open the book.
    assert clean_link('https://invitationbookshop.com/?q=h.tviewer&using_sb=status&qsb=keyword&qse=BQmk5nUNoKF3&utm_source=em&e=a%40b.c') == \
        'https://invitationbookshop.com/?q=h.tviewer&using_sb=status&qsb=keyword&qse=BQmk5nUNoKF3'
    assert clean_link('https://www.woot.com/') == ''
    assert clean_link('https://shop.example.com/products/x?variant=123&_kx=abc#top') == 'https://shop.example.com/products/x?variant=123'
    assert clean_link('javascript:alert(1)') == ''


def test_arriving_uses_the_saved_link_after_amazon_and_walmart_ids():
    saved = 'https://www.stanley1913.com/products/classic-easy-pour-growler-64-oz-last-call-1'
    assert store_url('stanley1913', '', '', saved) == saved
    assert store_url('amazon', 'B0XCFZA6HE', '', 'https://other.example.com/x') == 'https://www.amazon.com/dp/B0XCFZA6HE'
    assert store_url('woot.com', '', '', 'http://insecure.example.com/x') == ''
