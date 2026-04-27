"""Pitch deck generation — Claude structures the deck JSON, then generates one SVG per slide in parallel.

Exposed as a single tool so the Strands `pitch` agent can invoke it from the Caixa Postal chat.
"""
import concurrent.futures as cf
import json
import logging
import os
import re
import threading
import uuid
from typing import Any

import boto3
from botocore.config import Config
from strands import tool

from store import put_item, _table, _json_safe  # reuse DynamoDB wrapper

logger = logging.getLogger(__name__)

# P0.7: global semaphore caps the parallel slide-level Bedrock calls across ALL
# concurrent pitch_deck requests. Bedrock on-demand default is tight (~10 RPS);
# with 5 simultaneous pitch requests each fan-out to 6 slides = 30 inflight, which
# throttles. Cap at MAX_PARALLEL_SLIDES total across the worker.
_PITCH_MAX_PARALLEL = int(os.environ.get("CAIXA_PITCH_MAX_PARALLEL", "4"))
_PITCH_GATE = threading.BoundedSemaphore(_PITCH_MAX_PARALLEL)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)

_cfg = Config(retries={"max_attempts": 3, "mode": "adaptive"}, read_timeout=120)
_br = boto3.client("bedrock-runtime", region_name=REGION, config=_cfg)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 3:
            body = parts[1]
            if body.startswith(("json", "xml", "svg", "html")):
                body = body.split("\n", 1)[1] if "\n" in body else ""
            return body.strip()
    return t


def _converse(prompt: str, max_tokens: int = 2000, temperature: float = 0.6) -> str:
    resp = _br.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    return resp["output"]["message"]["content"][0]["text"]


DECK_PROMPT = """Você é um designer de pitch decks experiente. A ideia do usuário é:

\"\"\"{idea}\"\"\"

Monte um pitch deck com 6 slides. Retorne APENAS JSON válido, sem markdown, nesta estrutura exata:

{{
  "title": "nome curto do produto",
  "tagline": "frase de uma linha que resume a proposta",
  "theme": {{"primary": "#HEX", "accent": "#HEX", "bg": "#HEX"}},
  "slides": [
    {{"type":"cover","title":"...","subtitle":"..."}},
    {{"type":"problem","title":"O Problema","bullets":["...","...","..."]}},
    {{"type":"solution","title":"Nossa Solução","bullets":["...","...","..."]}},
    {{"type":"how","title":"Como Funciona","bullets":["...","...","..."]}},
    {{"type":"market","title":"Mercado e Oportunidade","bullets":["...","...","..."]}},
    {{"type":"cta","title":"...","subtitle":"..."}}
  ]
}}

Regras: pt-BR. Bullets curtos (10-14 palavras). Paleta moderna e legível. Tagline com impacto, sem buzzword vazia.
"""


def _generate_deck_json(idea: str) -> dict:
    raw = _converse(DECK_PROMPT.format(idea=idea), max_tokens=2000, temperature=0.7)
    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


SLIDE_PROMPT = """Desenhe UM slide em SVG puro 1280x720 baseado neste conteúdo:

PALETA: primary={primary}, accent={accent}, bg={bg}
TIPO: {stype}
CONTEÚDO: {content_json}

Regras: tipografia Arial, títulos 64-80px, bullets 28-32px. Bullets alinhados à esquerda com um marcador. 1-2 formas geométricas abstratas de fundo com opacidade baixa. Slides cover/cta centralizados. Contraste AA. SVG puro — NADA de imagens externas. Retorne APENAS o SVG começando em <svg, sem markdown.
"""


def _render_slide_svg(theme: dict, slide: dict) -> str:
    content_json = json.dumps(
        {k: v for k, v in slide.items() if k != "type"}, ensure_ascii=False
    )
    prompt = SLIDE_PROMPT.format(
        primary=theme.get("primary", "#6B46C1"),
        accent=theme.get("accent", "#F97316"),
        bg=theme.get("bg", "#0F172A"),
        stype=slide.get("type", "content"),
        content_json=content_json,
    )
    raw = _converse(prompt, max_tokens=3500, temperature=0.6)
    svg = _strip_fences(raw)
    i = svg.find("<svg")
    if i > 0:
        svg = svg[i:]
    end = svg.rfind("</svg>")
    if end != -1:
        svg = svg[: end + len("</svg>")]
    return svg


