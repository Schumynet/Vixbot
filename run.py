#!/usr/bin/env python3
"""
run.py - Bot Telegram per ricerca TMDB, estrazione VixSRC, scelta qualità/audio/sottotitoli (sequenziale),
download con yt-dlp + ffmpeg e invio in chat. Supporta singolo episodio, intervallo, stagione e serie completa.

Requisiti:
    pip install python-telegram-bot requests yt-dlp
Assicurati che ffmpeg e ffprobe siano nel PATH.

Avvio:
    python run.py --bot

Nota:
- Telegram non invia file > 2GB.
- Hardcode sottotitoli richiede il supporto subtitles di ffmpeg (libass).
"""

import os
import re
import sys
import json
import time
import logging
import subprocess
import urllib.parse
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------- CONFIG ----------
TMDB_API_KEY = "1e8c9083f94c62dd66fb2105cd7b613b"
VIX_DOMAIN = "vixsrc.to"
BOT_TOKEN = "7665240833:AAHnGk7pT1n-QD35rhoQ9WVtIzJLotCefDUy"
DOWNLOAD_ROOT = os.path.join(os.getcwd(), "video")
logging.basicConfig(level=logging.INFO)

# ---------- Utilities ----------
def http_get(url, **kwargs):
    try:
        return requests.get(url, timeout=12, **kwargs)
    except requests.exceptions.RequestException as e:
        logging.warning("HTTP error for %s : %s", url, e)
        return None

def clean_folder_name(name):
    s = re.sub(r'[\\/:*?"<>|]', "", str(name))
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def ffprobe_streams(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=index,codec_type:stream_tags=language:stream_tags=title",
           "-of", "json", path]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        return []
    try:
        data = json.loads(out.stdout)
        return data.get("streams", [])
    except Exception:
        return []

# ---------- TMDB / VixSRC helpers ----------
def tmdb_search(title, kind):
    endpoint = "movie" if kind == "movie" else "tv"
    q = requests.utils.requote_uri(title)
    url = f"https://api.themoviedb.org/3/search/{endpoint}?api_key={TMDB_API_KEY}&language=it-IT&query={q}"
    r = http_get(url)
    if r is None or r.status_code != 200:
        return []
    return r.json().get("results", []) or []

def get_all_seasons_info(tmdb_id):
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={TMDB_API_KEY}&language=it-IT"
    r = http_get(url)
    if r is None or r.status_code != 200:
        return []
    data = r.json()
    seasons = data.get("seasons", []) or []
    return [s for s in seasons if s.get("season_number", 0) > 0]

def get_tmdb_episodes(tmdb_id, season_number):
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key={TMDB_API_KEY}&language=it-IT"
    r = http_get(url)
    if r is None or r.status_code != 200:
        return []
    return r.json().get("episodes", []) or []

def get_tmdb_title(tmdb_id, kind, season=None, episode=None):
    base = "https://api.themoviedb.org/3"
    if kind == "movie":
        r = http_get(f"{base}/movie/{tmdb_id}?language=it-IT&api_key={TMDB_API_KEY}")
        if r is None or r.status_code != 200:
            return f"movie_{tmdb_id}"
        j = r.json()
        return j.get("title") or j.get("original_title") or f"movie_{tmdb_id}"
    else:
        r1 = http_get(f"{base}/tv/{tmdb_id}?language=it-IT&api_key={TMDB_API_KEY}")
        r2 = http_get(f"{base}/tv/{tmdb_id}/season/{season}/episode/{episode}?language=it-IT&api_key={TMDB_API_KEY}")
        name = "serie_" + str(tmdb_id)
        ep_name = f"Episodio_{episode}"
        if r1 is not None and r1.status_code == 200:
            j1 = r1.json()
            name = j1.get("name") or j1.get("original_name") or name
        if r2 is not None and r2.status_code == 200:
            j2 = r2.json()
            ep_name = j2.get("name") or ep_name
        return f"{name} - S{season}E{episode} - {ep_name}"

