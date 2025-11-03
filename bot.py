#!/usr/bin/env python3
"""
bot_vixsrc_tmdb_node.py
Bot Telegram che usa TMDB e il resolver Node (Playwright) per vixsrc.to

Requisiti Python:
 pip install python-telegram-bot==20.3 requests beautifulsoup4

Avvio:
 1) Avvia il resolve_service.js (Node) su localhost:3001
 2) Avvia questo script: python bot_vixsrc_tmdb_node.py
"""
import logging
import re
import urllib.parse
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ========== CONFIGURAZIONE ==========
BOT_TOKEN = "7660218441:AAFcJJwueHWpeZKvwzKX5CQKg8Qjz57P250"
TMDB_API_KEY = "be78689897669066bef6906e501b0e10"
ALLOWED_USERS = [7621984877]

SEARCH_BASE = "https://vixsrc.to"
NODE_RESOLVER_BASE = "http://127.0.0.1:3001"  # endpoint del servizio Node
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"}

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== UTILS ==========
def user_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

def tmdb_search(title: str) -> Optional[Dict]:
    base = "https://api.themoviedb.org/3"
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "it-IT"}
    try:
        r = requests.get(f"{base}/search/multi", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        if not results:
            return None
        top = results[0]
        return {
            "id": top.get("id"),
            "media_type": top.get("media_type") or ("movie" if top.get("title") else "tv"),
            "title": top.get("title") or top.get("name") or title,
            "overview": top.get("overview") or "",
            "poster": f"https://image.tmdb.org/t/p/w500{top['poster_path']}" if top.get("poster_path") else None,
        }
    except Exception as e:
        logger.warning("TMDB search error: %s", e)
        return None

def vix_movie_url(tmdb_id: int) -> str:
    return f"{SEARCH_BASE}/movie/{tmdb_id}"

def vix_tv_url(tmdb_id: int, season: int, episode: int) -> str:
    return f"{SEARCH_BASE}/tv/{tmdb_id}/{season}/{episode}"

def normalize_url(base: str, link: str) -> str:
    if not link:
        return link
    if link.startswith("//"):
        return "https:" + link
    if link.startswith("http"):
        return link
    return urllib.parse.urljoin(base, link)

def try_resolve_token_endpoint(url: str) -> List[Dict]:
    sources = []
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=12)
        r.raise_for_status()
        text = r.text
        json_urls = re.findall(r"(https?://[^\s'\"<>]+?/(?:sources|ajax|stream)[^\s'\"<>]*)", text)
        for ju in json_urls:
            try:
                jr = requests.get(ju, headers=DEFAULT_HEADERS, timeout=10)
                jr.raise_for_status()
                try:
                    data = jr.json()
                except Exception:
                    data = {}
                if isinstance(data, dict):
                    if data.get("file"):
                        sources.append({"label":"json-file","url":normalize_url(url,data["file"])})
                    if isinstance(data.get("sources"), list):
                        for s in data["sources"]:
                            if isinstance(s, dict):
                                src = s.get("file") or s.get("src") or s.get("url")
                                if src:
                                    sources.append({"label": s.get("label","source"), "url": normalize_url(url, src)})
                            elif isinstance(s, str):
                                sources.append({"label":"source","url":normalize_url(url,s)})
                found = re.findall(r"https?://[^\s'\"<>]+?\.(?:mp4|m3u8|webm)", jr.text)
                for f in found:
                    sources.append({"label":"found-in-endpoint","url":f})
            except Exception:
                continue
        tokens = re.findall(r"token\s*[:=]\s*['\"]([A-Za-z0-9_\-\.]+)['\"]", text)
        files = re.findall(r"(https?://[^\s'\"<>]+?\.(?:mp4|m3u8|webm)[^\s'\"<>]*)", text)
        for f in files:
            for t in tokens:
                if "token=" in f or "auth=" in f:
                    sources.append({"label":"file-with-token","url":f})
                else:
                    sep = "&" if "?" in f else "?"
                    candidate = f + sep + "token=" + t
                    sources.append({"label":"file+token","url":candidate})
        soup = BeautifulSoup(text, "html.parser")
        for iframe in soup.select("iframe"):
            src = iframe.get("src")
            if not src:
                continue
            iframe_url = normalize_url(url, src)
            try:
                r_if = requests.get(iframe_url, headers=DEFAULT_HEADERS, timeout=10)
                r_if.raise_for_status()
                found = re.findall(r"https?://[^\s'\"<>]+?\.(?:mp4|m3u8|webm)[^\s'\"<>]*", r_if.text)
                for f in found:
                    sources.append({"label":"iframe-found","url":f})
                more = re.findall(r"(https?://[^\s'\"<>]+?/sources[^\s'\"<>]*)", r_if.text)
                for m in more:
                    try:
                        mr = requests.get(m, headers=DEFAULT_HEADERS, timeout=8)
                        mr.raise_for_status()
                        found2 = re.findall(r"https?://[^\s'\"<>]+?\.(?:mp4|m3u8|webm)[^\s'\"<>]*", mr.text)
                        for f in found2:
                            sources.append({"label":"iframe-sources","url":f})
                    except Exception:
                        pass
            except Exception:
                continue
        found_global = re.findall(r"https?://[^\s'\"<>]+?\.(?:mp4|m3u8|webm)[^\s'\"<>]*", text)
        for f in found_global:
            sources.append({"label":"found-global","url":f})
    except Exception as e:
        logger.debug("try_resolve_token_endpoint error: %s", e)
    seen = set(); uniq = []
    for s in sources:
        u = s["url"]
        if u in seen: continue
        seen.add(u); uniq.append(s)
    return uniq