@tool
def generate_pitch_deck(idea: str) -> dict:
    """Gera um pitch deck completo de 6 slides a partir de uma ideia/briefing.

    Use esta ferramenta quando o usuário pede "monta um pitch", "cria um deck",
    "apresentação sobre X" ou "slides pra vender essa ideia".

    Args:
        idea: descrição da ideia de produto/negócio (3-10 frases ideal).

    Retorna um objeto com pitch_id, title, slide_count, e preview_url.
    O frontend renderiza um card com preview clicável.
    """
    if not idea or len(idea.strip()) < 10:
        return {"ok": False, "message": "Ideia muito curta, preciso de mais contexto."}
    deck = _generate_deck_json(idea)
    theme = deck.get("theme") or {"primary": "#6B46C1", "accent": "#F97316", "bg": "#0F172A"}
    slides = deck.get("slides", [])

    def _placeholder_svg(slide: dict, reason: str) -> str:
        """Fallback SVG when a single Bedrock slide call fails — keeps the deck usable (P1.10)."""
        title = (slide.get("title") or slide.get("type") or "slide").replace("<", "").replace(">", "")
        return (
            '<svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="1280" height="720" fill="{theme.get("bg","#0F172A")}"/>'
            f'<text x="640" y="340" font-family="Arial" font-size="48" fill="{theme.get("accent","#F97316")}" text-anchor="middle">{title}</text>'
            f'<text x="640" y="400" font-family="Arial" font-size="20" fill="#888" text-anchor="middle">(slide em recuperação — {reason})</text>'
            "</svg>"
        )

    def render_one(pair):
        i, slide = pair
        # Gate globally so we don't fan-out past Bedrock RPS budget (P0.7)
        with _PITCH_GATE:
            try:
                return i, _render_slide_svg(theme, slide)
            except Exception as e:
                logger.warning("pitch slide %d failed: %s", i, e)
                return i, _placeholder_svg(slide, type(e).__name__)

    # Per-request executor still lets slides overlap up to MAX_PARALLEL, but
    # the semaphore ensures total concurrency across the worker is bounded.
    results: dict[int, str] = {}
    with cf.ThreadPoolExecutor(max_workers=min(6, _PITCH_MAX_PARALLEL)) as ex:
        for fut in cf.as_completed([ex.submit(render_one, (i, s)) for i, s in enumerate(slides)]):
            try:
                idx, svg = fut.result()
            except Exception as e:
                # Should not reach here (render_one catches) — defensive
                logger.error("pitch slide unexpected failure: %s", e)
                continue
            results[idx] = svg
    ordered = [results[i] for i in sorted(results)]

    pitch_id = "pitch-" + uuid.uuid4().hex[:10]
    # Store the full deck in DynamoDB under a dedicated pk for retrieval
    item = {
        "pk": f"PITCH#{pitch_id}",
        "sk": "DECK",
        "category": "pitch",
        "created_at": int(__import__("time").time()),
        "title": deck.get("title", "Pitch"),
        "tagline": deck.get("tagline", ""),
        "theme": theme,
        "slides_meta": [{"type": s.get("type"), "title": s.get("title", "")} for s in slides],
        "svgs": ordered,
        "idea": idea,
    }
    _table.put_item(Item=item)

    # Also drop a lightweight index item so /ops shows it alongside other categories
    put_item(
        "pitch",
        title=deck.get("title", "Pitch"),
        tagline=deck.get("tagline", ""),
        pitch_id=pitch_id,
        slide_count=len(ordered),
    )

    return {
        "ok": True,
        "pitch_id": pitch_id,
        "title": deck.get("title", "Pitch"),
        "tagline": deck.get("tagline", ""),
        "slide_count": len(ordered),
        "preview_url": f"/api/pitch/{pitch_id}",
    }


def load_pitch(pitch_id: str) -> dict | None:
    resp = _table.get_item(Key={"pk": f"PITCH#{pitch_id}", "sk": "DECK"})
    item = resp.get("Item")
    if not item:
        return None
    # Respect soft-delete (P1.11): if the item was wiped via delete_item, don't serve it.
    if item.get("deleted_at"):
        return None
    return _json_safe(item)


DECK_HTML_TEMPLATE = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>
  html,body{{margin:0;padding:0;background:#050505;font-family:-apple-system,system-ui,sans-serif;color:#eee}}
  .stage{{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}}
  .slide{{width:min(1280px,96vw);aspect-ratio:16/9;box-shadow:0 20px 60px rgba(0,0,0,.6);border-radius:14px;overflow:hidden;background:#111}}
  .slide svg{{width:100%;height:100%;display:block}}
  .nav{{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);display:flex;gap:8px;background:rgba(20,20,20,.85);padding:8px 12px;border-radius:999px;backdrop-filter:blur(8px)}}
  .nav button{{background:#222;color:#eee;border:1px solid #333;padding:6px 14px;border-radius:999px;cursor:pointer;font-size:14px}}
  .nav button:hover{{background:#2d2d2d}}
  .nav span{{padding:6px 8px;opacity:.75;font-size:13px}}
  .caption{{position:fixed;top:14px;left:14px;opacity:.55;font-size:12px;letter-spacing:.06em;text-transform:uppercase}}
</style>
</head>
<body>
<div class="caption">{title} — {tagline}</div>
<div class="stage" id="stage"></div>
<div class="nav"><button onclick="prev()">&larr;</button><span id="counter"></span><button onclick="next()">&rarr;</button></div>
<script>
const SLIDES = {slides_json};
let i = 0;
function render() {{
  document.getElementById('stage').innerHTML = '<div class="slide">' + SLIDES[i] + '</div>';
  document.getElementById('counter').textContent = (i+1) + ' / ' + SLIDES.length;
}}
function next() {{ i = (i+1) % SLIDES.length; render(); }}
function prev() {{ i = (i-1+SLIDES.length) % SLIDES.length; render(); }}
document.addEventListener('keydown', e => {{ if (e.key==='ArrowRight'||e.key===' ') next(); if (e.key==='ArrowLeft') prev(); }});
render();
</script>
</body>
</html>"""


def render_deck_html(pitch: dict) -> str:
    return DECK_HTML_TEMPLATE.format(
        title=pitch.get("title", "Pitch"),
        tagline=pitch.get("tagline", ""),
        slides_json=json.dumps(pitch.get("svgs", [])),
    )
