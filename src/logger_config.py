```python
import logging
import os
from logging.handlers import RotatingFileHandler

class LoggingConfigurator:
    """
    Configura il sistema di logging per l'applicazione.
    """
    @staticmethod
    def setup_logging(log_dir: str = 'logs', log_level: int = logging.INFO, disable_console: bool = True) -> logging.Logger:
        """
        Imposta il logging su file con rotazione e opzionalmente su console.

        :param log_dir: Directory per i file di log.
        :param log_level: Livello di logging (es. logging.INFO, logging.DEBUG).
        :param disable_console: Se True, disabilita i log sulla console.
        :return: Istanza del logger configurato.
        """
        os.makedirs(log_dir, exist_ok=True)
        log_filename = os.path.join(log_dir, "moderation_bot.log")

        logger = logging.getLogger("ModerationBot")
        logger.setLevel(log_level)
        logger.propagate = False  # Evita log duplicati se il root logger è configurato altrove

        # Rimuovi handler esistenti per evitare duplicazioni in caso di reinizializzazione
        logger.handlers.clear()

        # Handler per file con rotazione
        file_handler = RotatingFileHandler(
            log_filename,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8"
        )
        file_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(module)s - %(message)s")
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        # Handler per console (solo se non disabilitato)
        if not disable_console:
            console_handler = logging.StreamHandler()
            console_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)

        logger.info("=== Sistema di Logging Inizializzato ===")
        logger.info("I log verranno salvati in: %s", os.path.abspath(log_filename))

        # Riduce verbosità di alcune librerie
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.WARNING)
        logging.getLogger("gspread").setLevel(logging.WARNING)
        # Disabilita i log di root per evitare doppi messaggi se configurato altrove
        # root_logger = logging.getLogger()
        # root_logger.handlers = []


        return logger
```