def estrai_url(tmdb_id, kind, season=None, episode=None):
    if kind == "movie":
        page_url = f"https://{VIX_DOMAIN}/movie/{tmdb_id}?lang=it"
    else:
        page_url = f"https://{VIX_DOMAIN}/tv/{tmdb_id}/{season}/{episode}/?lang=it"
    r = http_get(page_url, headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://{VIX_DOMAIN}"})
    if r is None or r.status_code != 200:
        return None
    html = r.text
    m = re.search(r"(https?://[^\s'\"<>]+?/playlist/[0-9]+\?[^\s'\"<>]+)", html)
    if m:
        return m.group(1)
    match = re.search(
        r"token':\s*'(?P<token>[^']+)',\s*'expires':\s*'(?P<expires>[^']+)',\s*.*?url:\s*'(?P<url>[^']+)',\s*}\s*window\.canPlayFHD\s*=\s*(?P<fhd>false|true)",
        html, re.DOTALL)
    if not match:
        return None
    token = match.group("token")
    expires = match.group("expires")
    raw_url = match.group("url")
    can_fhd = match.group("fhd")
    parsed = urlparse(raw_url)
    q = parse_qs(parsed.query)
    q["token"] = [token]
    q["expires"] = [expires]
    if can_fhd == "true":
        q["h"] = ["1"]
    new_q = urlencode(q, doseq=True)
    return urlunparse(parsed._replace(query=new_q))

def parse_m3u8_manifest(m3u8_url):
    r = http_get(m3u8_url, headers={"User-Agent": "Mozilla/5.0"})
    if r is None or r.status_code != 200:
        return [], [], []
    lines = r.text.splitlines()
    variants = []
    audios = set()
    subs = set()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            res = re.search(r"RESOLUTION=(\d+x\d+)", line)
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            uri = lines[i + 1] if i + 1 < len(lines) else ""
            variants.append({"uri": uri, "resolution": res.group(1) if res else "N/A", "bandwidth": int(bw.group(1)) if bw else 0})
        elif line.startswith("#EXT-X-MEDIA"):
            if "TYPE=AUDIO" in line:
                m = re.search(r'LANGUAGE="([^"]+)"', line)
                audios.add(m.group(1) if m else "und")
            if "TYPE=SUBTITLES" in line:
                m = re.search(r'LANGUAGE="([^"]+)"', line)
                subs.add(m.group(1) if m else "und")
    return variants, sorted(audios), sorted(subs)

# ---------- Download / mux ----------
def download_best_then_mux(url, title, prefer_audio_lang=None, chosen_sub=None,
                           kind="movie", series_title=None, season=None, episode=None,
                           chosen_variant_uri=None, burn_subs=False):
    if kind == "movie":
        parent_folder = os.path.join(DOWNLOAD_ROOT, "movie", clean_folder_name(series_title or title))
    else:
        series_folder = clean_folder_name(series_title or (title.split(" - ")[0] if title else "serie"))
        season_folder = f"S{season}" if season is not None else "S?"
        parent_folder = os.path.join(DOWNLOAD_ROOT, "TV", series_folder, season_folder)
    os.makedirs(parent_folder, exist_ok=True)

    assembly_folder = os.path.join(parent_folder, f".assem_{clean_folder_name(title)}")
    if os.path.exists(assembly_folder):
        try:
            for f in os.listdir(assembly_folder):
                p = os.path.join(assembly_folder, f)
                if os.path.isfile(p):
                    os.remove(p)
                else:
                    try:
                        os.rmdir(p)
                    except Exception:
                        pass
        except Exception:
            pass
    os.makedirs(assembly_folder, exist_ok=True)

    safe_title = clean_folder_name(title)
    tmp_base = os.path.join(assembly_folder, f"{safe_title}.tmp")
    merged_ext = "mkv"
    merged_file = f"{tmp_base}.{merged_ext}"
    final_file = os.path.join(parent_folder, f"{safe_title}.mp4")

    ytdlp_cmd = ["yt-dlp", "--no-part", "--no-check-certificate", "-o", f"{tmp_base}.%(ext)s", "-f", "bestvideo+bestaudio/best", "--merge-output-format", merged_ext]
    if chosen_variant_uri:
        ytdlp_cmd.append(chosen_variant_uri)
    else:
        ytdlp_cmd.append(url)

    if chosen_sub and isinstance(chosen_sub, dict) and chosen_sub.get("download"):
        lang = chosen_sub.get("lang")
        if lang:
            ytdlp_cmd += ["--write-sub", "--sub-lang", lang, "--sub-format", "srt/best,vtt/best"]
        else:
            ytdlp_cmd += ["--write-sub", "--sub-format", "srt/best,vtt/best"]

    logging.info("Running yt-dlp: %s", " ".join(ytdlp_cmd))
    rc = subprocess.run(ytdlp_cmd).returncode
    if rc != 0:
        logging.error("yt-dlp failed (rc=%s)", rc)
        return None

    if not os.path.exists(merged_file):
        candidates = [f for f in os.listdir(assembly_folder) if f.startswith(os.path.basename(tmp_base) + ".")]
        if not candidates:
            logging.error("No download candidate in assembly folder")
            return None
        merged_file = os.path.join(assembly_folder, candidates[0])

    sub_path = None
    for ext in (".srt", ".vtt"):
        for f in os.listdir(assembly_folder):
            if f.lower().endswith(ext) and f.startswith(os.path.basename(tmp_base)):
                sub_path = os.path.join(assembly_folder, f)
                break
        if sub_path:
            break

    streams = ffprobe_streams(merged_file)
    if not streams:
        logging.warning("ffprobe returned no streams; skipping mux")
        return None

    # basic map selection: choose first audio by prefer_audio_lang if possible
    audio_idx = None
    sub_idx_in_container = None
    for s in streams:
        if s.get("codec_type") == "audio":
            tags = s.get("tags") or {}
            lang = tags.get("language") or tags.get("lang") or ""
            if prefer_audio_lang and lang and lang.lower().startswith(prefer_audio_lang.lower()):
                audio_idx = s.get("index")
                break
            if audio_idx is None:
                audio_idx = s.get("index")
        if s.get("codec_type") == "subtitle" and sub_idx_in_container is None:
            sub_idx_in_container = s.get("index")

    ff_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info", "-i", merged_file]
    if sub_path:
        ff_cmd += ["-i", sub_path]
    maps = ["-map", "0:v:0"]
    if audio_idx is not None:
        maps += ["-map", f"0:{audio_idx}"]
    else:
        maps += ["-map", "0:a:0"]
    if sub_idx_in_container is not None:
        maps += ["-map", f"0:{sub_idx_in_container}"]
    elif sub_path:
        maps += ["-map", f"{1 if sub_path else 0}:0"]
    ff_cmd += maps

    if burn_subs and sub_path:
        ff_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info", "-i", merged_file, "-i", sub_path,
                  "-filter_complex", f"subtitles={sub_path}"]
        ff_cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "copy", final_file]
    else:
        if sub_idx_in_container is not None or sub_path:
            ff_cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text", final_file]
        else:
            ff_cmd += ["-c", "copy", final_file]

    logging.info("Running ffmpeg: %s", " ".join(ff_cmd))
    rc2 = subprocess.run(ff_cmd).returncode
    if rc2 != 0:
        logging.error("ffmpeg failed (rc=%s)", rc2)
        return None

    # cleanup assembly
    try:
        for f in os.listdir(assembly_folder):
            p = os.path.join(assembly_folder, f)
            try:
                os.remove(p)
            except Exception:
                pass
        os.rmdir(assembly_folder)
    except Exception:
        pass

    return final_file

