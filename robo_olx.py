# -*- coding: utf-8 -*-
"""
Robô de oportunidades OLX -> Telegram
Roda a cada 10 minutos no GitHub. Busca anúncios novos, calcula o preço
mediano de cada busca e avisa no Telegram quando algo está bem abaixo.
"""

# =====================================================================
#  CONFIGURAÇÃO — pode editar só esta parte
# =====================================================================

# Regiões do OLX onde procurar (formato do site: estado-go, estado-df, estado-sp...)
REGIOES = ["estado-go", "estado-df"]

# Buscas. Quanto mais específica a busca, melhor fica o preço médio.
# preco_minimo: abaixo disso ignora (evita capinha, carregador, peça...)
BUSCAS = [
    {"termo": "notebook i7",      "preco_minimo": 600},
    {"termo": "notebook i5",      "preco_minimo": 450},
    {"termo": "notebook ryzen",   "preco_minimo": 500},
    {"termo": "macbook",          "preco_minimo": 1200},
    {"termo": "iphone 13",        "preco_minimo": 900},
    {"termo": "iphone 14",        "preco_minimo": 1300},
    {"termo": "iphone 15",        "preco_minimo": 1800},
    {"termo": "samsung galaxy s23", "preco_minimo": 900},
]

# Avisar quando o preço estiver pelo menos X% abaixo da mediana (0.40 = 40%)
DESCONTO_MINIMO = 0.40

# Quantas páginas de resultados ler por busca/região para calcular a média
PAGINAS = 2

# Anúncios com estas palavras no título são ignorados
PALAVRAS_IGNORAR = [
    "defeito", "quebrado", "trincad", "peça", "peças", "retirada de peça",
    "carregador", "capinha", "capa ", "película", "pelicula", "tela ",
    "bateria", "teclado", "placa mãe", "placa mae", "carcaça", "carcaca",
    "troco", "troca", "aluguel", "conserto", "assistência", "assistencia",
    "não liga", "nao liga", "bloqueado", "icloud", "sucata",
]

# =====================================================================
#  DAQUI PRA BAIXO NÃO PRECISA MEXER
# =====================================================================

import html
from urllib.parse import quote
import json
import os
import re
import statistics
import sys
import time

from curl_cffi import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
ARQ_ESTADO = "estado.json"
TG = f"https://api.telegram.org/bot{TOKEN}"


