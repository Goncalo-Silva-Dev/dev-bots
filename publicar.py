"""
Bot do servidor DEVS - versão GitHub Actions
Corre de 30 em 30 minutos, publica o que houver de novo e termina.

- Cursos gratuitos  -> #cursos-gratuitos
- Notícias tech     -> #tech-news
- Notícias de AI    -> #ai-news
- Notícias PT       -> #portugal-tech
- Dica do dia       -> #dicas-rápidas (a partir das 10:00, uma por dia)
- Curso em destaque -> #cursos-gratuitos (a partir das 13:00, um por dia, de cursos.json)
- Dica de AI        -> #prompts       (a partir das 16:00, uma por dia)

Variáveis (Secrets do GitHub): DISCORD_TOKEN, GUILD_ID, ANTHROPIC_API_KEY (opcional)
"""
import datetime
import html
import json
import os
import random
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = os.environ["GUILD_ID"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODELO_AI = "claude-haiku-5-5"

BASE = Path(__file__).parent
STATE_FILE = BASE / "estado.json"
TIPS_FILE = BASE / "dicas.json"
CURSOS_FILE = BASE / "cursos.json"
TZ = ZoneInfo("Europe/Lisbon")
MAX_POR_FEED = 3
HORA_DICA = 10
HORA_CURSO = 13
HORA_DICA_AI = 16

API = "https://discord.com/api/v10"
HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "User-Agent": "DiscordBot (https://github.com, 1.0)",
}
UA_FEEDS = "Mozilla/5.0 (DEVS Discord bot; RSS reader)"

# A chave é o FIM do nome do canal (o emoji e o "│" são ignorados).
FEEDS = {
    "cursos-gratuitos": [
        ("freeCodeCamp", "https://www.freecodecamp.org/news/rss/"),
        ("Class Central", "https://www.classcentral.com/report/feed/"),
        ("r/FreeUdemyCoupons", "https://www.reddit.com/r/FreeUdemyCoupons/new/.rss"),
    ],
    "tech-news": [
        ("Hacker News", "https://hnrss.org/frontpage?points=250"),
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ],
    "ai-news": [
        ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
        ("OpenAI", "https://openai.com/news/rss.xml"),
        ("HN (AI)", "https://hnrss.org/newest?q=AI+OR+LLM+OR+GPT+OR+Claude&points=150"),
    ],
    "portugal-tech": [
        ("Pplware", "https://pplware.sapo.pt/feed/"),
        ("4gnews", "https://www.4gnews.pt/feed/"),
        ("Shifter", "https://shifter.pt/feed/"),
    ],
}
CORES = {"cursos-gratuitos": 0x9B59B6, "tech-news": 0xE67E22, "ai-news": 0x1ABC9C,
         "portugal-tech": 0x2ECC71, "geral": 0xF1C40F, "ai": 0x1ABC9C}
EMOJI = {"cursos-gratuitos": "🎓", "tech-news": "📰", "ai-news": "🤖", "portugal-tech": "🇵🇹"}
CANAL_DICA = {"geral": "dicas-rápidas", "ai": "prompts"}


# ------------------------------------------------------------------ Discord REST
def discord(method, path, **kw):
    for _ in range(5):
        r = requests.request(method, API + path, headers=HEADERS, timeout=20, **kw)
        if r.status_code == 429:  # rate limit
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return r.json() if r.content else None
    raise RuntimeError(f"Rate limit persistente em {path}")


def canais_do_servidor():
    # tipo 0 = texto, 5 = anúncios
    return [c for c in discord("GET", f"/guilds/{GUILD_ID}/channels") if c["type"] in (0, 5)]


def find_channel(canais, slug):
    return next((c for c in canais if c["name"].endswith(slug)), None)


def enviar(canal, embed):
    msg = discord("POST", f"/channels/{canal['id']}/messages", json={"embeds": [embed]})
    if canal["type"] == 5:  # canal de anúncios: publica para quem o segue
        try:
            discord("POST", f"/channels/{canal['id']}/messages/{msg['id']}/crosspost")
        except requests.HTTPError:
            pass
    time.sleep(1.5)


# ------------------------------------------------------------------ utilidades
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def clean(text, limit=280):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def entry_id(e):
    return e.get("id") or e.get("link") or e.get("title", "")


def image_of(e):
    for key in ("media_thumbnail", "media_content"):
        items = e.get(key)
        if items and items[0].get("url"):
            return items[0]["url"]
    for enc in e.get("enclosures", []):
        if enc.get("type", "").startswith("image"):
            return enc.get("href")
    return None


def embed_noticia(e, fonte, slug):
    emb = {
        "title": clean(e.get("title", "Sem título"), 250),
        "url": e.get("link"),
        "description": clean(e.get("summary", "")),
        "color": CORES.get(slug, 0x5865F2),
        "author": {"name": f"{EMOJI.get(slug, '🔗')} {fonte}"},
    }
    img = image_of(e)
    if img:
        emb["thumbnail"] = {"url": img}
    return emb


# ------------------------------------------------------------------ feeds
def verificar_feeds(state, canais):
    vistos = state.setdefault("vistos", {})
    total = 0
    for slug, feeds in FEEDS.items():
        canal = find_channel(canais, slug)
        if canal is None:
            print(f"[feeds] canal '*{slug}' não encontrado")
            continue
        for fonte, url in feeds:
            try:
                feed = feedparser.parse(url, agent=UA_FEEDS)
            except Exception as ex:
                print(f"[feeds] erro em {fonte}: {ex}")
                continue
            entries = feed.entries
            if not entries:
                print(f"[feeds] {fonte}: 0 entradas (feed em baixo ou URL errado?)")
                continue

            ja_vistos = set(vistos.get(url, []))
            primeira_vez = url not in vistos
            novos = [e for e in entries if entry_id(e) not in ja_vistos]
            novos = novos[:1] if primeira_vez else novos[:MAX_POR_FEED]

            for e in reversed(novos):
                try:
                    enviar(canal, embed_noticia(e, fonte, slug))
                    total += 1
                except requests.HTTPError as ex:
                    print(f"[feeds] falhou publicar em #{canal['name']}: {ex}")

            ids = [entry_id(e) for e in entries]
            vistos[url] = (ids + [i for i in vistos.get(url, []) if i not in ids])[:300]
            print(f"[feeds] {fonte}: {len(novos)} novas")
    return total