# ---------- BOT logic (sequential option selection) ----------
# user_data holds flow state and options (see below comments)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🔎 Cerca film/serie (TMDB)", callback_data="MENU_SEARCH")],
        [InlineKeyboardButton("ℹ️ Istruzioni", callback_data="MENU_HELP")],
    ]
    await update.message.reply_text("Benvenuto. Scegli un'opzione:", reply_markup=InlineKeyboardMarkup(kb))

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "MENU_SEARCH":
        await q.edit_message_text("Usa il comando /search <movie|tv> <titolo> per cercare. Esempio:\n/search movie Il Padrino")
    else:
        await q.edit_message_text(
            "Flow:\n"
            "1) /search movie|tv <titolo>\n"
            "2) Seleziona risultato TMDB\n"
            "3) (TV) scegli modalità: singolo/intervallo/stagione/serie completa\n"
            "4) Scegli qualità → audio → sottotitoli (sequenziale)\n"
            "5) Avvia download e ricevi file in chat (se <2GB)"
        )

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Uso: /search <movie|tv> <titolo>")
        return
    kind = args[0].lower()
    if kind not in ("movie", "tv"):
        await update.message.reply_text("Primo argomento deve essere movie o tv")
        return
    title = " ".join(args[1:]).strip()
    await update.message.reply_text(f"🔎 Cerco '{title}' su TMDB ({kind})...")
    results = tmdb_search(title, kind)
    if not results:
        await update.message.reply_text("Nessun risultato TMDB.")
        return
    context.user_data.clear()
    context.user_data["step"] = "choose_tmdb"
    context.user_data["kind"] = kind
    context.user_data["tmdb_results"] = results[:8]
    kb = []
    for i, it in enumerate(context.user_data["tmdb_results"]):
        name = it.get("title") or it.get("name") or it.get("original_title") or it.get("original_name") or "Unknown"
        date = it.get("release_date") or it.get("first_air_date") or ""
        kb.append([InlineKeyboardButton(f"{name} ({date})", callback_data=f"TMDB|{i}")])
    kb.append([InlineKeyboardButton("Annulla", callback_data="CANCEL")])
    await update.message.reply_text("Seleziona il risultato:", reply_markup=InlineKeyboardMarkup(kb))

