import os, json, textwrap, re
from datetime import datetime, timedelta, timezone
import requests

# ---------- ENV ----------
OPENAI_API_KEY      = os.environ["OPENAI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
MODEL        = os.getenv("MODEL", "gpt-4.1-mini")
HOURS        = int(os.getenv("HOURS", "24"))
LOCALE       = os.getenv("LOCALE", "fr-FR")
SOURCE_LANGS = os.getenv("SOURCE_LANGS", "fr,en")  # FR + EN
TZ           = os.getenv("TIMEZONE", "Europe/Paris")
COLOR        = int(os.getenv("EMBED_COLOR", "1752220"))  # 0x1ABC9C (teal)

now_utc   = datetime.now(timezone.utc)
since_utc = now_utc - timedelta(hours=HOURS)
date_str  = now_utc.strftime("%Y-%m-%d")

# ---------- Discord limits (safe) ----------
DISCORD_MAX_EMBEDS   = 10     # embeds per message
DISCORD_MAX_EMBED    = 6000   # hard API limit
EMBED_TARGET_BUDGET  = 5500   # target to keep margin
DISCORD_MAX_TITLE    = 256
DISCORD_MAX_DESC     = 4096
FIELD_HARD_MAX       = 1024
FIELD_SOFT_MAX       = 700
DESC_SOFT_MAX        = 300

# ---------- Helpers ----------
def _text_len(s: str) -> int:
    return len(s or "")

def chunk_text(txt: str, size: int = FIELD_SOFT_MAX):
    """Split by lines; hard-split very long lines (e.g., URLs)."""
    txt = (txt or "").strip()
    if not txt:
        return []
    lines, chunks, buf = txt.splitlines(), [], ""
    for line in lines:
        add_len = len(line) + (0 if not buf else 1)
        if len(buf) + add_len > size:
            if buf.strip():
                chunks.append(buf.rstrip())
            if len(line) > size:
                for i in range(0, len(line), size):
                    chunks.append(line[i:i+size])
                buf = ""
            else:
                buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks

def _clean_embed(e: dict) -> dict:
    """Remove None/empty & clamp base sizes."""
    out = {}
    for k, v in e.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if k == "title":
            out[k] = v[:DISCORD_MAX_TITLE]
        elif k == "description":
            out[k] = v[:DISCORD_MAX_DESC]
        elif k == "fields":
            vv = []
            for f in (v or []):
                name = (f.get("name") or "").strip()
                value = (f.get("value") or "").strip()
                if name and value:
                    if len(value) > FIELD_SOFT_MAX:
                        value = value[:FIELD_SOFT_MAX] + "…"
                    value = value[:FIELD_HARD_MAX]
                    vv.append({"name": name[:256], "value": value})
            if vv:
                out[k] = vv
        else:
            out[k] = v
    return out

def _embed_size(e: dict) -> int:
    size = 0
    size += _text_len(e.get("title"))
    size += _text_len(e.get("description"))
    for f in e.get("fields") or []:
        size += _text_len(f.get("name")) + _text_len(f.get("value"))
    footer = e.get("footer") or {}
    size += _text_len(footer.get("text"))
    return size

def _shrink_to_fit(e: dict):
    """Reduce desc/field to fit under target, then under hard 6000 if needed."""
    e = _clean_embed(e)
    guard = 0
    while _embed_size(e) > EMBED_TARGET_BUDGET and guard < 100:
        guard += 1
        desc = e.get("description")
        if desc and len(desc) > 120:
            e["description"] = desc[:-80] + "…"
            e = _clean_embed(e)
            continue
        if e.get("fields"):
            f = e["fields"][0]
            val = f["value"]
            if len(val) > 120:
                f["value"] = val[:-80] + "…"
                e = _clean_embed(e)
                continue
        break

    guard = 0
    while _embed_size(e) > DISCORD_MAX_EMBED and guard < 100:
        guard += 1
        desc = e.get("description")
        if desc and len(desc) > 50:
            e["description"] = desc[:-50] + "…"
            e = _clean_embed(e)
            continue
        if e.get("fields"):
            f = e["fields"][0]
            val = f["value"]
            if len(val) > 50:
                f["value"] = val[:-50] + "…"
                e = _clean_embed(e)
                continue
        if e.get("description"):
            e["description"] = ""
            e = _clean_embed(e)
            continue
        if e.get("fields"):
            e["fields"].pop(0)
            e = _clean_embed(e)
            continue
        break
    return e

def _send_embeds_in_batches(embeds: list):
    """Send in batches of up to 10 embeds (multiple messages if needed)."""
    for i in range(0, len(embeds), DISCORD_MAX_EMBEDS):
        batch = embeds[i:i+DISCORD_MAX_EMBEDS]
        safe_batch = [_shrink_to_fit(em) for em in batch]
        payload = {"embeds": [_clean_embed(em) for em in safe_batch if em.get("title") or em.get("description") or em.get("fields")]}
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
        if not r.ok:
            raise RuntimeError(f"Discord 400 payload error: {r.status_code} {r.text}")

def post_discord_embeds(title: str, description: str, bullet_text: str, links_text: str):
    """
    Robust strategy:
      - Embed #1: title + short description + footer (no fields)
      - Then: 1 field per embed for bullets (chunks), then sources (chunks)
      - Send multiple messages if >10 embeds
    """
    title = (title or "").strip()[:DISCORD_MAX_TITLE]
    description = (description or "").strip()
    if len(description) > DESC_SOFT_MAX:
        description = description[:DESC_SOFT_MAX] + "…"

    first_embed = {
        "title": title,
        "description": description if description else None,
        "color": COLOR,
        "footer": {"text": f"Veille Finance • {date_str} • Window: {HOURS}h • {TZ}"},
    }
    first_embed = _shrink_to_fit(first_embed)

    embeds = [first_embed]

    bullet_chunks = chunk_text(bullet_text, FIELD_SOFT_MAX) if bullet_text else []
    link_chunks   = chunk_text(links_text,  FIELD_SOFT_MAX) if links_text else []

    for idx, ch in enumerate(bullet_chunks):
        e = {"color": COLOR, "fields": [{"name": "Synthèse" if idx == 0 else "Synthèse (suite)", "value": ch}]}
        embeds.append(_shrink_to_fit(e))

    for idx, ch in enumerate(link_chunks):
        e = {"color": COLOR, "fields": [{"name": "Sources" if idx == 0 else "Sources (suite)", "value": ch}]}
        embeds.append(_shrink_to_fit(e))

    _send_embeds_in_batches(embeds)

# ---------- OpenAI (Responses API + web_search tool) ----------
def call_openai_websearch(prompt: str) -> str:
    """
    Call OpenAI Responses API with 'web_search' tool enabled.
    Returns Markdown.
    """
    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    body = {
        "model": MODEL,
        "input": prompt,
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
    }
    r = requests.post(url, headers=headers, json=body, timeout=180)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict) and data.get("output_text"):
        return data["output_text"].strip()

    try:
        chunks = []
        for blk in data.get("output", []):
            for c in blk.get("content", []):
                t = c.get("text")
                if t:
                    chunks.append(t)
        if chunks:
            return "".join(chunks).strip()
    except Exception:
        pass

    return json.dumps(data, ensure_ascii=False)[:4000]