# ------------------------------------------------------------------ dicas
def dica_da_lista(state, tipo):
    dicas = load_json(TIPS_FILE, {}).get(tipo, [])
    if not dicas:
        return {"titulo": "Sem dicas", "texto": f"Adiciona dicas a dicas.json ('{tipo}')."}
    key = f"ordem_{tipo}"
    ordem = state.get(key) or []
    if not ordem or max(ordem) >= len(dicas):
        ordem = list(range(len(dicas)))
        random.shuffle(ordem)
    idx = ordem.pop(0)
    state[key] = ordem
    return dicas[idx]


def dica_ai(tipo):
    import anthropic

    temas = {
        "geral": ["Git", "Python", "JavaScript", "CSS", "Linux/terminal", "VS Code", "SQL",
                  "Docker", "APIs REST", "segurança web", "produtividade", "debugging", "testes"],
        "ai": ["escrever prompts", "few-shot prompting", "pedir output estruturado (JSON)",
               "usar AI para programar", "rever código com AI", "agentes e ferramentas",
               "limitações e alucinações", "RAG", "custos e tokens", "AI para estudar"],
    }
    tema = random.choice(temas[tipo])
    msg = anthropic.Anthropic().messages.create(
        model=MODELO_AI,
        max_tokens=600,
        messages=[{"role": "user", "content": (
            f"Escreve uma dica prática e concreta sobre '{tema}' para uma comunidade de "
            "estudantes e devs em Portugal (português de Portugal). Formato: primeira linha = "
            "título curto (sem markdown); depois a dica em no máximo 120 palavras. Podes "
            "incluir um snippet curto num bloco de código.")}],
    )
    titulo, _, corpo = msg.content[0].text.strip().partition("\n")
    return {"titulo": titulo.strip("# *"), "texto": corpo.strip()}


def publicar_dicas(state, canais, forcar=False):
    agora = datetime.datetime.now(TZ)
    hoje = agora.date().isoformat()
    for tipo, hora in (("geral", HORA_DICA), ("ai", HORA_DICA_AI)):
        chave = f"ultima_dica_{tipo}"
        if not forcar and (agora.hour < hora or state.get(chave) == hoje):
            continue
        canal = find_channel(canais, CANAL_DICA[tipo])
        if canal is None:
            print(f"[dicas] canal '*{CANAL_DICA[tipo]}' não encontrado")
            continue
        d = None
        if ANTHROPIC_API_KEY:
            try:
                d = dica_ai(tipo)
            except Exception as ex:
                print(f"[dicas] AI falhou ({ex}), a usar a lista")
        d = d or dica_da_lista(state, tipo)
        prefixo = "💡 Dica do dia: " if tipo == "geral" else "🤖 Dica de AI: "
        enviar(canal, {"title": (prefixo + d["titulo"])[:256],
                       "description": d["texto"][:4000], "color": CORES[tipo]})
        state[chave] = hoje
        print(f"[dicas] publicada dica '{tipo}'")


# ------------------------------------------------------------------ curso em destaque
def publicar_curso(state, canais, forcar=False):
    agora = datetime.datetime.now(TZ)
    hoje = agora.date().isoformat()
    if not forcar and (agora.hour < HORA_CURSO or state.get("ultimo_curso") == hoje):
        return
    cursos = load_json(CURSOS_FILE, [])
    canal = find_channel(canais, "cursos-gratuitos")
    if not cursos or canal is None:
        print("[curso] sem cursos.json ou canal *cursos-gratuitos")
        return
    ordem = state.get("ordem_cursos") or []
    if not ordem or max(ordem) >= len(cursos):
        ordem = list(range(len(cursos)))
        random.shuffle(ordem)
    c = cursos[ordem.pop(0)]
    state["ordem_cursos"] = ordem

    enviar(canal, {
        "title": f"🎓 Curso em destaque: {c['nome']}"[:256],
        "url": c["url"],
        "description": c["descricao"],
        "color": 0x9B59B6,
        "author": {"name": c["fornecedor"]},
        "fields": [
            {"name": "Área", "value": c.get("area", "-"), "inline": True},
            {"name": "Nível", "value": c.get("nivel", "-"), "inline": True},
            {"name": "Duração", "value": c.get("duracao", "-"), "inline": True},
            {"name": "Certificado", "value": c.get("certificado", "-"), "inline": True},
            {"name": "Preço", "value": "Gratuito ✅", "inline": True},
        ],
        "footer": {"text": "Já fizeste este curso? Deixa a tua opinião em #reviews-cursos"},
    })
    state["ultimo_curso"] = hoje
    print(f"[curso] publicado: {c['nome']}")


# ------------------------------------------------------------------ main
def main():
    state = load_json(STATE_FILE, {})
    canais = canais_do_servidor()
    try:
        n = verificar_feeds(state, canais)
        forcar = os.environ.get("FORCAR_DICAS") == "1"
        publicar_dicas(state, canais, forcar=forcar)
        publicar_curso(state, canais, forcar=forcar)
        print(f"Feito: {n} notícias/cursos novos.")
    finally:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
