```python
import json
import logging
import os
from typing import List, Dict, Any

class ConfigManager:
    """
    Gestisce il caricamento e il salvataggio della configurazione del bot.
    """
    def __init__(self, config_path: str = 'config/config.json'):
        self.config_path = config_path
        if not os.path.exists(os.path.dirname(self.config_path)) and os.path.dirname(self.config_path) != '':
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        self.config = self.load_config()

    def load_config(self) -> Dict[str, Any]:
        """Carica la configurazione da file JSON con validazione di base."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            if not isinstance(config.get("banned_words", []), list):
                raise ValueError("Configurazione non valida: banned_words deve essere una lista.")
            if not isinstance(config.get("exempt_users", []), list):
                raise ValueError("Configurazione non valida: exempt_users deve essere una lista.")
            # Aggiungere altre validazioni se necessario

            return config
        except json.JSONDecodeError:
            logging.error("File di configurazione JSON non valido. Utilizzo configurazione predefinita.")
            return self._get_default_config()
        except FileNotFoundError:
            logging.warning("File di configurazione non trovato (%s). Utilizzo configurazione predefinita.", self.config_path)
            return self._get_default_config()
        except ValueError as e:
            logging.error("Errore nella configurazione: %s. Utilizzo configurazione predefinita.", e)
            return self._get_default_config()

    def _get_default_config(self) -> Dict[str, Any]:
        """Restituisce la configurazione predefinita."""
        return {
            "banned_words": [
                "panieriunipegasomercatorum", "unitelematica", "vendo panieri",
                "offro panieri", "panieri a pagamento"
            ],
            "exempt_users": [6232503826, 831728071, 906064950, "PoloLaDotta"],
            "allowed_languages": ["italian"],
            "google_sheet_id": "",
            "google_credentials_file": "config/credentials.json",
            "share_on_startup": False,
            "share_email": "nicomorini25@gmail.com",
            "check_headers": False,
            "backup_interval_days": 7,
            "backup_directory": "backups",
            "first_messages_threshold": 3,
            "admin_notification_user_id": None,
            "night_mode": {
                "start_hour": "23:00",
                "end_hour": "07:00",
                "start_message": "⛔ Il gruppo è attualmente in modalità notturna. L'invio di messaggi è temporaneamente disabilitato fino alle {end_hour}.",
                "end_message": "🔔 La modalità notturna è terminata. L'invio di messaggi è ora nuovamente abilitato.",
                "night_mode_groups": [
                    -1001413883199, -1002203654435, -1002205878265,
                    -1002067796519, -1001575146602, -1001355576572,
                    -1001772950682
                ],
                "enabled": True
            }
        }

    def save_config(self, config: Dict[str, Any]):
        """Salva la configurazione corrente su file JSON."""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except IOError:
            logging.error("Impossibile salvare il file di configurazione su %s", self.config_path)

    def get(self, key: str, default: Any = None) -> Any:
        """Ottiene un valore dalla configurazione, con un fallback."""
        return self.config.get(key, default)

    def get_nested(self, *keys: str, default: Any = None) -> Any:
        """
        Ottiene un valore da una struttura nidificata nella configurazione.
        Esempio: get_nested('night_mode', 'start_hour')
        """
        _config = self.config
        for key in keys:
            if isinstance(_config, dict) and key in _config:
                _config = _config[key]
            else:
                return default
        return _config
```