```python
import os
import sys
import signal
import logging # Importa logging per poter configurare il logger di base prima che il bot lo faccia
from dotenv import load_dotenv

# Importa la classe principale del bot
from bot_core import TelegramModerationBot

# Configurazione base del logging prima che il bot inizializzi il suo logger
# Questo cattura i log iniziali se qualcosa va storto prima della configurazione del logger del bot
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Variabile globale per il bot, per accedervi dal signal_handler
_bot_instance: TelegramModerationBot = None # type: ignore

def main():
    global _bot_instance
    logger.info(f"Applicazione avviata (PID: {os.getpid()})")
    logger.info(f"Versione Python: {sys.version.split()[0]}")
    
    # Carica .env per assicurarsi che le variabili siano disponibili globalmente se necessario
    # anche se il bot lo fa internamente.
    load_dotenv() 
    
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        logger.critical("TELEGRAM_BOT_TOKEN non impostato. Impossibile avviare il bot.")
        sys.exit(1)

    try:
        _bot_instance = TelegramModerationBot()
        _bot_instance.start() # Questo è un ciclo bloccante (run_polling)
    except ValueError as ve: # Es. token mancante gestito in TelegramModerationBot
        logger.critical(f"Errore di configurazione grave: {ve}")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Errore critico non gestito durante l'avvio o l'esecuzione del bot: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Applicazione terminata.")


def signal_handler(signum, frame):
    """Gestisce i segnali di interruzione (Ctrl+C) e terminazione."""
    logger.info(f"Ricevuto segnale {signal.Signals(signum).name}. Arresto pulito in corso...")
    
    global _bot_instance
    if _bot_instance and _bot_instance.application:
        # Se il bot e l'applicazione sono attivi, prova a fermare l'applicazione.
        # Questo dovrebbe permettere a run_polling di terminare e chiamare il blocco finally.
        # Nota: application.stop() è asincrono in PTB v20, ma run_polling dovrebbe gestirlo.
        # Per un arresto più controllato, si potrebbe usare application.stop() e poi attendere.
        # Per ora, ci affidiamo al fatto che KeyboardInterrupt o il segnale faranno terminare run_polling.
        if hasattr(_bot_instance.application, 'stop') and callable(_bot_instance.application.stop):
             _bot_instance.application.stop() # PTB v20
        # _bot_instance.stop() # Chiamato nel finally di _bot_instance.start()
    else:
        # Se il bot non è completamente inizializzato, esci direttamente
        sys.exit(0)


if __name__ == "__main__":
    # Imposta i gestori di segnale
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler) # Segnale di terminazione (es. da systemd o Docker)
    
    main()
```