async def callback_tmdb_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "CANCEL":
        context.user_data.clear()
        await q.edit_message_text("Operazione annullata.")
        return
    if not data.startswith("TMDB|"):
        return
    idx = int(data.split("|", 1)[1])
    results = context.user_data.get("tmdb_results") or []
    if idx < 0 or idx >= len(results):
        await q.edit_message_text("Selezione non valida.")
        context.user_data.clear()
        return
    chosen = results[idx]
    tmdb_id = chosen.get("id")
    kind = context.user_data.get("kind", "movie")
    context.user_data["chosen_tmdb_id"] = tmdb_id
    context.user_data["chosen_tmdb_item"] = chosen

    if kind == "movie":
        manifest = estrai_url(tmdb_id, "movie")
        if not manifest:
            await q.edit_message_text("❌ URL VixSRC non trovato per questo film.")
            context.user_data.clear()
            return
        context.user_data["manifest_url"] = manifest
        context.user_data["manifest_example"] = manifest
        context.user_data["kind"] = "movie"
        await present_quality_options(q.message.chat.id, context)
    else:
        seasons = get_all_seasons_info(tmdb_id)
        if not seasons:
            await q.edit_message_text("Nessuna stagione trovata su TMDB.")
            context.user_data.clear()
            return
        kb = [
            [InlineKeyboardButton("Singolo episodio", callback_data="MODE|single")],
            [InlineKeyboardButton("Intervallo episodi", callback_data="MODE|range")],
            [InlineKeyboardButton("Stagione completa", callback_data="MODE|season")],
            [InlineKeyboardButton("Serie completa", callback_data="MODE|all")],
            [InlineKeyboardButton("Annulla", callback_data="CANCEL")],
        ]
        context.user_data["step"] = "choose_dl_mode"
        await q.edit_message_text("Scegli modalità di download per la serie:", reply_markup=InlineKeyboardMarkup(kb))

async def callback_choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "CANCEL":
        context.user_data.clear()
        await q.edit_message_text("Operazione annullata.")
        return
    if not data.startswith("MODE|"):
        return
    mode = data.split("|", 1)[1]
    context.user_data["dl_mode"] = mode
    tmdb_id = context.user_data.get("chosen_tmdb_id")
    if not tmdb_id:
        await q.edit_message_text("Errore: tmdb id mancante.")
        context.user_data.clear()
        return

    seasons = get_all_seasons_info(tmdb_id)
    if not seasons:
        await q.edit_message_text("Nessuna stagione disponibile.")
        context.user_data.clear()
        return

    kb = [[InlineKeyboardButton(f"S{s.get('season_number')}", callback_data=f"SEASON|{s.get('season_number')}")] for s in seasons]
    kb.append([InlineKeyboardButton("Annulla", callback_data="CANCEL")])

    if mode == "single":
        context.user_data["step"] = "choose_season_for_single"
        await q.edit_message_text("Seleziona stagione:", reply_markup=InlineKeyboardMarkup(kb))
    elif mode == "range":
        context.user_data["step"] = "choose_season_for_range"
        await q.edit_message_text("Seleziona stagione per intervallo:", reply_markup=InlineKeyboardMarkup(kb))
    elif mode == "season":
        context.user_data["step"] = "choose_season_for_full"
        await q.edit_message_text("Seleziona stagione da scaricare:", reply_markup=InlineKeyboardMarkup(kb))
    elif mode == "all":
        context.user_data["step"] = "prepare_all"
        await q.edit_message_text("Preparazione lista episodi per la serie completa...")
        await prepare_episode_list_for_all(q, context)

