"""Which AI service reads store emails and product photos.

Two jobs use AI, and both keep the AI on a short leash:
- Store emails (store_mail.py): code finds the candidate values first (order numbers, amounts, lines that could be
  items). The AI only picks among them or says yes/no, so it can't invent an order number or a price.
- Photo ID (photo_id.py): one photo of a box -> name, brand, model, barcode digits. Barcode digits must pass their
  check digit before anything uses them.

Pick the service with BOXLOFT_AI_PROVIDER, or leave it empty to take the first key that is set:

    provider    key variable        store emails   photos   model (BOXLOFT_AI_MODEL overrides)
    typesafe    TYPESAFE_API_KEY    yes            no       jev-latest
    anthropic   ANTHROPIC_API_KEY   yes            yes      claude-opus-5-5 (needs `pip install anthropic`)
    openai      OPENAI_API_KEY      yes            yes      set BOXLOFT_AI_MODEL (no default)
    gemini      GEMINI_API_KEY      no             yes      gemini-3.8-flash (GEMINI_MODEL)

Photos use BOXLOFT_PHOTO_PROVIDER the same way (empty: gemini, then anthropic, then openai, by key).
With no key at all, store emails are not read and New product takes a typed name; the rest of the app works.

Every judge has the same face as TypeSafe's Jev: ask(state, questions) -> answers, where a question is
{'type': 'choice', 'instructions', 'criteria': {option: description}} -> {'choice': option, 'confidence': 0..1}
or {'type': 'noul', 'instructions'} -> {'noul': chance of yes, 0..1}. Tests use a fake judge, never the network."""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request


class AiError(Exception):
    pass


def _env(name: str) -> str:
    return (os.environ.get(name) or '').strip()


JUDGE_KEYS = (('typesafe', 'TYPESAFE_API_KEY'), ('anthropic', 'ANTHROPIC_API_KEY'), ('openai', 'OPENAI_API_KEY'))
PHOTO_KEYS = (('gemini', 'GEMINI_API_KEY'), ('anthropic', 'ANTHROPIC_API_KEY'), ('openai', 'OPENAI_API_KEY'))
KEY_OF = dict(JUDGE_KEYS + PHOTO_KEYS)


def _pick(choice_var: str, table) -> tuple:
    want = _env(choice_var).lower()
    if want:
        if want not in dict(table):
            raise AiError(f'{choice_var}={want} is not one of {", ".join(p for p, _ in table)}')
        return want, _env(dict(table)[want])
    for prov, var in table:
        if _env(var):
            return prov, _env(var)
    return '', ''


def judge_provider() -> tuple:
    """(provider, key) for store emails; ('', '') when none is set up."""
    return _pick('BOXLOFT_AI_PROVIDER', JUDGE_KEYS)


def photo_provider() -> tuple:
    return _pick('BOXLOFT_PHOTO_PROVIDER', PHOTO_KEYS)


def make_judge():
    """The judge store_mail asks. Raises AiError when no service is set up."""
    prov, key = judge_provider()
    if not key:
        raise AiError('No AI key for reading store emails (set ANTHROPIC_API_KEY, OPENAI_API_KEY or TYPESAFE_API_KEY)')
    if prov == 'typesafe':
        from store_mail import Jev
        return Jev(key)
    return LlmJudge(prov, key)


# ------------------------------------------------------------------ chat models

JUDGE_SYSTEM = (
    'You read data from an online store email and answer questions about it. You get JSON with "state" (the email, '
    'numbered lines L0, L1, ...) and "questions". Answer every question. Reply with one JSON object and nothing else: '
    'one key per question name. For a question of type "choice", the value is {"choice": <one key of its criteria, '
    'copied exactly>, "confidence": <0 to 1>}. For a question of type "noul" (a yes/no question), the value is '
    '{"noul": <the chance the answer is yes, 0 to 1>}. Use only what the email says.')


def anthropic_model() -> str:
    return _env('BOXLOFT_AI_MODEL') or 'claude-opus-5-5'


def _anthropic_text(key: str, content, max_tokens: int = 16000) -> tuple:
    """One Messages API call -> (text, input_tokens, output_tokens)."""
    try:
        import anthropic
    except ImportError:
        raise AiError('The anthropic package is missing: pip install anthropic')
    client = anthropic.Anthropic(api_key=key, timeout=120, max_retries=3)
    try:
        r = client.messages.create(model=anthropic_model(), max_tokens=max_tokens, output_config={'effort': 'low'},
                                   messages=[{'role': 'user', 'content': content}])
    except anthropic.APIStatusError as e:
        raise AiError(f'Anthropic {e.status_code}: {str(e)[:300]}')
    except anthropic.APIConnectionError as e:
        raise AiError(f'Anthropic unreachable: {e}')
    if r.stop_reason == 'refusal':
        raise AiError('Anthropic declined this request')
    text = ''.join(b.text for b in r.content if b.type == 'text')
    return text, r.usage.input_tokens, r.usage.output_tokens


