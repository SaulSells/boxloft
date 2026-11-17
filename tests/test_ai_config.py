"""ai.py (which AI service) and config.py (business settings). No network: every model call is a fake."""
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import ai  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_cover_all import srv  # noqa: E402,F401  (fixture)

KEYS = ('BOXLOFT_AI_PROVIDER', 'BOXLOFT_PHOTO_PROVIDER', 'BOXLOFT_AI_MODEL', 'TYPESAFE_API_KEY',
        'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY')


@pytest.fixture
def clean(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_provider_follows_the_keys_typesafe_first_for_emails_gemini_first_for_photos(clean):
    assert ai.judge_provider() == ('', '') and ai.photo_provider() == ('', '')
    clean.setenv('ANTHROPIC_API_KEY', 'a')
    assert ai.judge_provider() == ('anthropic', 'a') and ai.photo_provider() == ('anthropic', 'a')
    clean.setenv('TYPESAFE_API_KEY', 't')
    clean.setenv('GEMINI_API_KEY', 'g')
    assert ai.judge_provider() == ('typesafe', 't') and ai.photo_provider() == ('gemini', 'g')   # live today
    clean.setenv('BOXLOFT_AI_PROVIDER', 'anthropic')
    assert ai.judge_provider() == ('anthropic', 'a')
    clean.setenv('BOXLOFT_AI_PROVIDER', 'nope')
    with pytest.raises(ai.AiError):
        ai.judge_provider()


def test_no_key_store_mail_says_so(clean):
    import store_mail
    with pytest.raises(store_mail.StoreMailError, match='No AI key'):
        store_mail.default_judge()
    clean.setenv('TYPESAFE_API_KEY', 't')
    assert isinstance(store_mail.default_judge(), store_mail.Jev)
    clean.setenv('BOXLOFT_AI_PROVIDER', 'anthropic')
    clean.setenv('ANTHROPIC_API_KEY', 'a')
    assert isinstance(store_mail.default_judge(), ai.LlmJudge)


def test_llm_judge_keeps_only_answers_of_the_asked_shape(clean):
    qs = {'order_number': {'type': 'choice', 'instructions': 'x', 'criteria': {'A1': None, 'B2': None, 'none of these': ''}},
          'total': {'type': 'choice', 'instructions': 'x', 'criteria': {'$5.00': None}},
          'is_order': {'type': 'noul', 'instructions': 'x'},
          'item_3': {'type': 'noul', 'instructions': 'x'}}
    seen = {}

    def send(key, prompt, max_tokens=16000):
        seen['prompt'] = prompt
        return ('```json\n' + json.dumps({'order_number': {'choice': 'B2', 'confidence': 2},
                                         'total': {'choice': '$9.99'},            # not an option: dropped
                                         'is_order': {'noul': 0.97},
                                         'item_3': {'noul': 'yes'}}) + '\n```', 100, 20)  # not a number: dropped

    j = ai.LlmJudge('anthropic', 'k', send=send)
    a = j.ask({'L0': 'Order B2'}, qs)
    assert a == {'order_number': {'choice': 'B2', 'confidence': 1.0}, 'is_order': {'noul': 0.97}}
    assert j.usage == {'requests': 1, 'input_tokens': 100, 'output_tokens': 20}
    assert '"L0": "Order B2"' in seen['prompt']
    with pytest.raises(ai.AiError):
        ai.LlmJudge('anthropic', 'k', send=lambda *a: ('no json here', 1, 1)).ask({}, qs)


def test_llm_judge_runs_the_real_extract(clean):
    """store_mail.extract on a chat model's answers: the order number and total can only be values in the email."""
    import store_mail
    text = 'Thanks for your order\nOrder #W123456\nLEGO Alpine Lodge 10325\n$99.99\nTotal: $106.24\nVisa ending in 1111'

    def send(key, prompt, max_tokens=16000):
        q = json.loads(prompt.split('\n\n', 1)[1])['questions']
        out = {}
        for name, x in q.items():
            if x['type'] == 'choice':
                opts = list(x['criteria'])
                want = {'order_number': 'W123456', 'total': '$106.24'}.get(name)
                out[name] = {'choice': want if want in opts else opts[0], 'confidence': 0.9}
            else:
                out[name] = {'noul': 0.9 if name == 'is_order' or 'LEGO' in json.dumps(x) else 0.1}
        return json.dumps(out), 10, 10

    r = store_mail.extract(text, ai.LlmJudge('anthropic', 'k', send=send))
    assert r['is_order'] and r['order_number'] == 'W123456'


def test_chat_photo_read_shapes_like_gemini(clean, tmp_path):
    import photo_id
    calls = []

    def send(key, content, max_tokens=16000):
        calls.append(content)
        return json.dumps({'is_product': True, 'title': 'NECA Predator', 'brand': 'NECA', 'model': '',
                           'barcode_digits': '6-34482-51540-1', 'confidence': 'high'}), 50, 9

    r = ai.chat_photo_read('anthropic', 'k', b'\xff\xd8jpeg', photo_id.PROMPT, send=send)
    assert r['data']['title'] == 'NECA Predator' and calls[0][0]['type'] == 'image'
    r = ai.chat_photo_read('openai', 'k', b'\xff\xd8jpeg', photo_id.PROMPT, send=send)
    assert calls[1][0]['image_url']['url'].startswith('data:image/jpeg;base64,')


def test_config_defaults_are_this_warehouse_and_env_overrides(clean):
    import config
    for k in ('BOXLOFT_APP_NAME', 'BOXLOFT_SITE_NAME', 'BOXLOFT_PLACE', 'BOXLOFT_TZ', 'BOXLOFT_TZ_LABEL'):
        clean.delenv(k, raising=False)
    c = importlib.reload(config)
    assert c.client() == {'name': 'Boxloft', 'site': 'Main warehouse', 'place': '',
                          'tz': 'America/New_York', 'tzLabel': 'ET'}
    clean.setenv('BOXLOFT_SITE_NAME', 'Main warehouse')
    clean.setenv('BOXLOFT_PLACE', '')
    clean.setenv('BOXLOFT_TZ', 'America/Los_Angeles')
    clean.setenv('BOXLOFT_APP_NAME', '</script><b>')
    c = importlib.reload(config)
    assert (c.SITE_NAME, c.PLACE, c.TZ_LABEL) == ('Main warehouse', '', 'PT')
    assert b'</script><b>' not in c.client_script()[:-9]   # a name can't close the script tag early
    for k in ('BOXLOFT_APP_NAME', 'BOXLOFT_SITE_NAME', 'BOXLOFT_PLACE', 'BOXLOFT_TZ'):
        clean.delenv(k)
    importlib.reload(config)


def test_index_html_carries_the_settings(srv, tmp_path, monkeypatch):  # noqa: F811
    server, _ = srv
    d = tmp_path / 'dist'
    d.mkdir()
    (d / 'index.html').write_text('<!doctype html><html><head><title>x</title></head><body></body></html>')
    monkeypatch.setattr(server, 'STATIC', str(d))
    sent = {}

    class H:
        def send_bytes(self, body, ctype, cache='', extra=None):
            sent['body'] = body

    server.Handler.serve_static(H(), '/orders')
    assert b'<head><script>window.__APP__={"name": "Boxloft", "site": "Main warehouse"' in sent['body']