async def callback_season_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "CANCEL":
        context.user_data.clear()
        await q.edit_message_text("Operazione annullata.")
        return
    if not data.startswith("SEASON|"):
        return
    season_num = int(data.split("|", 1)[1])
    context.user_data["season"] = season_num
    step = context.user_data.get("step", "")
    tmdb_id = context.user_data.get("chosen_tmdb_id")
    if not tmdb_id:
        await q.edit_message_text("Errore: tmdb id mancante.")
        context.user_data.clear()
        return
    eps = get_tmdb_episodes(tmdb_id, season_num)
    available = []
    for ep in eps:
        epnum = ep.get("episode_number")
        if epnum is None:
            continue
        manifest = estrai_url(tmdb_id, "tv", season_num, epnum)
        if manifest:
            available.append((epnum, ep.get("name") or f"Episodio {epnum}", manifest))
    if not available:
        await q.edit_message_text("Nessun episodio disponibile per questa stagione su VixSRC.")
        context.user_data.clear()
        return

    if step == "choose_season_for_single":
        kb = [[InlineKeyboardButton(f"S{season_num}E{epnum} - {name}", callback_data=f"EP|{season_num}|{epnum}")] for epnum, name, _ in available]
        kb.append([InlineKeyboardButton("Annulla", callback_data="CANCEL")])
        context.user_data["available_eps"] = available
        context.user_data["step"] = "choose_episode_single"
        await q.edit_message_text("Seleziona episodio:", reply_markup=InlineKeyboardMarkup(kb))
    elif step == "choose_season_for_range":
        context.user_data["available_eps"] = available
        context.user_data["step"] = "choose_range_numbers"
        await q.edit_message_text("Invia due numeri separati da spazio: <start> <end> per l'intervallo di episodi (es. 1 5)")
    elif step == "choose_season_for_full":
        context.user_data["episodes_to_download"] = [epnum for epnum, _, _ in available]
        context.user_data["manifest_example"] = available[0][2]
        context.user_data["kind"] = "tv"
        await present_quality_options(q.message.chat.id, context)
    else:
        await q.edit_message_text("Passo non riconosciuto. Annullo.")
        context.user_data.clear()

async def callback_episode_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("EP|"):
        return
    _, season_str, ep_str = data.split("|")
    season_num = int(season_str)
    ep_num = int(ep_str)
    available = context.user_data.get("available_eps") or []
    manifest = None
    for s, name, u in available:
        if s == ep_num:
            manifest = u
            break
    if not manifest:
        manifest = estrai_url(context.user_data.get("chosen_tmdb_id"), "tv", season_num, ep_num)
    if not manifest:
        await q.edit_message_text("❌ Impossibile estrarre manifest per l'episodio selezionato.")
        context.user_data.clear()
        return
    context.user_data["manifest_url"] = manifest
    context.user_data["manifest_example"] = manifest
    context.user_data["kind"] = "tv"
    context.user_data["episodes_to_download"] = [ep_num]
    context.user_data["season"] = season_num
    await present_quality_options(q.message.chat.id, context)

async def text_handler_for_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    if step != "choose_range_numbers":
        await update.message.reply_text("Non capisco. Usa /search per iniziare.")
        return
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("Formato non valido. Invia: <start> <end>")
        return
    try:
        start = int(parts[0])
        end = int(parts[1])
    except Exception:
        await update.message.reply_text("Numeri non validi.")
        return
    available = context.user_data.get("available_eps") or []
    selected = [epnum for epnum, _, _ in available if start <= epnum <= end]
    if not selected:
        await update.message.reply_text("Nessun episodio nell'intervallo selezionato.")
        context.user_data.clear()
        return
    context.user_data["episodes_to_download"] = selected
    context.user_data["manifest_example"] = available[0][2]
    context.user_data["kind"] = "tv"
    await present_quality_options(update.effective_chat.id, context)