def _openai_text(key: str, content, max_tokens: int = 16000) -> tuple:
    model = _env('BOXLOFT_AI_MODEL')
    if not model:
        raise AiError('Set BOXLOFT_AI_MODEL to the OpenAI model to use')
    base = _env('OPENAI_BASE_URL') or 'https://api.openai.com/v1'
    body = json.dumps({'model': model, 'max_completion_tokens': max_tokens, 'response_format': {'type': 'json_object'},
                       'messages': [{'role': 'user', 'content': content}]}).encode()
    for attempt in range(4):
        req = urllib.request.Request(f'{base}/chat/completions', data=body, method='POST', headers={
            'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                p = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise AiError(f'OpenAI {e.code}: {e.read()[:300].decode("utf-8", "replace")}')
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise AiError(f'OpenAI unreachable: {e}')
    u = p.get('usage') or {}
    try:
        text = p['choices'][0]['message']['content'] or ''
    except (KeyError, IndexError):
        raise AiError('OpenAI gave no answer')
    return text, int(u.get('prompt_tokens') or 0), int(u.get('completion_tokens') or 0)


def json_object(text: str) -> dict:
    """The JSON object in a model's reply (it may wrap it in a code fence)."""
    m = re.search(r'\{.*\}', text or '', re.S)
    if not m:
        raise AiError('The AI answer had no JSON')
    try:
        out = json.loads(m.group(0))
    except ValueError:
        raise AiError('The AI answer was not valid JSON')
    if not isinstance(out, dict):
        raise AiError('The AI answer was not a JSON object')
    return out


def clean_answers(raw: dict, questions: dict) -> dict:
    """Keep only answers of the asked shape: a choice must be one of its options, a chance must be 0..1."""
    out = {}
    for name, q in questions.items():
        a = raw.get(name)
        if not isinstance(a, dict):
            continue
        if q.get('type') == 'choice':
            opts = list((q.get('criteria') or {}).keys())
            c = a.get('choice')
            if c in opts:
                try:
                    conf = min(1.0, max(0.0, float(a.get('confidence', 0.5))))
                except (TypeError, ValueError):
                    conf = 0.5
                out[name] = {'choice': c, 'confidence': conf}
        else:
            try:
                out[name] = {'noul': min(1.0, max(0.0, float(a.get('noul'))))}
            except (TypeError, ValueError):
                pass
    return out


class LlmJudge:
    """Jev's ask() on a chat model: the same questions, answered as JSON and checked."""

    def __init__(self, provider: str, key: str, send=None):
        self.provider, self.key = provider, key
        self.model = anthropic_model() if provider == 'anthropic' else (_env('BOXLOFT_AI_MODEL') or 'openai')
        self.send = send or (_anthropic_text if provider == 'anthropic' else _openai_text)
        self.usage = {'requests': 0, 'input_tokens': 0, 'output_tokens': 0}

    def ask(self, state, questions: dict) -> dict:
        prompt = JUDGE_SYSTEM + '\n\n' + json.dumps({'state': state, 'questions': questions}, ensure_ascii=False)
        text, tin, tout = self.send(self.key, prompt)
        self.usage['requests'] += 1
        self.usage['input_tokens'] += tin
        self.usage['output_tokens'] += tout
        return clean_answers(json_object(text), questions)


def chat_photo_read(provider: str, key: str, jpeg: bytes, prompt: str, send=None) -> dict:
    """Photo ID on Anthropic or OpenAI: same fields as photo_id.gemini_read's model answer."""
    b64 = base64.b64encode(jpeg).decode()
    ask = prompt + (' Reply with one JSON object and nothing else, with keys is_product (true/false), title, brand, '
                    'model, barcode_digits and confidence ("high", "medium" or "low").')
    if provider == 'anthropic':
        content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': b64}},
                   {'type': 'text', 'text': ask}]
        send = send or _anthropic_text
    else:
        content = [{'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
                   {'type': 'text', 'text': ask}]
        send = send or _openai_text
    t0 = time.time()
    text, tin, tout = send(key, content, 2000)
    data = json_object(text)
    return {'data': data, 'seconds': round(time.time() - t0, 1), 'tokens_in': tin, 'tokens_out': tout}
