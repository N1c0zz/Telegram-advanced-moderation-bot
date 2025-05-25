import logging
import os
from datetime import datetime
from typing import Dict, Any, Tuple, Optional

import gspread
from google.oauth2.service_account import Credentials

from .config_manager import ConfigManager # Assumendo che ConfigManager sia nello stesso package

class GoogleSheetsManager:
    """
    Gestisce l'interazione con Google Sheets per salvare dati di moderazione.
    """
    def __init__(self, logger: logging.Logger, config_manager: ConfigManager):
        self.logger = logger
        self.config = config_manager.config  # Accesso diretto al dizionario di configurazione
        self.config_manager_instance = config_manager # Manteniamo l'istanza per salvare
        self.client: Optional[gspread.Client] = None
        self.sheet: Optional[gspread.Spreadsheet] = None
        self.worksheets: Dict[str, gspread.Worksheet] = {}
        self._initialize_client()

    def _initialize_client(self):
        """Inizializza il client Google Sheets e apre/crea il foglio di lavoro."""
        try:
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds_file = self.config.get("google_credentials_file", "config/credentials.json")

            if not os.path.exists(creds_file):
                self.logger.error(f"File credenziali Google non trovato: {creds_file}. Google Sheets non sarà disponibile.")
                return

            credentials = Credentials.from_service_account_file(creds_file, scopes=scope)
            self.client = gspread.authorize(credentials)
            self.logger.info("Client Google Sheets autorizzato.")

            sheet_id = self.config.get("google_sheet_id")
            if sheet_id:
                try:
                    self.sheet = self.client.open_by_key(sheet_id)
                    self.logger.info(f"Google Sheet '{self.sheet.title}' aperto con successo.")
                    if self.config.get("share_on_startup", False):
                        self._share_sheet()
                except gspread.exceptions.SpreadsheetNotFound:
                    self.logger.warning(f"Google Sheet ID {sheet_id} non trovato. Creazione nuovo foglio.")
                    self._create_and_share_new_sheet()
            else:
                self.logger.info("Nessun Google Sheet ID specificato. Creazione nuovo foglio.")
                self._create_and_share_new_sheet()

            if self.sheet:
                self._initialize_worksheets()
                self.migrate_user_data_if_needed() # rinominato per chiarezza
                # self.test_connection() # test_connection è più una verifica API generale
        except Exception as e:
            self.logger.error(f"Errore durante l'inizializzazione di Google Sheets: {e}", exc_info=True)

    def _create_and_share_new_sheet(self):
        """Crea un nuovo foglio Google, lo condivide e salva il suo ID."""
        if not self.client: return

        self.sheet = self.client.create("TelegramModerationLog")
        self.logger.info(f"Nuovo Google Sheet creato: '{self.sheet.title}' (ID: {self.sheet.id})")
        
        self.config["google_sheet_id"] = self.sheet.id
        self.config_manager_instance.save_config(self.config) # Salva la config aggiornata
        self.logger.info(f"ID del nuovo foglio salvato nella configurazione.")
        self._share_sheet(notify=True, email_message="Nuovo foglio creato dal bot Telegram di moderazione.")


    def _share_sheet(self, notify: bool = False, email_message: Optional[str] = None):
        """Condivide il foglio con l'email specificata nella configurazione."""
        if not self.sheet: return

        share_email = self.config.get("share_email")
        if not share_email:
            self.logger.warning("Nessuna email specificata per la condivisione del foglio.")
            return
        try:
            self.sheet.share(share_email, perm_type='user', role='writer', notify=notify, email_message=email_message)
            self.logger.info(f"Foglio '{self.sheet.title}' condiviso con {share_email}.")
            sheet_url = f"https://docs.google.com/spreadsheets/d/{self.sheet.id}"
            self.logger.info(f"Puoi accedere al foglio da: {sheet_url}")
        except Exception as e:
            self.logger.error(f"Impossibile condividere il foglio '{self.sheet.title}' con {share_email}: {e}", exc_info=True)


    def _initialize_worksheets(self):
        """Inizializza i fogli di lavoro necessari (messages, admin, banned_users)."""
        if not self.sheet: return

        sheet_headers = {
            "messages": ["timestamp", "messaggio", "user_id", "username", "chat_id", "group_name", "approvato", "domanda", "motivo_rifiuto"],
            "admin": ["timestamp", "messaggio", "user_id", "username", "chat_id", "group_name"],
            "banned_users": ["user_id", "timestamp", "motivo"]
        }

        existing_ws_titles = [ws.title for ws in self.sheet.worksheets()]

        for name, headers in sheet_headers.items():
            try:
                if name in existing_ws_titles:
                    worksheet = self.sheet.worksheet(name)
                    self.logger.info(f"Foglio di lavoro '{name}' trovato.")
                    if self.config.get("check_headers", False):
                        current_headers = worksheet.row_values(1)
                        if current_headers != headers:
                            self.logger.warning(f"Intestazioni del foglio '{name}' non corrispondono. Attese: {headers}, Trovate: {current_headers}. Non verranno modificate.")
                else:
                    self.logger.info(f"Creazione nuovo foglio di lavoro '{name}'.")
                    worksheet = self.sheet.add_worksheet(title=name, rows=1, cols=len(headers))
                    worksheet.append_row(headers)
                self.worksheets[name] = worksheet
            except Exception as e:
                self.logger.error(f"Errore durante l'inizializzazione del foglio '{name}': {e}", exc_info=True)

    def migrate_user_data_if_needed(self):
        """Migra i dati degli utenti nel foglio 'banned_users' al nuovo formato minimale, se necessario."""
        if not self.client or "banned_users" not in self.worksheets:
            self.logger.info("Google Sheets non inizializzato o foglio 'banned_users' mancante. Salto migrazione.")
            return

        try:
            worksheet = self.worksheets["banned_users"]
            all_data = worksheet.get_all_records() # Ottiene i dati come lista di dizionari
            
            if not all_data: # Foglio vuoto o solo header
                current_headers = worksheet.row_values(1) if worksheet.row_count >=1 else []
                expected_headers = ["user_id", "timestamp", "motivo"]
                if current_headers == expected_headers:
                    self.logger.info("Foglio 'banned_users' vuoto ma con intestazioni corrette. Nessuna migrazione necessaria.")
                    return
                if not current_headers: # Foglio completamente vuoto
                     self.logger.info("Foglio 'banned_users' completamente vuoto. Nessuna migrazione necessaria, imposto header.")
                     worksheet.append_row(expected_headers)
                     return


            # Verifica se la migrazione è necessaria controllando le intestazioni
            # Se get_all_records() funziona, le intestazioni dovrebbero essere presenti.
            # Controlliamo la prima riga di dati per vedere se ha le chiavi attese.
            # Questa logica di migrazione è complessa e potrebbe aver bisogno di un test più approfondito
            # se il formato vecchio era molto diverso. Per ora assumiamo una semplice verifica delle colonne.
            # Il codice originale faceva un backup e ricreava il foglio.
            # Questa è una versione semplificata che verifica solo se user_id, timestamp, motivo esistono.
            # Se la struttura è molto diversa, la logica originale di backup e ricreazione è più sicura.
            
            # La logica di migrazione originale era piuttosto distruttiva.
            # Per ora, logghiamo un avviso se le intestazioni non corrispondono.
            # Una vera migrazione dovrebbe essere un task separato e testato.
            headers = worksheet.row_values(1)
            expected_headers = ["user_id", "timestamp", "motivo"]
            if headers != expected_headers:
                self.logger.warning(
                    f"Le intestazioni del foglio 'banned_users' ({headers}) "
                    f"non corrispondono al formato atteso ({expected_headers}). "
                    "La migrazione manuale potrebbe essere necessaria se i dati non vengono letti/scritti correttamente."
                )
            else:
                self.logger.info("Formato foglio 'banned_users' corretto. Nessuna migrazione dati eseguita.")

        except Exception as e:
            self.logger.error(f"Errore durante la verifica/migrazione dei dati utente: {e}", exc_info=True)
            
    def save_message(self, message_text: str, user_id: int, username: str, chat_id: int,
                     group_name: str, approvato: bool, domanda: bool, motivo_rifiuto: str = ""):
        """Salva un messaggio (approvato o rifiutato) nel foglio 'messages'."""
        if "messages" not in self.worksheets:
            self.logger.error("Foglio 'messages' non disponibile. Impossibile salvare il messaggio.")
            return False
        try:
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
            self.worksheets["messages"].append_row(row)
            # self.logger.debug(f"Messaggio salvato su Sheets: User {user_id} in chat {chat_id}")
            return True
        except Exception as e:
            self.logger.error(f"Errore durante il salvataggio del messaggio su Google Sheets: {e}", exc_info=True)
            return False

    def save_admin_message(self, message_text: str, user_id: int, username: str, chat_id: int, group_name: str):
        """Salva un messaggio di un admin nel foglio 'admin'."""
        if "admin" not in self.worksheets:
            self.logger.error("Foglio 'admin' non disponibile. Impossibile salvare il messaggio admin.")
            return False
        try:
            row = [
                datetime.now().isoformat(),
                message_text,
                str(user_id),
                username,
                str(chat_id),
                group_name
            ]
            self.worksheets["admin"].append_row(row)
            # self.logger.debug(f"Messaggio admin salvato su Sheets: User {user_id} in chat {chat_id}")
            return True
        except Exception as e:
            self.logger.error(f"Errore durante il salvataggio del messaggio admin su Google Sheets: {e}", exc_info=True)
            return False

    def ban_user(self, user_id: int, username: str, motivo: str = "Violazione regole") -> bool:
        """Aggiunge un utente alla lista dei bannati nel foglio 'banned_users'."""
        if "banned_users" not in self.worksheets:
            self.logger.error("Foglio 'banned_users' non disponibile. Impossibile bannare l'utente.")
            return False
        
        worksheet = self.worksheets["banned_users"]
        try:
            # Verifica se l'utente è già bannato per evitare duplicati
            # gspread non ha un modo semplice per cercare per valore senza leggere tutta la colonna.
            # Per fogli molto grandi, questo potrebbe essere inefficiente.
            # Alternativa: leggere tutti i record e cercare in memoria, o usare API v4 per query più complesse.
            user_ids_in_sheet = worksheet.col_values(1) # Colonna 'user_id' è la prima (indice 1)
            if str(user_id) in user_ids_in_sheet:
                self.logger.info(f"Utente {user_id} ({username}) è già nella lista dei bannati.")
                return True # Considerato successo se già bannato

            row = [
                str(user_id),
                datetime.now().isoformat(),
                motivo
            ]
            worksheet.append_row(row)
            self.logger.info(f"Utente {user_id} ({username}) bannato con successo. Motivo: {motivo}")
            return True
        except Exception as e:
            self.logger.error(f"Errore durante il ban dell'utente {user_id} ({username}) su Google Sheets: {e}", exc_info=True)
            return False

    def is_user_banned(self, user_id: int) -> bool:
        """Verifica se un utente è presente nella lista dei bannati."""
        if "banned_users" not in self.worksheets:
            self.logger.warning("Foglio 'banned_users' non disponibile per controllo ban.")
            return False # Non possiamo confermare, quindi assumiamo non bannato
        
        worksheet = self.worksheets["banned_users"]
        try:
            # Questo può essere lento per fogli grandi.
            # Considerare di caricare la lista in memoria all'avvio e aggiornarla.
            user_ids_in_sheet = worksheet.col_values(1) # Colonna 'user_id'
            return str(user_id) in user_ids_in_sheet
        except Exception as e:
            self.logger.error(f"Errore durante la verifica dello stato ban per l'utente {user_id}: {e}", exc_info=True)
            return False # In caso di errore, meglio assumere che non sia bannato