async def prepare_episode_list_for_all(q, context):
    tmdb_id = context.user_data.get("chosen_tmdb_id")
    seasons = get_all_seasons_info(tmdb_id)
    all_eps = []
    for s in seasons:
        sn = s.get("season_number")
        eps = get_tmdb_episodes(tmdb_id, sn)
        for ep in eps:
            epnum = ep.get("episode_number")
            if epnum is None:
                continue
            manifest = estrai_url(tmdb_id, "tv", sn, epnum)
            if manifest:
                all_eps.append((sn, epnum, ep.get("name") or f"E{epnum}", manifest))
    if not all_eps:
        await q.edit_message_text("Nessun episodio reperibile per la serie su VixSRC.")
        context.user_data.clear()
        return
    context.user_data["episodes_to_download"] = [(sn, epnum) for sn, epnum, _, _ in all_eps]
    context.user_data["manifest_example"] = all_eps[0][3]
    context.user_data["kind"] = "tv"
    await present_quality_options(q.message.chat.id, context)

# ---------- Sequential options: quality -> audio -> subs -> confirm ----------
async def present_quality_options(chat_id, context):
    manifest = context.user_data.get("manifest_example") or context.user_data.get("manifest_url")
    if not manifest:
        await context.bot.send_message(chat_id, "Manifest non disponibile.")
        context.user_data.clear()
        return
    await context.bot.send_message(chat_id, "Analizzo manifest per qualità/audio/sottotitoli...")
    variants, audios, subs = parse_m3u8_manifest(manifest)
    context.user_data["variants"] = variants
    context.user_data["audios"] = audios
    context.user_data["subs"] = subs

    kb = []
    if variants:
        for i, v in enumerate(variants):
            label = v.get("resolution") or f"var{i}"
            kb.append([InlineKeyboardButton(f"{label}", callback_data=f"SEQ|VAR|{i}")])
    else:
        kb.append([InlineKeyboardButton("Qualità predefinita", callback_data="SEQ|VAR|-1")])

    context.user_data["step"] = "choose_quality"
    await context.bot.send_message(chat_id, "Scegli la qualità video:", reply_markup=InlineKeyboardMarkup(kb))

async def callback_seq_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    parts = data.split("|")
    if len(parts) != 3:
        await q.edit_message_text("Selezione non valida.")
        return
    idx = int(parts[2])
    if idx >= 0:
        context.user_data["chosen_variant_idx"] = idx
        sel_label = context.user_data["variants"][idx].get("resolution")
        await q.edit_message_text(f"✅ Qualità selezionata: {sel_label}")
    else:
        context.user_data["chosen_variant_idx"] = None
        await q.edit_message_text("✅ Qualità predefinita selezionata")

    audios = context.user_data.get("audios", [])
    kb = []
    if audios:
        for a in audios:
            kb.append([InlineKeyboardButton(a, callback_data=f"SEQ|AUD|{a}")])
    else:
        kb.append([InlineKeyboardButton("Audio predefinito", callback_data="SEQ|AUD|und")])
    context.user_data["step"] = "choose_audio"
    await q.message.reply_text("Scegli la lingua audio:", reply_markup=InlineKeyboardMarkup(kb))

async def callback_seq_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    parts = data.split("|", 2)
    if len(parts) != 3:
        await q.edit_message_text("Selezione non valida.")
        return
    lang = parts[2]
    context.user_data["chosen_audio"] = lang
    await q.edit_message_text(f"✅ Audio selezionato: {lang}")

    subs = context.user_data.get("subs", [])
    kb = []
    if subs:
        for s in subs:
            kb.append([InlineKeyboardButton(s, callback_data=f"SEQ|SUB|{s}")])
        kb.append([InlineKeyboardButton("Nessun sottotitolo", callback_data="SEQ|SUB|NONE")])
    else:
        kb.append([InlineKeyboardButton("Nessun sottotitolo disponibile", callback_data="SEQ|SUB|NONE")])
    context.user_data["step"] = "choose_sub"
    await q.message.reply_text("Scegli la lingua dei sottotitoli (o Nessun sottotitolo):", reply_markup=InlineKeyboardMarkup(kb))