def extract_video_sources_from_page(page_url: str) -> List[Dict]:
    # primo tentativo semplice con requests/bs4
    try:
        r = requests.get(page_url, headers=DEFAULT_HEADERS, timeout=12)
        r.raise_for_status()
        html_text = r.text
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception as e:
        logger.warning("Failed fetch page %s : %s", page_url, e)
        html_text = ""
        soup = BeautifulSoup("", "html.parser")

    results = []
    for source in soup.select("video source"):
        src = source.get("src")
        if src:
            results.append({"label":"video-source","url":normalize_url(page_url, src)})
    for video in soup.select("video"):
        src = video.get("src")
        if src:
            results.append({"label":"video-tag","url":normalize_url(page_url, src)})
    for iframe in soup.select("iframe"):
        src = iframe.get("src") or ""
        if not src:
            continue
        iframe_url = normalize_url(page_url, src)
        if re.search(r"\.(mp4|m3u8|webm)(\?|$)", iframe_url):
            results.append({"label":"iframe-direct","url":iframe_url})
        else:
            resolved = try_resolve_token_endpoint(iframe_url)
            results.extend(resolved)
    found = re.findall(r"https?://[^\s'\"<>]+?\.(?:mp4|m3u8|webm)[^\s'\"<>]*", html_text)
    for f in found:
        results.append({"label":"found-in-page","url":f})
    if not results:
        # fallback: chiama il resolver Node (Playwright) in esecuzione
        try:
            resp = requests.get(NODE_RESOLVER_BASE + '/resolve', params={'url': page_url}, timeout=40)
            resp.raise_for_status()
            jsdata = resp.json()
            for s in jsdata.get('sources', []):
                results.append({"label": s.get("label","node"), "url": s.get("url")})
        except Exception as e:
            logger.debug("Node resolver failed: %s", e)
        # ultima risorsa: prova a risolver la pagina stessa con heuristics
        if not results:
            results.extend(try_resolve_token_endpoint(page_url))
    seen=set(); uniq=[]
    for r in results:
        u=r["url"]
        if u in seen: continue
        seen.add(u); uniq.append(r)
    return uniq

# stato semplice in-memory per conversazioni (season/episode flow)
CONTEXT_WAITING = {}  # user_id -> {"tmdb": {...}}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ciao! Usa /guarda <titolo> per cercare e riprodurre via vixsrc.to")

async def guarda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user_allowed(user.id):
        await update.message.reply_text("Non autorizzato.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /guarda <titolo>")
        return
    title = " ".join(context.args).strip()
    msg = await update.message.reply_text(f"Ricerca su TMDB per: {title} ...")
    info = tmdb_search(title)
    if not info:
        await msg.edit_text("Nessun risultato TMDB trovato.")
        return
    if info["media_type"] == "tv":
        CONTEXT_WAITING[user.id] = {"tmdb": info}
        await msg.edit_text(f"Trovata serie: *{info['title']}*\nInserisci stagione e episodio separati da spazio (es: 1 1)", parse_mode="Markdown")
        return
    vurl = vix_movie_url(info["id"])
    await msg.edit_text(f"Costruisco embed per film: {vurl}\nEstraggo sorgenti...")
    sources = extract_video_sources_from_page(vurl)
    if not sources:
        await msg.edit_text("Nessuna sorgente trovata su vixsrc.to per questo film.")
        return
    await present_sources_and_play(update.message, context, sources, info)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user_allowed(user.id):
        return
    text = (update.message.text or "").strip()
    if user.id in CONTEXT_WAITING:
        data = CONTEXT_WAITING.pop(user.id)
        tmdb_info = data.get("tmdb")
        m = re.match(r"^\s*(\d+)\s+(\d+)\s*$", text)
        if not m:
            await update.message.reply_text("Formato non valido. Usa: <stagione> <episodio> (es: 1 1)")
            CONTEXT_WAITING[user.id] = data
            return
        season = int(m.group(1)); episode = int(m.group(2))
        page_url = vix_tv_url(tmdb_info["id"], season, episode)
        msg = await update.message.reply_text(f"Costruisco embed per: {page_url}\nEstraggo sorgenti...")
        sources = extract_video_sources_from_page(page_url)
        if not sources:
            await msg.edit_text("Nessuna sorgente trovata su vixsrc.to per questo episodio.")
            return
        await present_sources_and_play(update.message, context, sources, tmdb_info, season, episode)
    else:
        await update.message.reply_text("Per cercare usa /guarda <titolo>")

