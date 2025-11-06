Telegram Downloader Bot (TMDB → VixSRC → yt-dlp + ffmpeg)

Bot Telegram per cercare film/serie su TMDB, estrarre manifest da vixsrc.to, scegliere qualità/audio/sottotitoli (sequenziale), scaricare con yt-dlp, post-processare con ffmpeg e inviare il video in chat.

Requisiti
- Python 3.9+ (consigliato 3.10/3.11/3.12)
- ffmpeg (e ffprobe) nel PATH
- Connessione internet stabile
- Account Telegram + bot creato via @BotFather (token)

File principali
- run.py (o botdownload.py): script del bot
- requirements.txt: dipendenze Python

Installazione (Linux / Termux / macOS)
1. Clona o copia i file nel server / device.
2. Crea e attiva un virtualenv (consigliato):
   `bash
   python -m venv venv
   source venv/bin/activate
   `
3. Installa dipendenze:
   `bash
   pip install -r requirements.txt
   `
4. Assicurati che ffmpeg e ffprobe siano installati e raggiungibili:
   `bash
   ffmpeg -version
   ffprobe -version
   `

Configurazione
È preferibile fornire il token via variabile d'ambiente per non lasciare il token hard-coded.

1. Esporta la variabile d'ambiente (Linux / Termux / macOS):
   `bash
   export TELEGRAMBOTTOKEN="123456:ABC-DEF..."
   `
2. Se usi lo script con token interno, modifica la variabile BOT_TOKEN in cima a run.py (meno sicuro).

Altri parametri utili:
- TMDB API key: lo script usa già una chiave incorporata; per stabilità puoi impostarne una tua e modificarla in TMDBAPIKEY.

Avvio
Avvia in foreground per vedere i log:
`bash
python run.py --bot
`

Avvio in background (esempio nohup):
`bash
nohup python run.py --bot > bot.log 2>&1 &
tail -f bot.log
`

Esempio con tmux:
`bash
tmux new -s bot
python run.py --bot

per staccare: Ctrl-b d
`

Uso rapido (utente)
1. Apri la chat del tuo bot su Telegram e invia /start.
2. Avvia ricerche con:
   - Film: /search movie <titolo>
   - Serie: /search tv <titolo>
3. Segui i pulsanti inline:
   - Seleziona risultato TMDB
   - Per serie scegli modalità: singolo / intervallo / stagione / serie completa
   - Il bot mostrerà sequenzialmente: qualità → audio → sottotitoli → conferma
   - Premi “Avvia download” per eseguire la procedura
4. Il bot scarica, post-elabora e invia il file in chat (se < 2 GB)

Opzioni e comportamenti
- Le scelte di qualità/audio/sottotitoli vengono richieste una sola volta per batch (intervallo/stagione/serie completa).
- Hardcode sottotitoli (burn-in) richiede che ffmpeg sia compilato con supporto per il filtro subtitles (libass).
- Telegram rifiuta file > 2 GB; per file grandi lo script salva localmente e notifica il percorso.

Debug e troubleshooting
- Token non valido: se il bot mostra InvalidToken o 401 Unauthorized, verifica token con:
  `bash
  curl -s https://api.telegram.org/bot<TOKEN>/getMe
  `
  Se ricevi 401 rigenera token con @BotFather.
- Problemi di rete: prova un ping verso api.telegram.org.
- Permessi file: assicurati che la cartella video/ sia scrivibile dall'utente che esegue il bot.
- Spazio disco: controlla df -h prima di scaricare pacchetti grandi.
- Logs: se avviato con nohup, leggi bot.log. Per avvio normale, guarda l'output della shell.

Sicurezza
- Non condividere il token pubblicamente.
- Usa variabile d'ambiente per il token in produzione.
- Pulisci periodicamente la cartella video/ per non esporre file sul server.

Miglioramenti consigliati
- Spostare i download pesanti in worker (es. Celery / subprocess con coda) per non bloccare il loop del bot.
- Implementare upload chunked / progressiva per inviare file grandi.
- Aggiungere autenticazione (lista di utenti autorizzati) se il bot è su server pubblico.
- Gestire limiti di disco/timeout e riprese automatiche dopo errori.