async def callback_seq_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    parts = data.split("|", 2)
    if len(parts) != 3:
        await q.edit_message_text("Selezione non valida.")
        return
    lang = parts[2]
    if lang == "NONE":
        context.user_data["chosen_sub_lang"] = None
        context.user_data["sub_download"] = False
        await q.edit_message_text("✅ Sottotitoli disabilitati")
    else:
        context.user_data["chosen_sub_lang"] = lang
        context.user_data["sub_download"] = True
        await q.edit_message_text(f"✅ Sottotitoli selezionati: {lang} (verranno scaricati)")

    kb = [
        [InlineKeyboardButton("Scarica sottotitoli: SI" if context.user_data.get("sub_download") else "Scarica sottotitoli: NO", callback_data="SEQ|TOG|SUB")],
        [InlineKeyboardButton("Hardcode sottotitoli: NO", callback_data="SEQ|TOG|BURN")],
        [InlineKeyboardButton("✅ Avvia download", callback_data="SEQ|DO|START")],
    ]
    context.user_data["step"] = "confirm_options"
    await q.message.reply_text("Opzioni finali: puoi attivare/disattivare scarica/hardcode, poi Avvia download:", reply_markup=InlineKeyboardMarkup(kb))

async def callback_seq_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    parts = data.split("|")
    if len(parts) < 3:
        await q.edit_message_text("Azione non valida.")
        return
    action = parts[1]
    arg = parts[2]
    if action == "TOG" and arg == "SUB":
        curr = context.user_data.get("sub_download", False)
        context.user_data["sub_download"] = not curr
        state = "SI" if context.user_data["sub_download"] else "NO"
        await q.edit_message_text(f"Scarica sottotitoli: {state}")
        kb = [
            [InlineKeyboardButton("Scarica sottotitoli: SI" if context.user_data.get("sub_download") else "Scarica sottotitoli: NO", callback_data="SEQ|TOG|SUB")],
            [InlineKeyboardButton("Hardcode sottotitoli: SI" if context.user_data.get("sub_burn") else "Hardcode sottotitoli: NO", callback_data="SEQ|TOG|BURN")],
            [InlineKeyboardButton("✅ Avvia download", callback_data="SEQ|DO|START")],
        ]
        await q.message.reply_text("Conferma opzioni:", reply_markup=InlineKeyboardMarkup(kb))
    elif action == "TOG" and arg == "BURN":
        curr = context.user_data.get("sub_burn", False)
        context.user_data["sub_burn"] = not curr
        state = "SI" if context.user_data["sub_burn"] else "NO"
        await q.edit_message_text(f"Hardcode sottotitoli: {state}")
        kb = [
            [InlineKeyboardButton("Scarica sottotitoli: SI" if context.user_data.get("sub_download") else "Scarica sottotitoli: NO", callback_data="SEQ|TOG|SUB")],
            [InlineKeyboardButton("Hardcode sottotitoli: SI" if context.user_data.get("sub_burn") else "Hardcode sottotitoli: NO", callback_data="SEQ|TOG|BURN")],
            [InlineKeyboardButton("✅ Avvia download", callback_data="SEQ|DO|START")],
        ]
        await q.message.reply_text("Conferma opzioni:", reply_markup=InlineKeyboardMarkup(kb))
    elif action == "DO" and arg == "START":
        await q.edit_message_text("⏳ Avvio download con le opzioni selezionate...")
        await run_downloads_and_send(update, context)
    else:
        await q.edit_message_text("Azione non riconosciuta.")