# ---------- Markdown parsing ----------
SECTION_LINKS_RE = re.compile(r"^\s*#{0,3}\s*links?\s*$", re.IGNORECASE | re.MULTILINE)
SECTION_LIENS_RE = re.compile(r"^\s*#{0,3}\s*liens?\s*$", re.IGNORECASE | re.MULTILINE)

def split_summary_links(md: str, fallback_title: str):
    """Return (title, bullets, links) from model Markdown output."""
    md = (md or "").strip()
    if not md:
        return fallback_title, "", ""

    title_match = re.search(r"^\s*#+\s*(.+)", md)
    title = title_match.group(1).strip() if title_match else fallback_title

    parts = SECTION_LINKS_RE.split(md)
    if len(parts) < 2:
        parts = SECTION_LIENS_RE.split(md)

    if len(parts) >= 2:
        before = parts[0].strip()
        after  = parts[1].strip()
        bullets = before
        links   = after
    else:
        bullets = md
        links = ""

    if title_match:
        start = title_match.end()
        if len(md) > start:
            bullets = md[start:].strip()

    return title, bullets, links

# ---------- Prompt ----------
def build_prompt():
    """
    FINANCE NEWS scope:
      - Macro: inflation (CPI/HICP), GDP, employment, PMIs
      - Central banks: ECB, Fed, BoE, BoJ (decisions, speeches, minutes)
      - Markets: equities, fixed income (yields, credit), FX, commodities, energy
      - Corporate: earnings, guidance, M&A, IPOs, restructurings, bankruptcies
      - Regulation/Policy: ESMA/ESRB/EC/SEC, Basel, MiFID/MiCA, sanctions, taxes
      - Fintech & payments: Open Banking, instant payments, crypto policy, CBDCs
      - Banking risk: liquidity, capital, stress tests, contagion
      - Europe focus acceptable; include major US/Asia when material
    """
    langs_note = SOURCE_LANGS.replace(",", ", ")
    return textwrap.dedent(f"""
    Tu es un analyste **Finance** chargé d'une veille quotidienne.

    Tâche :
    - Utilise la **recherche web** pour identifier les **actualités finance** publiées entre
      **{since_utc.strftime("%Y-%m-%d %H:%M UTC")}** et **{now_utc.strftime("%Y-%m-%d %H:%M UTC")}**.
    - **Sources à considérer** : en **français et en anglais** ({langs_note}). Priorise les sources **primaires**
      (communiqués officiels, régulateurs, banques centrales) et médias réputés (éviter duplications/marronniers).
      Si une info n’existe qu’en anglais, **inclue-la**.

    Rendu en **français ({LOCALE})** et en **Markdown** :
    1) Un titre : "Veille Finance — {date_str} (dernières {HOURS} h)".
    2) Un **résumé global** (2–3 phrases).
    3) **5–10 puces** factuelles (indices, rendements, chiffres macro, décisions de banques centrales, deals, guidance).
    4) Une section **Liens / Links** (10–15 items max) au format :
       - **[FR]** ou **[EN]** — Titre court — URL (ajoute la **date/heure** si dispo).
       (Marque chaque lien avec [FR]/[EN] selon la langue de la source.)
    5) Cite **uniquement des sources réelles** (URLs visibles). Indique [paywall] si nécessaire.
    6) Aucune invention ; si incertain, précise-le. Évite les posts SEO à faible valeur.

    Contexte :
    - Langue de sortie : français ({LOCALE})
    - Fuseau : {TZ}
    - Fenêtre : {HOURS} heures
    - Effectue **plusieurs recherches** si utile.

    Format strictement Markdown. Pas de blocs de code inutiles.
    """).strip()

# ---------- Main ----------
def main():
    prompt = build_prompt()
    try:
        md = call_openai_websearch(prompt)
    except Exception as e:
        err = f"⚠️ Erreur durant la recherche/synthèse : {e}"
        post_discord_embeds(
            title=f"Veille Finance — {date_str} (dernières {HOURS} h)",
            description=err,
            bullet_text="",
            links_text=""
        )
        raise

    if not md or len(md.strip()) < 30:
        post_discord_embeds(
            title=f"Veille Finance — {date_str} (dernières {HOURS} h)",
            description="Aucune sortie exploitable retournée par l'IA (clé API/modèle ?).",
            bullet_text="",
            links_text=""
        )
        return

    fallback_title = f"Veille Finance — {date_str} (dernières {HOURS} h)"
    title, bullets, links = split_summary_links(md, fallback_title)

    # Description: first short paragraph
    first_para = re.split(r"\n\s*\n", bullets.strip(), maxsplit=1)[0] if bullets.strip() else ""
    description = first_para
    bullets_body = bullets[len(first_para):].strip() if bullets.strip() else ""

    post_discord_embeds(
        title=title,
        description=description,
        bullet_text=bullets_body,
        links_text=links
    )

if __name__ == "__main__":
    main()
