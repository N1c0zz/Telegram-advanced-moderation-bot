import csv
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional
import threading
import json
import shutil
import tempfile

try:
    from .config_manager import ConfigManager  # Import relativo quando usato come modulo
except ImportError:
    from config_manager import ConfigManager   # Import assoluto per test standalone

class CSVDataManager:
    """
    Gestisce l'interazione con file CSV per salvare dati di moderazione.
    Replica le funzionalità di GoogleSheetsManager ma usando CSV locali.
    Progettato per funzionare in parallelo con Google Sheets.
    
    AGGIORNAMENTO: Aggiunto supporto per unban (rimozione dal CSV banned_users)
    """
    
    def __init__(self, logger: logging.Logger, config_manager: ConfigManager):
        self.logger = logger
        self.config = config_manager.config
        self.config_manager_instance = config_manager
        self.lock = threading.Lock()  # Thread safety per scritture CSV
        
        # Definisce la struttura dei file CSV (identica a Google Sheets)
        self.csv_structure = {
            "messages": {
                "filename": "messages.csv",
                "headers": ["timestamp", "messaggio", "user_id", "username", "chat_id", "group_name", "approvato", "domanda", "motivo_rifiuto"]
            },
            "admin": {
                "filename": "admin_messages.csv", 
                "headers": ["timestamp", "messaggio", "user_id", "username", "chat_id", "group_name"]
            },
            "banned_users": {
                "filename": "banned_users.csv",
                "headers": ["user_id", "timestamp", "motivo"]
            },
            # NUOVO: Tabella per storico unban
            "unban_history": {
                "filename": "unban_history.csv",
                "headers": ["user_id", "original_ban_timestamp", "unban_timestamp", "unban_reason", "unbanned_by"]
            }
        }
        
        # Directory per i CSV (configurabile)
        self.data_dir = self.config.get("csv_data_directory", "data/csv")
        self.backup_dir = self.config.get("csv_backup_directory", "data/csv_backups")
        
        self.auto_backup_enabled = self.config.get("csv_auto_backup_enabled", True)
        self.auto_backup_threshold = self.config.get("csv_auto_backup_row_threshold", 2000)
        self.auto_backup_preserve_headers = self.config.get("csv_auto_backup_preserve_headers", True)

        if self.auto_backup_enabled:
            self.logger.info(f"Backup automatico CSV abilitato: soglia {self.auto_backup_threshold} righe")
        else:
            self.logger.info("Backup automatico CSV disabilitato")

        # Inizializza solo se abilitato in config
        self.enabled = self.config.get("csv_enabled", True)  # Default abilitato
        
        if self.enabled:
            self._initialize_csv_files()
            self._banned_users_cache = None  # Cache per utenti bannati
            self._cache_timestamp = None
            self.logger.info("CSV DataManager inizializzato e abilitato")
        else:
            self.logger.info("CSV DataManager inizializzato ma DISABILITATO")
        
    def _initialize_csv_files(self):
        """Inizializza i file CSV creando le directory e i file con headers se non esistono."""
        try:
            # Crea le directory
            os.makedirs(self.data_dir, exist_ok=True)
            os.makedirs(self.backup_dir, exist_ok=True)
            
            for table_name, structure in self.csv_structure.items():
                file_path = os.path.join(self.data_dir, structure["filename"])
                
                # Se il file non esiste, crealo con gli headers
                if not os.path.exists(file_path):
                    with open(file_path, 'w', newline='', encoding='utf-8') as csvfile:
                        writer = csv.writer(csvfile)
                        writer.writerow(structure["headers"])
                    self.logger.info(f"File CSV creato: {structure['filename']}")
                else:
                    # Verifica che gli headers siano corretti
                    self._verify_csv_headers(file_path, structure["headers"], table_name)
                    
        except Exception as e:
            self.logger.error(f"Errore durante l'inizializzazione dei file CSV: {e}", exc_info=True)
    
    def _verify_csv_headers(self, file_path: str, expected_headers: List[str], table_name: str):
        """Verifica che gli headers del CSV siano corretti."""
        try:
            with open(file_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.reader(csvfile)
                actual_headers = next(reader, [])
                
                if actual_headers != expected_headers:
                    self.logger.warning(
                        f"Headers del file CSV {table_name} non corrispondono. "
                        f"Attesi: {expected_headers}, Trovati: {actual_headers}"
                    )
                    
        except Exception as e:
            self.logger.error(f"Errore nella verifica headers per {table_name}: {e}")
    
    def _append_to_csv(self, table_name: str, data: List[str]) -> bool:
        """Aggiunge una riga a un file CSV in modo thread-safe con backup automatico."""
        if not self.enabled:
            self.logger.debug(f"CSV disabilitato, skip salvataggio su {table_name}")
            return True
            
        if table_name not in self.csv_structure:
            self.logger.error(f"Tabella CSV sconosciuta: {table_name}")
            return False
            
        file_path = os.path.join(self.data_dir, self.csv_structure[table_name]["filename"])
        
        try:
            with self.lock:  # Thread safety
                with open(file_path, 'a', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(data)
            
            # NUOVO: Controlla se è necessario il backup automatico
            # Solo per le tabelle principali che crescono rapidamente
            if table_name in ['messages', 'admin']:  # Tabelle che crescono molto
                self.check_and_auto_backup_if_needed(table_name)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Errore durante la scrittura su CSV {table_name}: {e}", exc_info=True)
            return False
    
    def save_message(self, message_text: str, user_id: int, username: str, chat_id: int,
                     group_name: str, approvato: bool, domanda: bool, motivo_rifiuto: str = "") -> bool:
        """Salva un messaggio nel file CSV 'messages'. API identica a GoogleSheetsManager."""
        row = [
            datetime.now().isoformat(),
            message_text,
            str(user_id),
            username,
            str(chat_id),
            group_name,
            "SI" if approvato else "NO",
            "SI" if domanda else "NO",
            motivo_rifiuto
        ]
        
        success = self._append_to_csv("messages", row)
        if success and self.enabled:
            self.logger.debug(f"Messaggio salvato in CSV: User {user_id} in chat {chat_id}")
        return success
    
    def save_admin_message(self, message_text: str, user_id: int, username: str, chat_id: int, group_name: str) -> bool:
        """Salva un messaggio di un admin nel file CSV 'admin'. API identica a GoogleSheetsManager."""
        row = [
            datetime.now().isoformat(),
            message_text,
            str(user_id),
            username,
            str(chat_id),
            group_name
        ]
        
        success = self._append_to_csv("admin", row)
        if success and self.enabled:
            self.logger.debug(f"Messaggio admin salvato in CSV: User {user_id} in chat {chat_id}")
        return success
    
    def ban_user(self, user_id: int, username: str, motivo: str = "Violazione regole") -> bool:
        """Aggiunge un utente alla lista dei bannati nel file CSV 'banned_users'. API identica a GoogleSheetsManager."""
        if not self.enabled:
            self.logger.debug("CSV disabilitato, skip ban utente")
            return True
            
        # Verifica se l'utente è già bannato
        if self.is_user_banned(user_id):
            self.logger.info(f"Utente {user_id} ({username}) è già nella lista dei bannati CSV.")
            return True
        
        row = [
            str(user_id),
            datetime.now().isoformat(),
            motivo
        ]
        
        success = self._append_to_csv("banned_users", row)
        if success:
            self.logger.info(f"Utente {user_id} ({username}) bannato con successo in CSV. Motivo: {motivo}")
            self._invalidate_banned_cache()  # Invalida la cache
        return success
    
    def unban_user(self, user_id: int, unban_reason: str = "Unban da dashboard", unbanned_by: str = "dashboard") -> bool:
        """
        NUOVO: Rimuove un utente dalla lista dei bannati nel file CSV 'banned_users'.
        Sposta l'entry nel file di storico unban per mantenere traccia.
        
        Args:
            user_id: ID dell'utente da sbannare
            unban_reason: Motivo dell'unban
            unbanned_by: Chi ha effettuato l'unban (dashboard, admin, etc.)
            
        Returns:
            bool: True se l'operazione è riuscita, False altrimenti
        """
        if not self.enabled:
            self.logger.debug("CSV disabilitato, skip unban utente")
            return True
            
        if not self.is_user_banned(user_id):
            self.logger.info(f"Utente {user_id} non è nella lista dei bannati CSV.")
            return True  # Considerato successo se non è bannato
        
        try:
            with self.lock:  # Thread safety per operazione complessa
                # 1. Leggi il file banned_users e trova l'entry dell'utente
                banned_file_path = os.path.join(self.data_dir, self.csv_structure["banned_users"]["filename"])
                unban_history_file_path = os.path.join(self.data_dir, self.csv_structure["unban_history"]["filename"])
                
                user_ban_data = None
                remaining_rows = []
                
                # Leggi e filtra il file banned_users
                with open(banned_file_path, 'r', encoding='utf-8') as csvfile:
                    reader = csv.reader(csvfile)
                    headers = next(reader, [])  # Leggi headers
                    remaining_rows.append(headers)  # Mantieni headers
                    
                    for row in reader:
                        if len(row) >= 1 and row[0] == str(user_id):
                            # Trovata l'entry dell'utente da rimuovere
                            user_ban_data = row
                            self.logger.debug(f"Trovata entry ban per utente {user_id}: {row}")
                        else:
                            # Mantieni tutte le altre righe
                            remaining_rows.append(row)
                
                if not user_ban_data:
                    self.logger.warning(f"Entry ban per utente {user_id} non trovata nel CSV")
                    return False
                
                # 2. Salva l'entry nell'unban_history prima di rimuoverla
                unban_history_row = [
                    str(user_id),
                    user_ban_data[1] if len(user_ban_data) > 1 else "",  # original_ban_timestamp
                    datetime.now().isoformat(),  # unban_timestamp
                    unban_reason,
                    unbanned_by
                ]
                
                # Crea file unban_history se non esiste
                if not os.path.exists(unban_history_file_path):
                    with open(unban_history_file_path, 'w', newline='', encoding='utf-8') as csvfile:
                        writer = csv.writer(csvfile)
                        writer.writerow(self.csv_structure["unban_history"]["headers"])
                
                # Aggiungi entry allo storico unban
                with open(unban_history_file_path, 'a', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(unban_history_row)
                
                # 3. Riscrivi il file banned_users senza l'utente unbannato
                # Usa file temporaneo per sicurezza
                temp_file_path = banned_file_path + '.tmp'
                
                with open(temp_file_path, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerows(remaining_rows)
                
                # 4. Sostituisci il file originale con quello aggiornato
                shutil.move(temp_file_path, banned_file_path)
                
                # 5. Invalida cache e log successo
                self._invalidate_banned_cache()
                
                ban_motivo = user_ban_data[2] if len(user_ban_data) > 2 else "Motivo sconosciuto"
                ban_timestamp = user_ban_data[1] if len(user_ban_data) > 1 else "Data sconosciuta"
                
                self.logger.info(
                    f"Utente {user_id} unbannato con successo da CSV. "
                    f"Ban originale: {ban_timestamp} ({ban_motivo}). "
                    f"Unban: {unban_reason} by {unbanned_by}"
                )
                
                return True
                
        except Exception as e:
            self.logger.error(f"Errore durante unban utente {user_id}: {e}", exc_info=True)
            
            # Cleanup file temporaneo se esiste
            temp_file_path = os.path.join(self.data_dir, self.csv_structure["banned_users"]["filename"] + '.tmp')
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except:
                    pass
            
            return False
    
    def get_unban_history(self, limit: int = None) -> List[Dict]:
        """
        NUOVO: Restituisce lo storico degli unban.
        
        Args:
            limit: Numero massimo di record da restituire
            
        Returns:
            Lista di dizionari con storico unban
        """
        return self.read_csv_data("unban_history", limit=limit)
    
    def get_user_ban_history(self, user_id: int) -> Dict:
        """
        NUOVO: Restituisce lo storico completo di ban/unban per un utente specifico.
        
        Args:
            user_id: ID dell'utente
            
        Returns:
            Dizionario con storico completo dell'utente
        """
        try:
            # Controlla se attualmente bannato
            currently_banned = self.is_user_banned(user_id)
            
            # Cerca negli unban
            unban_history = self.get_unban_history()
            user_unbans = [record for record in unban_history if record.get('user_id') == str(user_id)]
            
            # Se attualmente bannato, cerca i dettagli del ban corrente
            current_ban_details = None
            if currently_banned:
                banned_users = self.read_csv_data("banned_users")
                for ban_record in banned_users:
                    if ban_record.get('user_id') == str(user_id):
                        current_ban_details = ban_record
                        break
            
            return {
                'user_id': user_id,
                'currently_banned': currently_banned,
                'current_ban_details': current_ban_details,
                'unban_history': user_unbans,
                'total_bans': len(user_unbans) + (1 if currently_banned else 0),
                'total_unbans': len(user_unbans)
            }
            
        except Exception as e:
            self.logger.error(f"Errore nel recupero storico ban/unban per utente {user_id}: {e}")
            return {
                'user_id': user_id,
                'currently_banned': False,
                'current_ban_details': None,
                'unban_history': [],
                'total_bans': 0,
                'total_unbans': 0,
                'error': str(e)
            }
    
    def is_user_banned(self, user_id: int) -> bool:
        """Verifica se un utente è presente nella lista dei bannati CSV. API identica a GoogleSheetsManager."""
        if not self.enabled:
            return False  # Se CSV disabilitato, non può confermare ban
            
        try:
            banned_users = self._get_banned_users_cached()
            return str(user_id) in banned_users
            
        except Exception as e:
            self.logger.error(f"Errore durante la verifica dello stato ban per l'utente {user_id}: {e}")
            return False
    
    def _get_banned_users_cached(self) -> set:
        """Restituisce set di user_id bannati usando una cache con TTL di 5 minuti."""
        current_time = datetime.now()
        
        # Cache valida per 5 minuti
        if (self._banned_users_cache is not None and 
            self._cache_timestamp is not None and 
            (current_time - self._cache_timestamp).seconds < 300):
            return self._banned_users_cache
        
        # Ricarica cache
        banned_users = set()
        file_path = os.path.join(self.data_dir, self.csv_structure["banned_users"]["filename"])
        
        try:
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as csvfile:
                    reader = csv.DictReader(csvfile)
                    for row in reader:
                        banned_users.add(row['user_id'])
                        
                self._banned_users_cache = banned_users
                self._cache_timestamp = current_time
                return banned_users
            else:
                return set()
                
        except Exception as e:
            self.logger.error(f"Errore durante il caricamento utenti bannati da CSV: {e}")
            return set()
    
    def _invalidate_banned_cache(self):
        """Invalida la cache degli utenti bannati."""
        self._banned_users_cache = None
        self._cache_timestamp = None
    
    def backup_csv_files(self) -> bool:
        """Crea backup di tutti i file CSV."""
        if not self.enabled:
            self.logger.info("CSV disabilitato, skip backup")
            return True
            
        try:
            backup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_folder = os.path.join(self.backup_dir, f"backup_{backup_timestamp}")
            os.makedirs(backup_folder, exist_ok=True)
            
            backup_count = 0
            for table_name, structure in self.csv_structure.items():
                source_path = os.path.join(self.data_dir, structure["filename"])
                if os.path.exists(source_path):
                    backup_path = os.path.join(backup_folder, structure["filename"])
                    
                    # Copia il file
                    import shutil
                    shutil.copy2(source_path, backup_path)
                    backup_count += 1
            
            self.logger.info(f"Backup CSV completato: {backup_count} file salvati in {backup_folder}")
            return True
            
        except Exception as e:
            self.logger.error(f"Errore durante il backup CSV: {e}", exc_info=True)
            return False
    
    def get_csv_stats(self) -> Dict[str, int]:
        """Restituisce statistiche sui file CSV."""
        stats = {}
        
        if not self.enabled:
            return {"csv_disabled": True}
        
        for table_name, structure in self.csv_structure.items():
            file_path = os.path.join(self.data_dir, structure["filename"])
            try:
                if os.path.exists(file_path):
                    with open(file_path, 'r', encoding='utf-8') as csvfile:
                        reader = csv.reader(csvfile)
                        row_count = sum(1 for row in reader) - 1  # -1 per header
                        stats[table_name] = max(0, row_count)
                else:
                    stats[table_name] = 0
            except Exception as e:
                self.logger.error(f"Errore nel conteggio righe per {table_name}: {e}")
                stats[table_name] = -1  # Indica errore
        
        return stats
    
    def read_csv_data(self, table_name: str, limit: int = None) -> List[Dict]:
        """Legge dati da un file CSV e li restituisce come lista di dizionari."""
        if not self.enabled:
            return []
            
        if table_name not in self.csv_structure:
            self.logger.error(f"Tabella CSV sconosciuta: {table_name}")
            return []
        
        file_path = os.path.join(self.data_dir, self.csv_structure[table_name]["filename"])
        
        try:
            data = []
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as csvfile:
                    reader = csv.DictReader(csvfile)
                    for i, row in enumerate(reader):
                        if limit and i >= limit:
                            break
                        data.append(row)
            
            return data
            
        except Exception as e:
            self.logger.error(f"Errore durante la lettura di {table_name}: {e}")
            return []
    
    def cleanup_old_backups(self, keep_days: int = 30):
        """Rimuove backup più vecchi di N giorni."""
        if not self.enabled or not os.path.exists(self.backup_dir):
            return
            
        try:
            cutoff_time = datetime.now().timestamp() - (keep_days * 24 * 3600)
            removed_count = 0
            
            for backup_folder in os.listdir(self.backup_dir):
                backup_path = os.path.join(self.backup_dir, backup_folder)
                if os.path.isdir(backup_path):
                    folder_time = os.path.getmtime(backup_path)
                    if folder_time < cutoff_time:
                        import shutil
                        shutil.rmtree(backup_path)
                        removed_count += 1
            
            if removed_count > 0:
                self.logger.info(f"Rimossi {removed_count} backup CSV più vecchi di {keep_days} giorni")
                
        except Exception as e:
            self.logger.error(f"Errore durante pulizia backup CSV: {e}")

    def get_status(self) -> Dict[str, any]:
        """Restituisce lo stato del sistema CSV."""
        status = {
            "enabled": self.enabled,
            "data_dir": self.data_dir,
            "backup_dir": self.backup_dir,
            "files_exist": {},
            "stats": self.get_csv_stats() if self.enabled else {}
        }
        
        # Controlla esistenza file
        for table_name, structure in self.csv_structure.items():
            file_path = os.path.join(self.data_dir, structure["filename"])
            status["files_exist"][table_name] = os.path.exists(file_path)
        
        return status

    def check_and_auto_backup_if_needed(self, table_name: str) -> bool:
        """
        Controlla se un file CSV ha raggiunto la soglia di righe e esegue backup automatico.
        
        Args:
            table_name: Nome della tabella da controllare
            
        Returns:
            bool: True se è stato eseguito un backup, False altrimenti
        """
        if not self.enabled or not self.auto_backup_enabled:
            return False
        
        if table_name not in self.csv_structure:
            return False
        
        file_path = os.path.join(self.data_dir, self.csv_structure[table_name]["filename"])
        
        if not os.path.exists(file_path):
            return False
        
        try:
            # Conta le righe del file (escludendo header)
            with open(file_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.reader(csvfile)
                row_count = sum(1 for row in reader) - 1  # -1 per header
            
            # Se non ha raggiunto la soglia, non fare nulla
            if row_count < self.auto_backup_threshold:
                return False
            
            self.logger.info(f"🔄 File {table_name} ha raggiunto {row_count} righe (soglia: {self.auto_backup_threshold})")
            self.logger.info(f"   Avvio backup automatico e svuotamento...")
            
            # Esegui backup automatico con svuotamento
            backup_success = self._perform_auto_backup_and_clear(table_name, file_path, row_count)
            
            if backup_success:
                self.logger.info(f"✅ Backup automatico di {table_name} completato con successo")
            else:
                self.logger.error(f"❌ Backup automatico di {table_name} fallito")
            
            return backup_success
            
        except Exception as e:
            self.logger.error(f"Errore durante controllo backup automatico per {table_name}: {e}")
            return False


    def _perform_auto_backup_and_clear(self, table_name: str, file_path: str, row_count: int) -> bool:
        """
        Esegue il backup automatico e svuota il file originale.
        
        Args:
            table_name: Nome della tabella
            file_path: Percorso del file CSV
            row_count: Numero di righe nel file
            
        Returns:
            bool: True se il backup e svuotamento sono riusciti
        """
        try:
            with self.lock:  # Thread safety
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                # Nome del file di backup: messages-20240115_143022.csv
                backup_filename = f"{table_name}-{timestamp}.csv"
                backup_path = os.path.join(self.backup_dir, backup_filename)
                
                # Crea directory di backup se non esiste
                os.makedirs(self.backup_dir, exist_ok=True)
                
                # STEP 1: Copia il file corrente nel backup
                shutil.copy2(file_path, backup_path)
                
                # STEP 2: Verifica che il backup sia stato creato correttamente
                if not os.path.exists(backup_path):
                    raise Exception(f"Backup file non creato: {backup_path}")
                
                # Verifica che il backup contenga i dati
                with open(backup_path, 'r', encoding='utf-8') as backup_file:
                    backup_reader = csv.reader(backup_file)
                    backup_rows = sum(1 for row in backup_reader)
                    if backup_rows != (row_count + 1):  # +1 per header
                        raise Exception(f"Backup incompleto: attese {row_count + 1} righe, trovate {backup_rows}")
                
                # STEP 3: Svuota il file originale mantenendo solo l'header
                if self.auto_backup_preserve_headers:
                    headers = self.csv_structure[table_name]["headers"]
                    with open(file_path, 'w', newline='', encoding='utf-8') as csvfile:
                        writer = csv.writer(csvfile)
                        writer.writerow(headers)
                    self.logger.info(f"   File {table_name} svuotato (header preservato)")
                else:
                    # Svuota completamente (non raccomandato)
                    open(file_path, 'w').close()
                    self.logger.info(f"   File {table_name} svuotato completamente")
                
                # STEP 4: Log del successo
                backup_size_mb = os.path.getsize(backup_path) / 1024 / 1024
                self.logger.info(
                    f"📦 Backup automatico creato: {backup_filename} "
                    f"({row_count} righe, {backup_size_mb:.2f} MB)"
                )
                
                return True
                
        except Exception as e:
            self.logger.error(f"Errore durante backup automatico di {table_name}: {e}")
            
            # Cleanup in caso di errore
            try:
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                    self.logger.info(f"Backup parziale rimosso: {backup_filename}")
            except:
                pass
            
            return False