# ---------- Download runner that uses saved options ----------
async def run_downloads_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    ud = context.user_data
    manifest_example = ud.get("manifest_example") or ud.get("manifest_url")
    chosen_variant_uri = None
    if ud.get("chosen_variant_idx") is not None:
        variants = ud.get("variants", [])
        idx = ud.get("chosen_variant_idx")
        if 0 <= idx < len(variants):
            v = variants[idx]
            chosen_variant_uri = v.get("uri")
            if chosen_variant_uri and not chosen_variant_uri.startswith("http"):
                base = manifest_example.rsplit("/", 1)[0] + "/"
                chosen_variant_uri = urllib.parse.urljoin(base, chosen_variant_uri)
    chosen_audio = ud.get("chosen_audio")
    chosen_sub = None
    if ud.get("sub_download") or ud.get("chosen_sub_lang"):
        chosen_sub = {"lang": ud.get("chosen_sub_lang"), "download": True}
    burn = ud.get("sub_burn", False)
    kind = ud.get("kind", "movie")

    episodes_list = []
    if kind == "movie":
        manifest = ud.get("manifest_url") or manifest_example
        title = (ud.get("chosen_tmdb_item") or {}).get("title") or "movie_from_bot"
        episodes_list = [("movie", manifest, title, None, None)]
    else:
        eps = ud.get("episodes_to_download") or []
        if not eps:
            manifest = ud.get("manifest_example")
            title = "tv_from_bot"
            episodes_list = [("tv", manifest, title, ud.get("season"), ud.get("episode"))]
        else:
            if isinstance(eps[0], tuple):
                for sn, epn in eps:
                    manifest = estrai_url(ud.get("chosen_tmdb_id"), "tv", sn, epn)
                    title = get_tmdb_title(ud.get("chosen_tmdb_id"), "tv", sn, epn)
                    episodes_list.append(("tv", manifest, title, sn, epn))
            else:
                season = ud.get("season")
                for epn in eps:
                    manifest = estrai_url(ud.get("chosen_tmdb_id"), "tv", season, epn)
                    title = get_tmdb_title(ud.get("chosen_tmdb_id"), "tv", season, epn)
                    episodes_list.append(("tv", manifest, title, season, epn))

    chat = q.message.chat if q else update.effective_chat
    await chat.send_message(f"⏱️ Inizio download di {len(episodes_list)} item. Opzioni: qualità idx {ud.get('chosen_variant_idx')}, audio {chosen_audio}, subs {bool(chosen_sub)}, burn {burn}")

    sent_any = False
    for idx, (k, manifest_url, title, season, epnum) in enumerate(episodes_list, 1):
        if not manifest_url:
            await chat.send_message(f"⚠️ Manifest non trovato per item {title}, salto.")
            continue
        await chat.send_message(f"⏳ ({idx}/{len(episodes_list)}) Scarico: {title}")
        final_path = download_best_then_mux(manifest_url, title, prefer_audio_lang=chosen_audio,
                                           chosen_sub=chosen_sub, kind="movie" if k == "movie" else "tv",
                                           series_title=title if k == "movie" else title.split(" - ")[0],
                                           season=season, episode=epnum,
                                           chosen_variant_uri=chosen_variant_uri, burn_subs=burn)
        if final_path and os.path.exists(final_path):
            try:
                with open(final_path, "rb") as vf:
                    await chat.send_video(vf)
                sent_any = True
            except Exception as e:
                await chat.send_message(f"Errore invio file: {e}. File salvato in: {final_path}")
        else:
            await chat.send_message(f"❌ Download fallito per: {title}")
        time.sleep(1)

    if sent_any:
        await chat.send_message("✔️ Operazione terminata.")
    else:
        await chat.send_message("⚠️ Nessun file inviato. Controlla i log.")
    context.user_data.clear()

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Comando non riconosciuto. Usa /search per cercare contenuti.")

# ---------- Entrypoint / Bot runner ----------
def run_bot():
    if not BOT_TOKEN or BOT_TOKEN.startswith("INSERISCI") or BOT_TOKEN.strip() == "":
        print("Inserisci il token del bot in BOT_TOKEN prima di avviare.")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^MENU_"))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CallbackQueryHandler(callback_tmdb_choose, pattern=r"^TMDB\|"))
    app.add_handler(CallbackQueryHandler(callback_choose_mode, pattern=r"^MODE\|"))
    app.add_handler(CallbackQueryHandler(callback_season_selected, pattern=r"^SEASON\|"))
    app.add_handler(CallbackQueryHandler(callback_episode_choose, pattern=r"^EP\|"))
    # sequential handlers
    app.add_handler(CallbackQueryHandler(callback_seq_quality, pattern=r"^SEQ\|VAR\|"))
    app.add_handler(CallbackQueryHandler(callback_seq_audio, pattern=r"^SEQ\|AUD\|"))
    app.add_handler(CallbackQueryHandler(callback_seq_sub, pattern=r"^SEQ\|SUB\|"))
    app.add_handler(CallbackQueryHandler(callback_seq_confirm, pattern=r"^SEQ\|"))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler_for_range))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logging.info("Bot avviato.")
    app.run_polling()

if __name__ == "__main__":
    if "--bot" in sys.argv:
        run_bot()
    else:
        print("Avvia il bot con: python run.py --bot")