# Miglioramenti suggeriti per user_counters.py

import json
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional
import logging

class UserMessageCounters:
    """Gestisce i contatori di messaggi per utente/gruppo in un file JSON locale."""
    
    def __init__(self, file_path: str = None, 
                 cleanup_days: int = 90, auto_save_interval: int = 5):
        # Determina il percorso del file se non specificato
        if file_path is None:
            # Ottieni la directory del file corrente (__file__ del modulo)
            current_dir = os.path.dirname(os.path.abspath(__file__))
            # Sali di un livello per uscire dalla cartella src
            repo_root = os.path.dirname(current_dir)
            # Crea il percorso nella cartella data del repo
            file_path = os.path.join(repo_root, "data", "user_message_counts.json")
        
        self.file_path = file_path
        self.cleanup_days = cleanup_days  # Giorni dopo i quali rimuovere utenti inattivi
        self.auto_save_interval = auto_save_interval  # Salva ogni N incrementi
        self.lock = threading.Lock()  
        self.logger = logging.getLogger("UserCounters")
        self._unsaved_changes = 0  # Traccia modifiche non salvate
        
        # Crea directory se non esiste
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        # Carica dati esistenti
        self.data = self._load_data()
        
        # Cleanup iniziale
        self._cleanup_old_users()
    
    def _load_data(self) -> Dict:
        """Carica i dati dal file JSON."""
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Migrazione per compatibilità con versioni precedenti
                    return self._migrate_data_if_needed(data)
            return {}
        except Exception as e:
            self.logger.error(f"Errore caricamento contatori: {e}")
            # Backup del file corrotto
            if os.path.exists(self.file_path):
                backup_path = f"{self.file_path}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                try:
                    os.rename(self.file_path, backup_path)
                    self.logger.info(f"File corrotto salvato come backup: {backup_path}")
                except:
                    pass
            return {}
    
    def _migrate_data_if_needed(self, data: Dict) -> Dict:
        """Migra dati da versioni precedenti se necessario."""
        # Esempio: se in futuro cambiamo il formato, qui possiamo migrare
        return data
    
    def _save_data(self):
        """Salva i dati nel file JSON."""
        try:
            # Salvataggio atomico usando file temporaneo
            temp_path = f"{self.file_path}.tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            
            # Sposta il file temporaneo su quello finale (operazione atomica)
            os.replace(temp_path, self.file_path)
            self._unsaved_changes = 0
            self.logger.debug(f"Contatori salvati: {len(self.data)} utenti tracciati")
            
        except Exception as e:
            self.logger.error(f"Errore salvataggio contatori: {e}")
            # Rimuovi file temp se esiste
            if os.path.exists(f"{self.file_path}.tmp"):
                try:
                    os.remove(f"{self.file_path}.tmp")
                except:
                    pass
    
    def _cleanup_old_users(self):
        """Rimuove utenti inattivi da troppo tempo."""
        if self.cleanup_days <= 0:
            return
            
        cutoff_date = datetime.now() - timedelta(days=self.cleanup_days)
        cutoff_iso = cutoff_date.isoformat()
        
        keys_to_remove = []
        for key, data in self.data.items():
            last_updated = data.get('last_updated', '1970-01-01T00:00:00')
            if last_updated < cutoff_iso:
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self.data[key]
        
        if keys_to_remove:
            self.logger.info(f"Rimossi {len(keys_to_remove)} utenti inattivi da oltre {self.cleanup_days} giorni")
            self._save_data()
    
    def increment_and_get_count(self, user_id: int, chat_id: int) -> int:
        """
        Incrementa il contatore dell'utente e restituisce il nuovo totale.
        Thread-safe.
        """
        with self.lock:
            # Chiave composta: "user_id:chat_id"
            key = f"{user_id}:{chat_id}"
            
            if key not in self.data:
                # Primo messaggio
                self.data[key] = {
                    'count': 1,
                    'first_message_date': datetime.now().isoformat(),
                    'last_updated': datetime.now().isoformat()
                }
                new_count = 1
            else:
                # Incrementa esistente
                self.data[key]['count'] += 1
                self.data[key]['last_updated'] = datetime.now().isoformat()
                new_count = self.data[key]['count']
            
            self._unsaved_changes += 1
            
            # Salvataggio più frequente per primi messaggi (critici per ban logic)
            should_save = (
                new_count <= 5 or  # Primi 5 messaggi sempre salvati
                self._unsaved_changes >= self.auto_save_interval or  # Ogni N modifiche
                new_count % 25 == 0  # Milestone ogni 25 messaggi
            )
            
            if should_save:
                self._save_data()
            
            return new_count
    
    def get_count(self, user_id: int, chat_id: int) -> int:
        """Restituisce il contatore attuale senza incrementare."""
        with self.lock:
            key = f"{user_id}:{chat_id}"
            return self.data.get(key, {}).get('count', 0)
    
    def get_user_info(self, user_id: int, chat_id: int) -> Optional[Dict]:
        """Restituisce informazioni complete sull'utente."""
        with self.lock:
            key = f"{user_id}:{chat_id}"
            return self.data.get(key)
    
    def is_new_user(self, user_id: int, chat_id: int, threshold: int = 3) -> bool:
        """Verifica se l'utente è considerato 'nuovo' (sotto la soglia)."""
        return self.get_count(user_id, chat_id) <= threshold
    
    def force_save(self):
        """Forza il salvataggio (utile per shutdown)."""
        with self.lock:
            if self._unsaved_changes > 0:
                self._save_data()
                self.logger.info("Salvataggio forzato dei contatori completato")
    
    def get_stats(self) -> Dict:
        """Restituisce statistiche sui contatori."""
        with self.lock:
            if not self.data:
                return {
                    'total_tracked_users': 0,
                    'first_time_users': 0,
                    'veteran_users': 0,
                    'unsaved_changes': 0
                }
            
            total_users = len(self.data)
            first_time_users = sum(1 for entry in self.data.values() if entry['count'] <= 3)
            
            return {
                'total_tracked_users': total_users,
                'first_time_users': first_time_users,
                'veteran_users': total_users - first_time_users,
                'unsaved_changes': self._unsaved_changes,
                'total_messages_tracked': sum(entry['count'] for entry in self.data.values())
            }
    
    def cleanup_now(self):
        """Esegue pulizia manuale dei dati vecchi."""
        with self.lock:
            old_count = len(self.data)
            self._cleanup_old_users()
            new_count = len(self.data)
            return old_count - new_count  # Numero di utenti rimossi