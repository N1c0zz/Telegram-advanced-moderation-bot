```markdown
# Telegram Advanced Moderation Bot

Questo bot Telegram offre funzionalità avanzate di moderazione per gruppi, tra cui:
- Filtro di parole/frasi bannate.
- Analisi del contenuto tramite OpenAI per rilevare spam, vendite, linguaggio inappropriato.
- Rilevamento della lingua e restrizione a lingue consentite.
- Ban automatico per violazioni ripetute o gravi.
- Night Mode per chiudere automaticamente i gruppi durante la notte.
- Backup dei log di moderazione su Google Sheets.
- Rilevamento di spam cross-gruppo.
- Gestione dei messaggi modificati.

## Struttura del Progetto

```
telegram-advanced-moderation-bot/
├── .env                      # Variabili d'ambiente
├── .gitignore
├── requirements.txt
├── config/
│   ├── config.json           # Configurazione principale
│   └── credentials.json      # Credenziali Google (opzionale)
├── logs/
├── backups/
├── src/                      # Codice sorgente
│   ├── __init__.py
│   ├── main.py               # Entry point
│   ├── bot_core.py           # Logica principale del bot Telegram
│   ├── config_manager.py
│   ├── logger_config.py
│   ├── sheets_interface.py   # Interfaccia Google Sheets
│   ├── backup_handler.py
│   ├── moderation_rules.py   # Logica di analisi dei messaggi
│   ├── cache_utils.py        # Utilità di caching
│   └── spam_detection.py     # Rilevamento spam cross-gruppo
└── README.md
```

## Setup

1.  **Clona il repository:**
    ```bash
    git clone <url_del_tuo_repository>
    cd telegram-advanced-moderation-bot
    ```

2.  **Crea un ambiente virtuale (consigliato):**
    ```bash
    python -m venv venv
    source venv/bin/activate  # Su Windows: venv\Scripts\activate
    ```

3.  **Installa le dipendenze:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configura le variabili d'ambiente:**
    Crea un file `.env` nella root del progetto con il seguente contenuto:
    ```env
    TELEGRAM_BOT_TOKEN="IL_TUO_TOKEN_TELEGRAM"
    OPENAI_API_KEY="LA_TUA_CHIAVE_API_OPENAI"
    # GOOGLE_APPLICATION_CREDENTIALS="config/credentials.json" # Imposta se vuoi che gspread trovi le credenziali qui
    ```
    Sostituisci con i tuoi valori effettivi.

5.  **Configura il bot:**
    Modifica il file `config/config.json`. Puoi partire dalla struttura di esempio fornita.
    - `banned_words`: Lista di parole e frasi da bloccare.
    - `exempt_users`: Lista di ID utente o username Telegram che sono esenti dalla moderazione.
    - `google_sheet_id`: (Opzionale) ID del foglio Google Sheets se ne hai uno esistente. Se vuoto, ne verrà creato uno nuovo.
    - `google_credentials_file`: Percorso al file JSON delle credenziali del service account Google (default: `config/credentials.json`). Se usi Google Sheets, scarica questo file dalla console Google Cloud e mettilo in `config/`. **NON AGGIUNGERE `credentials.json` A GIT SE CONTIENE SEGRETI SENZA AVERLO PRIMA MESSO IN `.gitignore`.**
    - `share_email`: Email con cui condividere il foglio Google Sheets creato/aperto.
    - `night_mode`: Configurazione per la modalità notturna (orari, gruppi, messaggi).

6.  **Avvia il bot:**
    ```bash
    python src/main.py
    ```

## Funzionalità Principali

-   **`/stats`**: (Admin) Mostra statistiche di moderazione.
-   **`/backup`**: (Admin) Esegue un backup manuale dei dati su Google Sheets (se configurato).
-   **`/nighton`**: (Admin) Attiva manualmente la Night Mode nel gruppo corrente.
-   **`/nightoff`**: (Admin) Disattiva manualmente la Night Mode nel gruppo corrente.
-   **`/nightonall`**: (Admin) Attiva manualmente la Night Mode in tutti i gruppi configurati.
-   **`/nightoffall`**: (Admin) Disattiva manualmente la Night Mode in tutti i gruppi configurati.

## Note sulla Configurazione di Google Sheets

Per usare l'integrazione con Google Sheets:
1.  Crea un progetto su [Google Cloud Console](https://console.cloud.google.com/).
2.  Abilita le API "Google Drive API" e "Google Sheets API".
3.  Crea un Service Account:
    - Vai su "IAM & Admin" -> "Service Accounts".
    - Clicca "Create Service Account".
    - Dagli un nome, es. "telegram-bot-sheets".
    - Concedi il ruolo "Editor" (o più granulare se preferisci).
    - Clicca "Done".
    - Trova il service account creato, clicca sui tre puntini -> "Manage keys".
    - "Add Key" -> "Create new key" -> Seleziona "JSON" e clicca "Create".
    - Un file JSON verrà scaricato. Rinominalo (es. `credentials.json`) e mettilo nella directory `config/`.
4.  Nel file `config.json`, imposta `google_credentials_file` al percorso corretto (es. `config/credentials.json`).
5.  Se il bot crea un nuovo foglio, prenderà l'indirizzo email del service account (dal file JSON, campo `client_email`) e lo userà per creare il foglio. Dovrai poi condividere manualmente questo foglio con il tuo account Google personale se vuoi accedervi direttamente dall'interfaccia di Sheets, oppure puoi specificare `share_email` in `config.json` per farlo fare al bot.

## Contribuire

Pull request sono benvenute. Per modifiche major, apri prima una issue per discutere cosa vorresti cambiare.

## Licenza