# ---------- estado (anúncios já vistos, chat do Telegram) ----------
def carregar_estado():
    if os.path.exists(ARQ_ESTADO):
        with open(ARQ_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    return {"vistos": {}, "chat_id": None, "primeira_execucao": True, "ultimo_aviso_erro": 0}


def salvar_estado(est):
    limite = time.time() - 30 * 86400  # esquece anúncios com mais de 30 dias
    est["vistos"] = {k: v for k, v in est["vistos"].items() if v > limite}
    with open(ARQ_ESTADO, "w", encoding="utf-8") as f:
        json.dump(est, f, ensure_ascii=False, indent=0)


# ---------- Telegram ----------
def descobrir_chat_id():
    r = requests.get(f"{TG}/getUpdates", timeout=20).json()
    for upd in reversed(r.get("result", [])):
        msg = upd.get("message") or upd.get("edited_message") or {}
        if msg.get("chat", {}).get("id"):
            return msg["chat"]["id"]
    return None


def enviar(chat_id, texto, foto=None):
    try:
        if foto:
            r = requests.post(f"{TG}/sendPhoto", timeout=20, data={
                "chat_id": chat_id, "photo": foto, "caption": texto[:1000], "parse_mode": "HTML"})
            if r.status_code == 200:
                return
        requests.post(f"{TG}/sendMessage", timeout=20, data={
            "chat_id": chat_id, "text": texto, "parse_mode": "HTML",
            "disable_web_page_preview": "false"})
    except Exception as e:
        print("Falha ao enviar Telegram:", e)


# ---------- OLX ----------
def preco_para_numero(p):
    if p is None:
        return None
    if isinstance(p, (int, float)):
        return float(p)
    s = re.sub(r"[^\d,]", "", str(p)).replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def achar_anuncios(obj):
    """Procura, dentro do JSON da página, a lista de anúncios."""
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:3]):
        if any("subject" in x and ("url" in x or "friendlyUrl" in x) for x in obj[:5]):
            return obj
    if isinstance(obj, dict):
        if isinstance(obj.get("ads"), list):
            return obj["ads"]
        for v in obj.values():
            r = achar_anuncios(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = achar_anuncios(v)
            if r:
                return r
    return None


def ler_pagina(regiao, termo, pagina):
    url = f"https://www.olx.com.br/{regiao}?q={quote(termo)}&sf=1"
    if pagina > 1:
        url += f"&o={pagina}"
    r = requests.get(url, impersonate="chrome", timeout=30,
                     headers={"Accept-Language": "pt-BR,pt;q=0.9"})
    if r.status_code != 200:
        print(f"  {regiao} '{termo}' pág {pagina}: HTTP {r.status_code}")
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        print(f"  {regiao} '{termo}' pág {pagina}: página sem dados (possível bloqueio)")
        return None
    brutos = achar_anuncios(json.loads(m.group(1))) or []
    anuncios = []
    for a in brutos:
        if not isinstance(a, dict) or not a.get("subject"):
            continue
        id_ = str(a.get("listId") or a.get("adId") or a.get("id") or a.get("url"))
        imgs = a.get("images") or []
        foto = None
        if imgs and isinstance(imgs[0], dict):
            foto = imgs[0].get("original") or imgs[0].get("originalWebp")
        anuncios.append({
            "id": id_,
            "titulo": a["subject"],
            "preco": preco_para_numero(a.get("price") or a.get("priceValue")),
            "url": a.get("url") or a.get("friendlyUrl"),
            "local": a.get("location") or "",
            "foto": foto,
        })
    return anuncios


def deve_ignorar(titulo):
    t = titulo.lower()
    return any(p in t for p in PALAVRAS_IGNORAR)


def brl(v):
    return "R$ " + f"{v:,.0f}".replace(",", ".")


# ---------- principal ----------
def main():
    if not TOKEN:
        sys.exit("Falta o segredo TELEGRAM_TOKEN no GitHub.")

    est = carregar_estado()
    if not est.get("chat_id"):
        est["chat_id"] = descobrir_chat_id()
        if not est["chat_id"]:
            sys.exit("Não achei seu chat. Mande um 'oi' para o seu bot no Telegram e rode de novo.")

    primeira = est.get("primeira_execucao", True)
    chat = est["chat_id"]
    total_lidos = 0
    alertas = 0

    for busca in BUSCAS:
        termo, minimo = busca["termo"], busca["preco_minimo"]
        anuncios = []
        for regiao in REGIOES:
            for pag in range(1, PAGINAS + 1):
                try:
                    res = ler_pagina(regiao, termo, pag)
                except Exception as e:
                    print(f"  erro em {regiao} '{termo}':", e)
                    res = None
                if not res:
                    break
                anuncios += res
                time.sleep(2)

        total_lidos += len(anuncios)
        validos = [a for a in anuncios
                   if a["preco"] and a["preco"] >= minimo and not deve_ignorar(a["titulo"])]
        if len(validos) < 8:
            print(f"'{termo}': poucos anúncios ({len(validos)}) para calcular média, pulando alertas")
            for a in anuncios:
                est["vistos"].setdefault(a["id"], time.time())
            continue

        mediana = statistics.median(a["preco"] for a in validos)
        teto = mediana * (1 - DESCONTO_MINIMO)
        print(f"'{termo}': {len(validos)} anúncios, mediana {brl(mediana)}, alerta abaixo de {brl(teto)}")

        for a in anuncios:
            novo = a["id"] not in est["vistos"]
            est["vistos"].setdefault(a["id"], time.time())
            if not novo or primeira or a not in validos:
                continue
            if a["preco"] <= teto:
                desconto = 1 - a["preco"] / mediana
                txt = (f"🔥 <b>Oportunidade: {html.escape(termo)}</b>\n\n"
                       f"{html.escape(a['titulo'])}\n"
                       f"💰 <b>{brl(a['preco'])}</b> (média {brl(mediana)}, {desconto:.0%} abaixo)\n"
                       f"📍 {html.escape(str(a['local']))}\n\n"
                       f"{a['url']}")
                enviar(chat, txt, a["foto"])
                alertas += 1
                time.sleep(1)

    if total_lidos == 0 and time.time() - est.get("ultimo_aviso_erro", 0) > 86400:
        enviar(chat, "⚠️ Robô OLX: não consegui ler nenhum anúncio nesta rodada. "
                     "O OLX pode estar bloqueando o acesso pela nuvem. Vou continuar tentando.")
        est["ultimo_aviso_erro"] = time.time()

    if primeira and total_lidos > 0:
        enviar(chat, f"✅ Robô OLX ligado! Li {total_lidos} anúncios e guardei como referência. "
                     f"A partir de agora te aviso só dos anúncios <b>novos</b> que estiverem "
                     f"{DESCONTO_MINIMO:.0%} ou mais abaixo da média.")
        est["primeira_execucao"] = False

    salvar_estado(est)
    print(f"Fim: {total_lidos} anúncios lidos, {alertas} alertas enviados.")


if __name__ == "__main__":
    main()
