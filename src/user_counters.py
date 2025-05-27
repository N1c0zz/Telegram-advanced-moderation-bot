import json
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional
import hashlib
import logging

class UserMessageCounters:
    """Gestisce i contatori di messaggi per utente/gruppo in un file JSON locale."""
    
    def __init__(self, file_path: str = None, 
             cleanup_days: int = 90, auto_save_interval: int = 5,
             integrity_check: bool = True, 
             logger: Optional[logging.Logger] = None):  # NUOVO parametro
        # Determina il percorso del file se non specificato
        if file_path is None:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            repo_root = os.path.dirname(current_dir)
            file_path = os.path.join(repo_root, "data", "user_message_counts.json")
        
        self.file_path = file_path
        self.backup_path = f"{file_path}.backup"
        self.checksum_path = f"{file_path}.checksum"
        self.cleanup_days = cleanup_days
        self.auto_save_interval = auto_save_interval
        self.integrity_check = integrity_check
        self.lock = threading.Lock()  
        
        # CORREZIONE: Usa il logger passato dal bot, non uno nuovo
        self.logger = logger if logger is not None else logging.getLogger("UserCounters")
        
        self._unsaved_changes = 0
        
        # Crea directory se non esiste
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        # Controllo integrità all'avvio (ora usa il logger corretto)
        self._startup_integrity_check()
        
        # Carica dati esistenti
        self.data = self._load_data()
        
        # Log stato all'avvio
        self._log_startup_status()
        
        # Cleanup iniziale
        self._cleanup_old_users()

    def _calculate_checksum(self, file_path: str) -> str:
        """NUOVO: Calcola checksum MD5 del file."""
        try:
            with open(file_path, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception:
            return ""
    
    def _save_checksum(self, checksum: str):
        """NUOVO: Salva checksum in file separato."""
        try:
            with open(self.checksum_path, 'w') as f:
                f.write(f"{checksum}\n{datetime.now().isoformat()}")
        except Exception as e:
            self.logger.warning(f"Impossibile salvare checksum: {e}")
    
    def _load_checksum(self) -> Tuple[str, str]:
        """NUOVO: Carica checksum salvato. Restituisce (checksum, timestamp)."""
        try:
            if os.path.exists(self.checksum_path):
                with open(self.checksum_path, 'r') as f:
                    lines = f.read().strip().split('\n')
                    return lines[0], lines[1] if len(lines) > 1 else ""
            return "", ""
        except Exception:
            return "", ""
    
    def _startup_integrity_check(self):
        """NUOVO: Controllo integrità completo all'avvio."""
        if not self.integrity_check:
            return
            
        file_exists = os.path.exists(self.file_path)
        backup_exists = os.path.exists(self.backup_path)
        
        # LOGGING CRITICO: Stato dei file all'avvio
        self.logger.info("=== CONTROLLO INTEGRITÀ FILE CONTATORI ALL'AVVIO ===")
        self.logger.info(f"📁 File principale: {self.file_path}")
        self.logger.info(f"   ✅ Esiste: {file_exists}")
        
        if file_exists:
            try:
                file_size = os.path.getsize(self.file_path)
                file_mtime = datetime.fromtimestamp(os.path.getmtime(self.file_path))
                current_checksum = self._calculate_checksum(self.file_path)
                saved_checksum, checksum_time = self._load_checksum()
                
                self.logger.info(f"   📊 Dimensione: {file_size} bytes")
                self.logger.info(f"   🕒 Ultima modifica: {file_mtime}")
                self.logger.info(f"   🛡️ Checksum corrente: {current_checksum}")
                
                if saved_checksum:
                    if current_checksum == saved_checksum:
                        self.logger.info(f"   ✅ Integrità VERIFICATA (checksum corrispondente)")
                    else:
                        self.logger.warning(f"   ⚠️ ATTENZIONE: Checksum NON corrisponde!")
                        self.logger.warning(f"      Salvato: {saved_checksum} ({checksum_time})")
                        self.logger.warning(f"      Attuale: {current_checksum}")
                        self.logger.warning(f"   📁 Il file potrebbe essere stato modificato o corrotto!")
                else:
                    self.logger.info(f"   ℹ️ Nessun checksum precedente trovato (normale al primo avvio)")
                
                # Test di lettura
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    test_data = json.load(f)
                    self.logger.info(f"   ✅ File JSON valido ({len(test_data)} entries)")
                    
            except json.JSONDecodeError as e:
                self.logger.error(f"   ❌ ERRORE: File JSON corrotto! {e}")
                self._handle_corrupted_file()
            except Exception as e:
                self.logger.error(f"   ❌ ERRORE durante controllo integrità: {e}")
        else:
            self.logger.info(f"   ℹ️ File non esiste (normale al primo avvio)")
        
        # Controllo backup
        self.logger.info(f"📁 File backup: {backup_exists}")
        if backup_exists:
            try:
                backup_size = os.path.getsize(self.backup_path)
                backup_mtime = datetime.fromtimestamp(os.path.getmtime(self.backup_path))
                self.logger.info(f"   📊 Dimensione backup: {backup_size} bytes")
                self.logger.info(f"   🕒 Data backup: {backup_mtime}")
            except Exception as e:
                self.logger.warning(f"   ⚠️ Errore lettura info backup: {e}")
        
        self.logger.info("=== FINE CONTROLLO INTEGRITÀ ===")
    
    def _handle_corrupted_file(self):
        """NUOVO: Gestisce file corrotti."""
        self.logger.error("🚨 FILE CONTATORI CORROTTO RILEVATO!")
        
        # Crea backup del file corrotto con timestamp
        corrupted_backup = f"{self.file_path}.corrupted_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            os.rename(self.file_path, corrupted_backup)
            self.logger.error(f"   📁 File corrotto salvato come: {corrupted_backup}")
        except Exception as e:
            self.logger.error(f"   ❌ Impossibile salvare backup file corrotto: {e}")
        
        # Prova a ripristinare dal backup
        if os.path.exists(self.backup_path):
            try:
                # Test il backup prima di usarlo
                with open(self.backup_path, 'r', encoding='utf-8') as f:
                    test_backup = json.load(f)
                
                # Ripristina dal backup
                os.rename(self.backup_path, self.file_path)
                self.logger.warning(f"   ✅ RIPRISTINATO dal backup ({len(test_backup)} entries)")
                
            except Exception as e:
                self.logger.error(f"   ❌ Backup anche corrotto o non leggibile: {e}")
                self.logger.error(f"   🆘 CREAZIONE NUOVO FILE VUOTO!")
        else:
            self.logger.error(f"   ❌ Nessun backup disponibile!")
            self.logger.error(f"   🆘 CREAZIONE NUOVO FILE VUOTO!")
    
    def _log_startup_status(self):
        """NUOVO: Log dettagliato dello stato all'avvio."""
        stats = self.get_stats()
        self.logger.info("=== STATO CONTATORI POST-CARICAMENTO ===")
        self.logger.info(f"👥 Utenti tracciati: {stats['total_tracked_users']}")
        self.logger.info(f"🆕 Utenti nuovi (≤3 msg): {stats['first_time_users']}")
        self.logger.info(f"⭐ Utenti veterani (>3 msg): {stats['veteran_users']}")
        self.logger.info(f"📨 Messaggi totali tracciati: {stats.get('total_messages_tracked', 0)}")
        self.logger.info("========================================")
    
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
        """Salva i dati nel file JSON con backup e checksum."""
        try:
            # NUOVO: Crea backup prima di salvare (se file esiste)
            if os.path.exists(self.file_path):
                try:
                    # Mantieni solo il backup più recente
                    if os.path.exists(self.backup_path):
                        os.remove(self.backup_path)
                    os.rename(self.file_path, self.backup_path)
                    self.logger.debug("📁 Backup precedente creato")
                except Exception as e:
                    self.logger.warning(f"Impossibile creare backup: {e}")
            
            # Salvataggio atomico usando file temporaneo
            temp_path = f"{self.file_path}.tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            
            # Sposta il file temporaneo su quello finale (operazione atomica)
            os.replace(temp_path, self.file_path)
            
            # NUOVO: Calcola e salva checksum
            if self.integrity_check:
                new_checksum = self._calculate_checksum(self.file_path)
                self._save_checksum(new_checksum)
                self.logger.debug(f"🛡️ Checksum aggiornato: {new_checksum}")
            
            self._unsaved_changes = 0
            self.logger.debug(f"💾 Contatori salvati: {len(self.data)} utenti tracciati")
            
        except Exception as e:
            self.logger.error(f"❌ ERRORE CRITICO durante salvataggio contatori: {e}")
            
            # NUOVO: Prova a ripristinare dal backup in caso di errore
            if os.path.exists(self.backup_path):
                try:
                    os.rename(self.backup_path, self.file_path)
                    self.logger.warning("⚠️ Ripristinato backup dopo errore salvataggio")
                except Exception as restore_error:
                    self.logger.error(f"❌ Impossibile ripristinare backup: {restore_error}")
            
            # Rimuovi file temp se esiste
            if os.path.exists(f"{self.file_path}.tmp"):
                try:
                    os.remove(f"{self.file_path}.tmp")
                except:
                    pass
            
            # Re-raise l'eccezione per far sapere al chiamante che il salvataggio è fallito
            raise
    
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

    def get_integrity_status(self) -> Dict:
        """NUOVO: Restituisce stato integrità del file."""
        if not os.path.exists(self.file_path):
            return {
                'file_exists': False,
                'integrity_ok': False,
                'message': 'File contatori non esiste'
            }
        
        try:
            current_checksum = self._calculate_checksum(self.file_path)
            saved_checksum, checksum_time = self._load_checksum()
            
            return {
                'file_exists': True,
                'integrity_ok': current_checksum == saved_checksum if saved_checksum else True,
                'current_checksum': current_checksum,
                'saved_checksum': saved_checksum,
                'checksum_time': checksum_time,
                'file_size': os.path.getsize(self.file_path),
                'last_modified': datetime.fromtimestamp(os.path.getmtime(self.file_path)).isoformat()
            }
        except Exception as e:
            return {
                'file_exists': True,
                'integrity_ok': False,
                'error': str(e),
                'message': 'Errore durante controllo integrità'
            }