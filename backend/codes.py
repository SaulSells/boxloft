"""Scan codes for things Boxloft prints: boxes, locations and one-off units.

A printed code is a two-letter type prefix, a 7-digit id and a Luhn check digit:

    SB00001234   box 123      (shown to people as BOX-000123-4 style text)
    SL00000427   location 42
    SU00000917   unit 91

The prefix stops a box scan from being read as a shelf, and none of them can be
mistaken for a UPC (all digits), an ASIN or an FNSKU (10 characters, B0/X0).
The check digit catches misreads and typing slips. Anything that does not parse
as one of ours is a product barcode (UPC, EAN, FNSKU, ASIN or SKU).
"""

import re

PREFIXES = {'SB': 'box', 'SL': 'location', 'SU': 'unit'}
KIND_PREFIX = {v: k for k, v in PREFIXES.items()}
ID_DIGITS = 7

_CODE_RE = re.compile(r'^(SB|SL|SU)-?(\d{%d})-?(\d)$' % ID_DIGITS)


def luhn_digit(digits: str) -> int:
    """Luhn check digit for a string of digits."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def make_code(kind: str, entity_id: int) -> str:
    """Barcode content for an entity, e.g. make_code('box', 123) -> 'SB00001234'."""
    if entity_id <= 0 or entity_id >= 10 ** ID_DIGITS:
        raise ValueError(f'id out of range: {entity_id}')
    body = str(entity_id).zfill(ID_DIGITS)
    return f'{KIND_PREFIX[kind]}{body}{luhn_digit(body)}'


def pretty_code(code: str) -> str:
    """Human text under the barcode: SB00001234 -> SB-0000123-4."""
    return f'{code[:2]}-{code[2:-1]}-{code[-1]}'


def parse_scan(raw: str) -> dict:
    """Classify a scan.

    Returns {'kind': 'box'|'location'|'unit', 'id': int, 'code': str} for our codes,
    {'kind': 'bad_check', 'code': str} when a code of ours fails its check digit,
    and {'kind': 'product', 'value': str} for anything else.
    """
    text = (raw or '').strip().upper()
    m = _CODE_RE.match(text)
    if not m:
        return {'kind': 'product', 'value': (raw or '').strip()}
    prefix, body, check = m.groups()
    if luhn_digit(body) != int(check):
        return {'kind': 'bad_check', 'code': text}
    return {'kind': PREFIXES[prefix], 'id': int(body), 'code': f'{prefix}{body}{check}'}
