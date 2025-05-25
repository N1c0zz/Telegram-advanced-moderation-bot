import logging
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional

try:
    import Levenshtein
    LEVENSHTEIN_AVAILABLE = True
except ImportError:
    LEVENSHTEIN_AVAILABLE = False
    logging.warning("Libreria Levenshtein non trovata. La similarità dei messaggi non funzionerà.")


class CrossGroupSpamDetector:
    """
    Rileva se un utente invia messaggi molto simili attraverso più gruppi
    in una finestra temporale ristretta.
    """
    def __init__(self,
                 time_window_hours: int = 1,
                 similarity_threshold: float = 0.85,
                 min_groups: int = 2,
                 logger: Optional[logging.Logger] = None):
        self.time_window_hours = time_window_hours
        self.similarity_threshold = similarity_threshold
        self.min_groups = min_groups
        # user_id -> [(timestamp, messaggio, chat_id)]
        self.user_messages: Dict[int, List[Tuple[datetime, str, int]]] = {}
        self.logger = logger or logging.getLogger(__name__)

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """Calcola la similarità tra due testi usando la distanza di Levenshtein normalizzata."""
        if not LEVENSHTEIN_AVAILABLE:
            return 0.0 # Non possiamo calcolare la similarità

        text1_norm = text1.lower().strip()
        text2_norm = text2.lower().strip()

        max_len = max(len(text1_norm), len(text2_norm))
        if max_len == 0:
            return 1.0  # Entrambi vuoti sono considerati identici

        distance = Levenshtein.distance(text1_norm, text2_norm)
        similarity = 1.0 - (distance / max_len)
        return similarity

    def add_message(self, user_id: int, message_text: str, chat_id: int) -> Tuple[bool, List[int], float]:
        """
        Aggiunge un messaggio e controlla se l'attività è sospetta.
        Restituisce: (is_suspicious, list_of_involved_chat_ids, max_similarity_found)
        """
        current_time = datetime.now()

        if user_id not in self.user_messages:
            self.user_messages[user_id] = []

        self.user_messages[user_id].append((current_time, message_text, chat_id))

        # Pulisci messaggi vecchi
        cutoff_time = current_time - timedelta(hours=self.time_window_hours)
        self.user_messages[user_id] = [
            msg_data for msg_data in self.user_messages[user_id] if msg_data[0] >= cutoff_time
        ]

        return self.check_suspicious_activity(user_id)

    def check_suspicious_activity(self, user_id: int) -> Tuple[bool, List[int], float]:
        """
        Controlla se l'utente ha inviato messaggi simili in gruppi diversi.
        Restituisce (is_suspicious, groups_involved, max_similarity_score).
        """
        if not LEVENSHTEIN_AVAILABLE: # Se Levenshtein non è disponibile, non possiamo fare il check
             return False, [], 0.0

        if user_id not in self.user_messages or len(self.user_messages[user_id]) < self.min_groups:
            return False, [], 0.0 # Non abbastanza messaggi o gruppi per essere sospetto

        messages = self.user_messages[user_id]
        
        # Raggruppa messaggi per chat_id, tenendo solo il più recente per ogni chat
        latest_chat_messages: Dict[int, Tuple[datetime, str]] = {}
        for timestamp, msg_text, cid in messages:
            if cid not in latest_chat_messages or timestamp > latest_chat_messages[cid][0]:
                latest_chat_messages[cid] = (timestamp, msg_text)
        
        if len(latest_chat_messages) < self.min_groups:
            return False, [], 0.0 # Non ha scritto in abbastanza gruppi diversi

        suspicious_groups = set()
        max_similarity = 0.0
        
        chat_ids = list(latest_chat_messages.keys())
        
        for i in range(len(chat_ids)):
            for j in range(i + 1, len(chat_ids)):
                chat_id1 = chat_ids[i]
                chat_id2 = chat_ids[j]
                
                msg1_text = latest_chat_messages[chat_id1][1]
                msg2_text = latest_chat_messages[chat_id2][1]
                
                similarity = self._calculate_similarity(msg1_text, msg2_text)
                max_similarity = max(max_similarity, similarity)

                if similarity >= self.similarity_threshold:
                    suspicious_groups.add(chat_id1)
                    suspicious_groups.add(chat_id2)
                    if self.logger:
                        self.logger.info(
                            f"Alta similarità ({similarity:.2f}) rilevata per utente {user_id} "
                            f"tra gruppi {chat_id1} e {chat_id2}. Msg1: '{msg1_text[:30]}...', Msg2: '{msg2_text[:30]}...'"
                        )
        
        if len(suspicious_groups) >= self.min_groups:
            return True, list(suspicious_groups), max_similarity
        
        return False, [], max_similarity

    def cleanup_old_data(self):
        """Rimuove dati utente più vecchi della finestra temporale definita."""
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(hours=self.time_window_hours)
        
        for user_id in list(self.user_messages.keys()):
            self.user_messages[user_id] = [
                msg_data for msg_data in self.user_messages[user_id] if msg_data[0] >= cutoff_time
            ]
            if not self.user_messages[user_id]:
                del self.user_messages[user_id]
        if self.logger:
            self.logger.debug("Dati vecchi del CrossGroupSpamDetector puliti.")