async def present_sources_and_play(message, context: ContextTypes.DEFAULT_TYPE, sources: List[Dict], tmdb_info: Optional[Dict]=None, season: Optional[int]=None, episode: Optional[int]=None):
    chat = message.chat
    keyboard = []
    for s in sources[:12]:
        label = s.get("label") or s.get("url")
        short = (label[:36] + "...") if len(label) > 39 else label
        payload = "play:" + urllib.parse.quote_plus(s["url"])
        if tmdb_info:
            payload += ":" + urllib.parse.quote_plus(tmdb_info["title"])
        keyboard.append([InlineKeyboardButton(short, callback_data=payload)])
    if sources:
        keyboard.append([InlineKeyboardButton("Apri pagina vixsrc", url=sources[0]["url"])])
    reply = InlineKeyboardMarkup(keyboard)
    text = "Seleziona sorgente da riprodurre:"
    if tmdb_info:
        text = f"{tmdb_info['title']}\n\n{tmdb_info.get('overview','')[:300]}\n\n{text}"
        try:
            if tmdb_info.get("poster"):
                await context.bot.send_photo(chat_id=chat.id, photo=tmdb_info["poster"], caption=text, parse_mode="Markdown")
                await context.bot.send_message(chat_id=chat.id, text="Scegli sorgente:", reply_markup=reply)
                return
        except Exception:
            pass
    await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=reply)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("play:"):
        await query.edit_message_text("Callback non riconosciuto.")
        return
    parts = data.split(":", 2)
    video_url = urllib.parse.unquote_plus(parts[1]) if len(parts) > 1 else None
    page_title = urllib.parse.unquote_plus(parts[2]) if len(parts) > 2 else None
    if not video_url:
        await query.edit_message_text("URL video non valido.")
        return
    final_url = video_url
    if re.search(r"/(sources|token|stream|ajax)/", video_url):
        resolved = try_resolve_token_endpoint(video_url)
        if resolved:
            final_url = resolved[0]["url"]
    chat_id = query.message.chat_id
    tmdb_info = tmdb_search(page_title) if page_title else None
    caption_lines = []
    if tmdb_info:
        caption_lines.append(f"*{tmdb_info['title']}*")
        if tmdb_info.get("overview"):
            caption_lines.append(tmdb_info["overview"])
    elif page_title:
        caption_lines.append(page_title)
    caption = "\n\n".join(caption_lines) if caption_lines else None
    try:
        if tmdb_info and tmdb_info.get("poster"):
            await context.bot.send_photo(chat_id=chat_id, photo=tmdb_info["poster"], caption=caption, parse_mode="Markdown")
        else:
            if caption:
                await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="Markdown")
    except Exception:
        pass
    try:
        if re.search(r"\.m3u8(\?|$)", final_url):
            await context.bot.send_message(chat_id=chat_id, text=f"Link HLS: {final_url}")
        else:
            await context.bot.send_video(chat_id=chat_id, video=final_url, caption=f"Riproduzione: {page_title or ''}")
    except Exception as e:
        logger.exception("Invio video fallito: %s", e)
        await context.bot.send_message(chat_id=chat_id, text=f"Impossibile inviare il video direttamente. Ecco il link:\n{final_url}")
    try:
        await query.edit_message_text("Riproduzione inviata.")
    except Exception:
        pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Unhandled exception", exc_info=context.error)

def main():
    if not BOT_TOKEN or "INSERISCI" in BOT_TOKEN:
        print("Imposta BOT_TOKEN nello script.")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("guarda", guarda_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)
    print("Bot avviato...")
    app.run_polling()

if __name__ == "__main__":
    main()
