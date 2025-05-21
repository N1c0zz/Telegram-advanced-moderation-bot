```python
import csv
import logging
import os
import pathlib
import schedule # type: ignore
import time
from datetime import datetime
from typing import Optional

from .sheets_interface import GoogleSheetsManager # Assumendo che GoogleSheetsManager sia nello stesso package

class SheetBackupManager:
    """
    Gestisce il backup dei dati da Google Sheets a file CSV locali.
    """
    def __init__(self, sheets_manager: GoogleSheetsManager, logger: logging.Logger, backup_dir: str = "backups"):
        self.sheets_manager = sheets_manager
        self.logger = logger
        self.backup_dir = backup_dir
        pathlib.Path(self.backup_dir).mkdir(parents=True, exist_ok=True)

    def backup_sheets_to_csv(self) -> bool:
        """
        Esegue il backup di tutti i fogli (tranne 'banned_users') in file CSV.
        Dopo il backup, svuota i fogli backuppati mantenendo le intestazioni.
        Restituisce True se almeno un backup è stato eseguito con successo, False altrimenti.
        """
        if not self.sheets_manager.client or not self.sheets_manager.sheet or not self.sheets_manager.worksheets:
            self.logger.error("Google Sheets non inizializzato correttamente. Impossibile eseguire il backup.")
            return False

        current_date_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        successful_backups_sheets = []
        excluded_sheets = ["banned_users"] # Foglio da non svuotare/backuppare in questo modo

        self.logger.info("Avvio processo di backup dei fogli Google Sheets...")

        for sheet_name, worksheet in self.sheets_manager.worksheets.items():
            if sheet_name in excluded_sheets:
                self.logger.info(f"Foglio '{sheet_name}' escluso dal processo di backup e pulizia.")
                continue
            
            try:
                all_values = worksheet.get_all_values() # Include intestazioni
                if not all_values or len(all_values) <= 1: # Vuoto o solo intestazioni
                    self.logger.info(f"Il foglio '{sheet_name}' è vuoto o contiene solo intestazioni. Salto backup.")
                    continue

                filename = f"{sheet_name}_{current_date_str}.csv"
                filepath = os.path.join(self.backup_dir, filename)

                with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
                    csv_writer = csv.writer(csvfile)
                    csv_writer.writerows(all_values)
                
                self.logger.info(f"Backup del foglio '{sheet_name}' completato: {filepath}")
                successful_backups_sheets.append(sheet_name)

            except Exception as e:
                self.logger.error(f"Errore durante il backup del foglio '{sheet_name}': {e}", exc_info=True)

        # Svuota i fogli di cui è stato eseguito il backup con successo
        for sheet_name_to_clear in successful_backups_sheets:
            try:
                self._clear_worksheet_data(sheet_name_to_clear)
            except Exception as e: # Errore specifico qui è loggato da _clear_worksheet_data
                self.logger.error(f"Fallita pulizia del foglio '{sheet_name_to_clear}' dopo backup. Errore: {e}")

        if successful_backups_sheets:
            self.logger.info(f"Processo di backup completato. {len(successful_backups_sheets)} fogli backuppati.")
            return True
        else:
            self.logger.info("Nessun foglio è stato backuppato in questa sessione.")
            return False

    def _clear_worksheet_data(self, sheet_name: str):
        """Svuota un foglio di lavoro mantenendo la riga delle intestazioni."""
        worksheet = self.sheets_manager.worksheets.get(sheet_name)
        if not worksheet:
            self.logger.error(f"Foglio '{sheet_name}' non trovato per la pulizia.")
            return

        try:
            # Ottieni le intestazioni (prima riga)
            headers = worksheet.row_values(1)
            if not headers: # Dovrebbe sempre esserci se il foglio è inizializzato correttamente
                self.logger.warning(f"Nessuna intestazione trovata nel foglio '{sheet_name}'. Impossibile pulire in modo sicuro.")
                return

            # Cancella tutte le righe tranne l'intestazione.
            # gspread API: worksheet.clear() rimuove tutto.
            # Dobbiamo cancellare le righe dalla 2 in poi.
            num_rows = worksheet.row_count
            if num_rows > 1:
                 # Costruisci il range per cancellare, es. 'A2:D100'
                 # Assumendo che le colonne non superino 'Z' per semplicità.
                 # chr(65 + len(headers) -1) dà la lettera della colonna finale.
                last_col_letter = gspread.utils.rowcol_to_a1(1, len(headers)).replace('1','')

                # Crea un batch di richieste di cancellazione
                # worksheet.delete_rows(2, num_rows) # Questo può essere lento per molti dati
                # Alternativa: pulire il contenuto delle celle
                range_to_clear = f'A2:{last_col_letter}{num_rows}'
                worksheet.batch_clear([range_to_clear])
                self.logger.info(f"Dati del foglio '{sheet_name}' svuotati con successo (mantenute intestazioni).")
            else:
                self.logger.info(f"Il foglio '{sheet_name}' ha solo intestazioni o è vuoto. Nessuna pulizia dati necessaria.")
        except Exception as e:
            self.logger.error(f"Errore durante la pulizia del foglio '{sheet_name}': {e}", exc_info=True)
            raise # Rilancia per essere gestito dal chiamante

    def schedule_regular_backups(self, interval_days: int = 7, at_time: str = "03:00"):
        """
        Programma backup regolari.
        :param interval_days: Intervallo in giorni tra i backup.
        :param at_time: Orario del backup nel formato "HH:MM".
        """
        if interval_days <= 0:
            self.logger.warning("Intervallo di backup non valido. Backup non programmato.")
            return
        
        try:
            # schedule.every(interval_days).days.at(at_time).do(self.backup_sheets_to_csv) # La chiamata originale
            # Per evitare che l'oggetto self non sia quello giusto nel contesto di schedule, usiamo un wrapper o functools.partial
            # Tuttavia, il modo in cui è chiamato (self.backup_sheets_to_csv) dovrebbe funzionare correttamente.
            # Verifichiamo che `at_time` sia valido.
            datetime.strptime(at_time, "%H:%M") # Valida il formato dell'ora
            
            schedule.every(interval_days).days.at(at_time).do(
                lambda: self.logger.info("Esecuzione backup programmato...") or self.backup_sheets_to_csv()
            )
            self.logger.info(f"Backup dei fogli Google Sheets programmato ogni {interval_days} giorni alle {at_time}.")
        except ValueError:
            self.logger.error(f"Formato ora '{at_time}' non valido per la programmazione del backup. Usare HH:MM.")
        except Exception as e:
            self.logger.error(f"Errore durante la programmazione dei backup: {e}", exc_info=True)

```