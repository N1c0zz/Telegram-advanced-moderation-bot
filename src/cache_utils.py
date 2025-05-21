```python
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any

class MessageCache:
    """
    Mantiene una cache di messaggi recenti per utente e chat,
    utile per il rilevamento di "primi N messaggi" e pulizia cross-gruppo.
    """
    def __init__(self, max_hours: int = 3):
        """
        :param max_hours: Numero massimo di ore per cui mantenere i messaggi in cache.
        """
        self.messages: Dict[int, Dict[int, List[Tuple[int, str, datetime]]]] = {}  # chat_id -> {user_id -> [(msg_id, text, timestamp)]}
        self.max_hours = max_hours

    def add_message(self, chat_id: int, user_id: int, message_id: int, message_text: str):
        """Aggiunge un messaggio alla cache."""
        if chat_id not in self.messages:
            self.messages[chat_id] = {}
        if user_id not in self.messages[chat_id]:
            self.messages[chat_id][user_id] = []

        self.messages[chat_id][user_id].append((message_id, message_text, datetime.now()))
        self._cleanup_user_messages(chat_id, user_id)

    def _cleanup_user_messages(self, chat_id: int, user_id: int):
        """Rimuove i messaggi vecchi per un utente specifico in una chat."""
        if chat_id not in self.messages or user_id not in self.messages[chat_id]:
            return

        cutoff_time = datetime.now() - timedelta(hours=self.max_hours)
        self.messages[chat_id][user_id] = [
            msg_data for msg_data in self.messages[chat_id][user_id] if msg_data[2] >= cutoff_time
        ]

    def get_recent_messages(self, chat_id: int, user_id: int) -> List[Tuple[int, str]]:
        """Restituisce ID e testi dei messaggi recenti di un utente in una chat."""
        if chat_id not in self.messages or user_id not in self.messages[chat_id]:
            return []

        self._cleanup_user_messages(chat_id, user_id)
        return [(msg_id, text) for msg_id, text, _ in self.messages[chat_id][user_id]]

    def get_message_count(self, chat_id: int, user_id: int) -> int:
        """Restituisce il numero di messaggi recenti dell'utente nella chat."""
        if chat_id not in self.messages or user_id not in self.messages[chat_id]:
            return 0
        self._cleanup_user_messages(chat_id, user_id)
        return len(self.messages[chat_id][user_id])

    def is_first_few_messages(self, chat_id: int, user_id: int, threshold: int = 3) -> bool:
        """Controlla se questo è uno dei primi N messaggi dell'utente nella chat."""
        return self.get_message_count(chat_id, user_id) <= threshold

    def cleanup_all_old_data(self):
        """Rimuove tutti i messaggi vecchi dall'intera cache."""
        for chat_id in list(self.messages.keys()):
            for user_id in list(self.messages[chat_id].keys()):
                self._cleanup_user_messages(chat_id, user_id)
                if not self.messages[chat_id][user_id]:
                    del self.messages[chat_id][user_id]
            if not self.messages[chat_id]:
                del self.messages[chat_id]


class MessageAnalysisCache:
    """
    Cache per i risultati dell'analisi dei messaggi (es. da OpenAI)
    per evitare richieste ripetute per messaggi identici.
    """
    def __init__(self, cache_size: int = 1000):
        self.cache: Dict[str, Tuple[bool, bool, bool]] = {}
        self.access_count: Dict[str, int] = {} # Per eventuale policy LRU/LFU
        self.cache_size = cache_size

    def _get_message_hash(self, message: str) -> str:
        """Genera un hash MD5 del messaggio per usarlo come chiave cache."""
        return hashlib.md5(message.encode('utf-8')).hexdigest()

    def get(self, message: str) -> Optional[Tuple[bool, bool, bool]]:
        """Recupera un risultato di analisi dalla cache, se presente."""
        message_hash = self._get_message_hash(message)
        if message_hash in self.cache:
            self.access_count[message_hash] = self.access_count.get(message_hash, 0) + 1
            return self.cache[message_hash]
        return None

    def set(self, message: str, analysis_result: Tuple[bool, bool, bool]):
        """Salva un risultato di analisi nella cache."""
        message_hash = self._get_message_hash(message)

        if len(self.cache) >= self.cache_size:
            # Semplice politica FIFO se la cache è piena, rimuovendo il più vecchio (non tracciato)
            # Per una politica LRU/LFU, bisognerebbe tracciare l'ordine di inserimento o l'uso
            try:
                # Rimuovi un elemento a caso o il più vecchio se si tiene traccia
                # Qui rimuoviamo uno basato sull'ordine di inserimento (dizionari Python 3.7+)
                oldest_key = next(iter(self.cache))
                self.cache.pop(oldest_key, None)
                self.access_count.pop(oldest_key, None)
            except StopIteration: # Cache era vuota, ma si sta riempiendo
                pass


        self.cache[message_hash] = analysis_result
        self.access_count[message_hash] = 0 # Reset/init access count
```