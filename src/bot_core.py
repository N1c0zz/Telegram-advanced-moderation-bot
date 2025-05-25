import asyncio
import os
import logging
import time
from datetime import datetime, timedelta
import concurrent.futures
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
import schedule # type: ignore
from telegram import Update, ChatPermissions, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.error import BadRequest, Forbidden

# Importazioni locali dal package src
from .config_manager import ConfigManager
from .logger_config import LoggingConfigurator
from .sheets_interface import GoogleSheetsManager
from .backup_handler import SheetBackupManager
from .moderation_rules import AdvancedModerationBotLogic
from .cache_utils import MessageCache
from .spam_detection import CrossGroupSpamDetector


class TelegramModerationBot:
    """
    Classe principale del bot Telegram per la moderazione avanzata.
    Orchestra i vari componenti come configurazione, logging, interazione con Google Sheets,
    logica di moderazione, e gestione dei comandi/messaggi Telegram.
    """
    def __init__(self):
        load_dotenv()

        self.config_manager = ConfigManager()
        self.logger = LoggingConfigurator.setup_logging(
            log_dir=self.config_manager.get('log_directory', 'logs'),
            disable_console=self.config_manager.get('disable_console_logging', True)
        )

        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self.token:
            self.logger.critical("Token Telegram (TELEGRAM_BOT_TOKEN) non trovato nelle variabili d'ambiente.")
            raise ValueError("Token Telegram mancante.")
        
        if not os.getenv("OPENAI_API_KEY"):
            self.logger.warning("Chiave API OpenAI (OPENAI_API_KEY) non trovata. L'analisi AI sarà limitata.")

        self.sheets_manager = GoogleSheetsManager(self.logger, self.config_manager)
        
        self.moderation_logic = AdvancedModerationBotLogic(self.config_manager, self.logger) # Passa solo config e logger

        self.backup_manager = SheetBackupManager(
            self.sheets_manager,
            self.logger,
            self.config_manager.get('backup_directory', 'backups')
        )
        backup_interval = self.config_manager.get_nested('backup_interval_days', default=7)
        self.backup_manager.schedule_regular_backups(interval_days=backup_interval)

        self.cross_group_spam_detector = CrossGroupSpamDetector(
            time_window_hours=self.config_manager.get_nested('spam_detector', 'time_window_hours', default=1),
            similarity_threshold=self.config_manager.get_nested('spam_detector', 'similarity_threshold', default=0.85),
            min_groups=self.config_manager.get_nested('spam_detector', 'min_groups', default=2),
            logger=self.logger
        )
        schedule.every(30).minutes.do(self.cross_group_spam_detector.cleanup_old_data)

        self.message_cache = MessageCache(
            max_hours=self.config_manager.get_nested('message_cache', 'max_hours', default=3)
        )
        schedule.every(30).minutes.do(self.message_cache.cleanup_all_old_data)

        # Attributi per Night Mode e stato
        self.night_mode_transition_active: bool = False
        self.night_mode_grace_period_end: Optional[datetime] = None
        self.night_mode_messages_sent: Dict[int, int] = {} # chat_id -> message_id del messaggio di notifica night mode
        self.original_group_permissions: Dict[int, ChatPermissions] = {} # chat_id -> ChatPermissions originali
        self.scheduler_active: bool = True # Flag per lo scheduler in background

        # Statistiche a livello di bot (azioni Telegram)
        self.bot_stats: Dict[str, int] = {
            'total_messages_processed': 0,
            'messages_deleted_total': 0,
            'messages_deleted_by_direct_filter': 0, # Sostituisce direct_filter_deletions di AdvancedModerationBot
            'messages_deleted_by_ai_filter': 0, # Sostituisce ai_filter_deletions
            'edited_messages_detected': 0,
            'edited_messages_deleted': 0
        }
        self.application: Optional[Application] = None # Inizializzato in start()
        self._operation_locks: Dict[str, bool] = {} # Semplice lock in memoria

    # --- Lock Management ---
    def _acquire_lock(self, operation_name: str, timeout: int = 60) -> bool:
        """
        Tenta di acquisire un lock basato su file per un'operazione.
        Restituisce True se il lock è acquisito, False altrimenti.
        """
        lock_file_path = f"{operation_name}.lock" # Creato nella CWD
        
        if os.path.exists(lock_file_path):
            try:
                creation_time = os.path.getmtime(lock_file_path)
                if (time.time() - creation_time) > timeout:
                    self.logger.warning(f"Lock per '{operation_name}' ({lock_file_path}) scaduto. Rimozione forzata.")
                    os.remove(lock_file_path)
                else:
                    self.logger.debug(f"Lock per '{operation_name}' ({lock_file_path}) già attivo.")
                    return False # Lock valido esistente
            except OSError as e:
                self.logger.error(f"Errore nel controllo del lock file '{lock_file_path}': {e}")
                return False # Non sicuro procedere

        try:
            with open(lock_file_path, 'w') as f:
                f.write(datetime.now().isoformat())
            self.logger.debug(f"Lock acquisito per '{operation_name}' ({lock_file_path}).")
            return True
        except IOError as e:
            self.logger.error(f"Errore nella creazione del lock file '{lock_file_path}': {e}")
            return False

    def _release_lock(self, operation_name: str):
        """Rilascia il lock per un'operazione."""
        lock_file_path = f"{operation_name}.lock"
        try:
            if os.path.exists(lock_file_path):
                os.remove(lock_file_path)
                self.logger.debug(f"Lock rilasciato per '{operation_name}' ({lock_file_path}).")
        except OSError as e:
            self.logger.error(f"Errore nel rilascio del lock file '{lock_file_path}': {e}")
    
    # --- Safe Coroutine Execution ---
    def _safe_run_coroutine(self, coroutine_func: callable, description: str = "operazione asincrona"):
        """
        Esegue una coroutine in modo sicuro, preferendo il loop dell'applicazione Telegram
        o un nuovo loop se necessario. `coroutine_func` deve essere una funzione che
        prende un'istanza di `telegram.Bot` come primo argomento e restituisce una coroutine.
        """
        if self.application and hasattr(self.application, '_loop') and self.application._loop and self.application._loop.is_running():
            bot_instance = self.application.bot
            actual_coroutine = coroutine_func(bot_instance) # Crea la coroutine passando il bot dell'app
            future = asyncio.run_coroutine_threadsafe(actual_coroutine, self.application.loop)
            try:
                return future.result(timeout=60) # Timeout per l'operazione
            except concurrent.futures.TimeoutError:
                self.logger.error(f"Timeout durante l'esecuzione di '{description}' sul loop principale.")
            except Exception as e_future:
                self.logger.error(f"Errore futuro durante l'esecuzione di '{description}': {e_future}", exc_info=True)
        else:
            self.logger.warning(f"Loop dell'applicazione non disponibile per '{description}'. Esecuzione in un loop temporaneo.")
            # Questo blocco è più complesso e potenzialmente problematico per la gestione delle risorse del bot temporaneo.
            # Per operazioni programmate (schedule), assicurarsi che `self.application` sia disponibile.
            # Se `self.application` non è ancora inizializzato, questa parte non dovrebbe essere chiamata.
            # Mantengo la logica originale per ora, ma con cautela.
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            temp_bot_instance = None
            try:
                temp_bot_instance = Bot(token=self.token)
                actual_coroutine = coroutine_func(temp_bot_instance) # Crea coroutine con bot temporaneo
                result = new_loop.run_until_complete(actual_coroutine)
                # Gestione chiusura risorse bot temporaneo
                if hasattr(temp_bot_instance, '_client') and hasattr(temp_bot_instance._client, 'shutdown'): # PTB v20+
                     new_loop.run_until_complete(temp_bot_instance._client.shutdown())
                elif hasattr(temp_bot_instance, 'shutdown'): # Vecchie versioni
                     new_loop.run_until_complete(temp_bot_instance.shutdown())
                return result
            except Exception as e_inner:
                self.logger.error(f"Errore durante l'esecuzione di '{description}' nel loop temporaneo: {e_inner}", exc_info=True)
            finally:
                if temp_bot_instance: # Assicurati che sia stato creato
                    # Tentativo extra di chiusura se non fatto prima
                    try:
                        if hasattr(temp_bot_instance, '_client') and hasattr(temp_bot_instance._client, 'shutdown') and not new_loop.is_closed():
                            new_loop.run_until_complete(temp_bot_instance._client.shutdown())
                    except Exception as e_shutdown:
                        self.logger.error(f"Errore chiusura finale risorse bot temporaneo per '{description}': {e_shutdown}")

                if not new_loop.is_closed():
                    new_loop.close()
                asyncio.set_event_loop(None) # Ripristina
        return None

    # --- Night Mode Logic ---
    def get_night_mode_groups(self) -> List[int]:
        """Restituisce la lista degli ID dei gruppi configurati per la Night Mode."""
        return self.config_manager.get_nested('night_mode', 'night_mode_groups', default=[])

    def is_night_mode_period_active(self, chat_id_to_check_config_for: int = -1) -> bool:
        """
        Verifica se l'orario corrente rientra nel periodo di Night Mode definito nella configurazione.
        :param chat_id_to_check_config_for: Se fornito e diverso da -1, verifica anche che il
                                             gruppo sia abilitato per la night mode. Se -1, controlla solo l'orario.
        """
        nm_config = self.config_manager.get('night_mode', {})
        if not nm_config.get('enabled', True):
            return False

        if chat_id_to_check_config_for != -1: # -1 è un valore speciale per indicare solo controllo orario
            if chat_id_to_check_config_for not in nm_config.get('night_mode_groups', []):
                return False # Gruppo non configurato per night mode

        start_str = nm_config.get('start_hour', '23:00')
        end_str = nm_config.get('end_hour', '07:00')

        try:
            start_time = datetime.strptime(start_str, '%H:%M').time()
            end_time = datetime.strptime(end_str, '%H:%M').time()
        except ValueError:
            self.logger.error(f"Formato ora Night Mode non valido: start='{start_str}', end='{end_str}'. Usare HH:MM.")
            return False # Default a non attivo se configurazione errata

        now_time = datetime.now().time()

        if start_time <= end_time:  # Night mode non attraversa la mezzanotte (es. 01:00 - 05:00)
            return start_time <= now_time < end_time
        else:  # Night mode attraversa la mezzanotte (es. 23:00 - 07:00)
            return now_time >= start_time or now_time < end_time

    async def _apply_night_mode_permissions(self, bot: Bot, chat_id: int, activate: bool):
        """Applica o ripristina i permessi di un gruppo per la Night Mode."""
        group_name_for_log = f"Chat {chat_id}"
        try:
            chat_info = await bot.get_chat(chat_id)
            group_name_for_log = chat_info.title or group_name_for_log
        except Exception: # Non bloccare se non riusciamo a prendere il nome
            pass
        
        nm_config = self.config_manager.get('night_mode', {})

        if activate:
            # Salva permessi originali se non già fatto
            if chat_id not in self.original_group_permissions:
                try:
                    current_chat = await bot.get_chat(chat_id)
                    if current_chat.permissions:
                         self.original_group_permissions[chat_id] = current_chat.permissions
                         self.logger.info(f"Permessi originali salvati per {group_name_for_log} ({chat_id}).")
                except Exception as e:
                    self.logger.warning(f"Impossibile salvare i permessi originali per {group_name_for_log} ({chat_id}): {e}")
            
            # Imposta permessi restrittivi
            # Nota: la granularità dei permessi può variare con le versioni di python-telegram-bot
            # Questo cerca di essere compatibile con versioni più recenti.
            try:
                restricted_permissions = ChatPermissions(
                    can_send_messages=False, can_send_audios=False, can_send_documents=False,
                    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                    can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
                    can_add_web_page_previews=False, # Spesso True è OK anche in night mode
                    can_change_info=False, can_invite_users=True, can_pin_messages=False
                )
            except TypeError: # Fallback per versioni più vecchie o diverse di PTB
                 self.logger.debug("Usando ChatPermissions con meno parametri (fallback).")
                 restricted_permissions = ChatPermissions(can_send_messages=False, can_invite_users=True)

            await bot.set_chat_permissions(chat_id, restricted_permissions)
            self.logger.info(f"🌙 Night Mode ATTIVATA per {group_name_for_log} ({chat_id}).")

            # Invia messaggio di notifica
            start_msg_template = nm_config.get('start_message', "⛔ Night Mode attiva fino alle {end_hour}.")
            end_hour_str = nm_config.get('end_hour', '07:00')
            start_msg = start_msg_template.format(end_hour=end_hour_str)
            
            # Aggiungi info per nuovi membri
            start_msg += (
                f"\n\nℹ️ Per i nuovi membri: questa è una restrizione temporanea. "
                f"Potrai scrivere dalle {end_hour_str}."
            )
            try:
                sent_notification = await bot.send_message(chat_id, start_msg)
                # Unpinna vecchio messaggio se esiste
                if chat_id in self.night_mode_messages_sent:
                    try:
                        await bot.unpin_chat_message(chat_id, self.night_mode_messages_sent[chat_id])
                    except Exception: pass # Ignora se non pinnato o già rimosso
                # Pinna nuovo messaggio e salva ID
                # await bot.pin_chat_message(chat_id, sent_notification.message_id, disable_notification=True)
                self.night_mode_messages_sent[chat_id] = sent_notification.message_id # Non lo pinno per non essere invasivo
            except Forbidden:
                 self.logger.warning(f"Impossibile inviare/pinnare messaggio Night Mode in {group_name_for_log} ({chat_id}). Permessi insufficienti?")
            except Exception as e:
                self.logger.error(f"Errore invio messaggio Night Mode in {group_name_for_log} ({chat_id}): {e}")


        else: # Disattiva Night Mode
            # Ripristina permessi
            if chat_id in self.original_group_permissions:
                await bot.set_chat_permissions(chat_id, self.original_group_permissions[chat_id])
                del self.original_group_permissions[chat_id]
                self.logger.info(f"Permessi originali ripristinati per {group_name_for_log} ({chat_id}).")
            else:
                # Fallback a permessi generici "tutto aperto"
                # Stessa logica di compatibilità di prima
                try:
                    default_permissions = ChatPermissions(
                        can_send_messages=True, can_send_audios=True, can_send_documents=True,
                        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                        can_add_web_page_previews=True, can_change_info=False, # Admin only
                        can_invite_users=True, can_pin_messages=False # Admin only
                    )
                except TypeError:
                    self.logger.debug("Usando ChatPermissions con meno parametri per ripristino (fallback).")
                    default_permissions = ChatPermissions(can_send_messages=True, can_invite_users=True)
                await bot.set_chat_permissions(chat_id, default_permissions)
                self.logger.info(f"Permessi predefiniti ripristinati per {group_name_for_log} ({chat_id}).")

            self.logger.info(f"☀️ Night Mode DISATTIVATA per {group_name_for_log} ({chat_id}).")

            # Rimuovi messaggio di notifica pinnato
            if chat_id in self.night_mode_messages_sent:
                try:
                    # await bot.unpin_chat_message(chat_id, self.night_mode_messages_sent[chat_id])
                    await bot.delete_message(chat_id, self.night_mode_messages_sent[chat_id])
                    del self.night_mode_messages_sent[chat_id]
                except Exception as e:
                    self.logger.warning(f"Impossibile rimuovere messaggio Night Mode in {group_name_for_log} ({chat_id}): {e}")


    async def _task_manage_night_mode_for_all_groups(self, bot: Bot, activate: bool):
        """Task asincrono per (dis)attivare la Night Mode su tutti i gruppi configurati."""
        groups_to_manage = self.get_night_mode_groups()
        if not groups_to_manage:
            self.logger.info(f"Nessun gruppo configurato per la Night Mode. Operazione {'attivazione' if activate else 'disattivazione'} saltata.")
            return

        action_str = "ATTIVAZIONE" if activate else "DISATTIVAZIONE"
        self.logger.info(f"Inizio {action_str} Night Mode per {len(groups_to_manage)} gruppi.")
        
        # Imposta periodo di grazia solo all'attivazione
        if activate:
            self.night_mode_transition_active = True
            self.night_mode_grace_period_end = datetime.now() + timedelta(seconds=self.config_manager.get_nested('night_mode', 'grace_period_seconds', default=15))


        for chat_id in groups_to_manage:
            try:
                await self._apply_night_mode_permissions(bot, chat_id, activate)
            except Forbidden: # Errore di permessi specifici del bot nel gruppo
                self.logger.error(f"PERMESSI INSUFFICIENTI per {'attivare' if activate else 'disattivare'} Night Mode in chat {chat_id}. Il bot è admin con i permessi necessari?")
            except BadRequest as br: # Altri errori API Telegram
                self.logger.error(f"Errore BadRequest (Telegram API) per chat {chat_id} durante {action_str} Night Mode: {br}")
            except Exception as e:
                self.logger.error(f"Errore generico per chat {chat_id} durante {action_str} Night Mode: {e}", exc_info=True)
        
        # Termina periodo di grazia solo dopo disattivazione o se non si attiva
        if not activate:
            self.night_mode_transition_active = False
            self.night_mode_grace_period_end = None

        self.logger.info(f"Completata {action_str} Night Mode per i gruppi configurati.")


    def _scheduled_activate_night_mode(self):
        """Metodo chiamato da `schedule` per attivare la Night Mode."""
        if not self._acquire_lock("night_mode_activation"): return
        try:
            self.logger.info("Attivazione programmata della Night Mode...")
            self._safe_run_coroutine(
                lambda bot: self._task_manage_night_mode_for_all_groups(bot, activate=True),
                description="attivazione Night Mode programmata"
            )
        finally:
            self._release_lock("night_mode_activation")

    def _scheduled_deactivate_night_mode(self):
        """Metodo chiamato da `schedule` per disattivare la Night Mode."""
        if not self._acquire_lock("night_mode_deactivation"): return
        try:
            self.logger.info("Disattivazione programmata della Night Mode...")
            self._safe_run_coroutine(
                lambda bot: self._task_manage_night_mode_for_all_groups(bot, activate=False),
                description="disattivazione Night Mode programmata"
            )
        finally:
            self._release_lock("night_mode_deactivation")

    def _schedule_night_mode_jobs(self):
        """Pianifica i job per attivare e disattivare la Night Mode."""
        nm_config = self.config_manager.get('night_mode', {})
        if not nm_config.get('enabled', True):
            self.logger.info("Night Mode disabilitata nella configurazione. Nessun job pianificato.")
            return

        start_str = nm_config.get('start_hour', '23:00')
        end_str = nm_config.get('end_hour', '07:00')

        try:
            # Valida formato ora
            datetime.strptime(start_str, '%H:%M')
            datetime.strptime(end_str, '%H:%M')

            schedule.every().day.at(start_str).do(self._scheduled_activate_night_mode).tag('night_mode')
            schedule.every().day.at(end_str).do(self._scheduled_deactivate_night_mode).tag('night_mode')
            self.logger.info(f"Night Mode pianificata: ON @ {start_str}, OFF @ {end_str}.")
        except ValueError:
            self.logger.error(f"Formato ora non valido per Night Mode ('{start_str}' o '{end_str}'). Job non pianificati.")


    # --- Message Handlers ---
    async def _handle_message_moderation(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_edited: bool = False):
        """Logica di moderazione centrale per messaggi nuovi o modificati."""
        message = update.effective_message
        if not message or not message.text or not message.text.strip():
            return

        self.bot_stats['total_messages_processed'] += 1
        if is_edited:
            self.bot_stats['edited_messages_detected'] += 1

        user = message.from_user
        chat = message.chat
        
        user_id = user.id
        username = user.username or f"UserID_{user.id}"
        chat_id = chat.id
        group_name = chat.title or f"ChatPrivata_{chat_id}"
        message_text = message.text
        message_id = message.message_id

        # Aggiungi messaggio alla cache per "first N messages" e spam cross-gruppo
        if not is_edited: # Solo per messaggi nuovi
            self.message_cache.add_message(chat_id, user_id, message_id, message_text)

        # 1. Utenti esenti (admin)
        exempt_users_list = self.config_manager.get('exempt_users', [])
        if user_id in exempt_users_list or username in exempt_users_list:
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da utente esente {username} ({user_id}) in {group_name} ({chat_id}).")
            self.sheets_manager.save_admin_message(message_text, user_id, username, chat_id, group_name)
            return

        # 2. Controllo Night Mode (solo per messaggi nuovi, gli edit non dovrebbero passare se NM attiva)
        # Se la night mode è attiva e non siamo nel periodo di grazia, i messaggi degli utenti normali
        # non dovrebbero nemmeno arrivare perché i permessi sono ristretti. Se arrivano, è anomalo.
        # Potrebbe essere un admin (già gestito sopra) o un bug.
        # La logica di cancellazione durante night mode era nel codice originale, la manteniamo per sicurezza.
        if self.is_night_mode_period_active(chat_id):
            is_in_grace_period = self.night_mode_transition_active and \
                                 self.night_mode_grace_period_end and \
                                 datetime.now() < self.night_mode_grace_period_end
            if not is_in_grace_period:
                self.logger.warning(
                    f"Messaggio ricevuto da {username} ({user_id}) in {group_name} ({chat_id}) "
                    "durante Night Mode attiva (fuori periodo di grazia). Cancellazione."
                )
                # Non dovrebbe accadere se i permessi sono impostati correttamente.
                # Questo è un fallback.
                try:
                    await context.bot.delete_message(chat_id, message_id)
                    self.bot_stats['messages_deleted_total'] += 1
                    if is_edited: self.bot_stats['edited_messages_deleted'] += 1
                except Exception as e:
                    self.logger.error(f"Errore cancellazione messaggio durante Night Mode anomala: {e}")
                return


        # 3. Utenti già bannati (controllo su Google Sheets)
        if self.sheets_manager.is_user_banned(user_id):
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da utente già bannato {username} ({user_id}). Cancellazione.")
            self.sheets_manager.save_message(
                message_text, user_id, username, chat_id, group_name,
                approvato=False, domanda=False, motivo_rifiuto=f"utente bannato (msg {'editato' if is_edited else 'nuovo'})"
            )
            try:
                await context.bot.delete_message(chat_id, message_id)
                self.bot_stats['messages_deleted_total'] += 1
                if is_edited: self.bot_stats['edited_messages_deleted'] += 1
            except Exception as e:
                self.logger.error(f"Errore cancellazione msg da utente bannato: {e}")
            return

        # 4. Spam Cross-Gruppo (solo per messaggi nuovi, e prima di analisi costose)
        if not is_edited:
            is_spam, groups_involved, similarity = self.cross_group_spam_detector.add_message(user_id, message_text, chat_id)
            if is_spam:
                self.logger.warning(
                    f"Possibile SPAM CROSS-GRUPPO da {username} ({user_id}) in {len(groups_involved)} gruppi "
                    f"(similarità: {similarity:.2f}). Messaggio: '{message_text[:50]}...'"
                )
                # Analizza il contenuto specifico per decidere se bannare
                is_inappropriate_content, _, _ = self.moderation_logic.analyze_with_openai(message_text)
                is_direct_banned = self.moderation_logic.contains_banned_word(message_text)

                if is_inappropriate_content or is_direct_banned:
                    self.logger.warning(f"Contenuto SPAM CROSS-GRUPPO confermato come inappropriato. Ban e pulizia.")
                    self.sheets_manager.ban_user(user_id, username, f"Spam cross-gruppo (similarità {similarity:.2f})")
                    self.sheets_manager.save_message(
                        message_text, user_id, username, chat_id, group_name,
                        approvato=False, domanda=False,
                        motivo_rifiuto=f"spam cross-gruppo inappropriato (similarità {similarity:.2f})"
                    )
                    try: # Cancella messaggio corrente
                        await context.bot.delete_message(chat_id, message_id)
                        self.bot_stats['messages_deleted_total'] += 1
                        self.bot_stats['messages_deleted_by_direct_filter'] +=1 # Consideriamolo un filtro diretto
                    except Exception as e:
                        self.logger.error(f"Errore cancellazione msg spam cross-gruppo corrente: {e}")
                    
                    # Pulizia messaggi precedenti nei gruppi coinvolti
                    await self._delete_recent_user_messages_from_cache(context, user_id, username, groups_involved)
                    await self._send_temporary_notification(context, chat_id, "❌ Messaggio eliminato per spam cross-gruppo.")
                    return
                else:
                    self.logger.info("Cross-posting rilevato, ma contenuto sembra legittimo. Nessun ban/delete automatico.")
                    # Il messaggio procederà con la normale analisi


        # --- Inizio analisi contenuto ---
        action_taken = False
        motivo_finale_rifiuto = ""
        
        # 5. Filtro diretto (parole/pattern bannati ad alta confidenza)
        if self.moderation_logic.contains_banned_word(message_text):
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da {username} contiene parole/pattern bannati.")
            motivo_finale_rifiuto = f"parole/pattern bannati (msg {'editato' if is_edited else 'nuovo'})"
            action_taken = True
            self.bot_stats['messages_deleted_by_direct_filter'] += 1
            self.moderation_logic.stats['direct_filter_matches'] +=1 # Statistica della classe moderation_logic
            
            # Ban se è un messaggio editato sospetto o un primo messaggio chiaramente spam
            first_msg_threshold = self.config_manager.get('first_messages_threshold', 3)
            is_first_few = self.message_cache.is_first_few_messages(chat_id, user_id, first_msg_threshold)
            if is_edited or (is_first_few and not is_edited) : # Ban più aggressivo per edit o primi messaggi
                self.logger.warning(f"Ban per {username} ({user_id}): {'messaggio editato' if is_edited else 'primo messaggio'} con parole bannate.")
                self.sheets_manager.ban_user(user_id, username, f"{'Edit ' if is_edited else 'Primo msg '}con contenuto bannato")
                motivo_finale_rifiuto += " - Ban applicato"


        # 6. Analisi AI (OpenAI o fallback) se non già bloccato dal filtro diretto
        is_inappropriate_ai, is_question_ai, is_disallowed_lang_ai = (False, False, False)
        if not action_taken:
            is_inappropriate_ai, is_question_ai, is_disallowed_lang_ai = self.moderation_logic.analyze_with_openai(message_text)
            if is_inappropriate_ai:
                self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da {username} rilevato come inappropriato da AI.")
                motivo_finale_rifiuto = f"contenuto inappropriato (AI) (msg {'editato' if is_edited else 'nuovo'})"
                action_taken = True
                self.bot_stats['messages_deleted_by_ai_filter'] += 1
                
                # Ban se è un messaggio editato sospetto o un primo messaggio chiaramente spam via AI
                first_msg_threshold = self.config_manager.get('first_messages_threshold', 3)
                is_first_few = self.message_cache.is_first_few_messages(chat_id, user_id, first_msg_threshold)
                if is_edited or (is_first_few and not is_edited):
                    self.logger.warning(f"Ban per {username} ({user_id}): {'messaggio editato' if is_edited else 'primo messaggio'} inappropriato (AI).")
                    self.sheets_manager.ban_user(user_id, username, f"{'Edit ' if is_edited else 'Primo msg '} inappropriato (AI)")
                    motivo_finale_rifiuto += " - Ban applicato (AI)"


            elif is_disallowed_lang_ai:
                self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da {username} rilevato in lingua non consentita da AI/Langdetect.")
                motivo_finale_rifiuto = f"lingua non consentita (msg {'editato' if is_edited else 'nuovo'})"
                action_taken = True
                self.bot_stats['messages_deleted_by_ai_filter'] += 1 # Anche la lingua conta come filtro AI qui
                # Generalmente non si banna per lingua al primo colpo, a meno che non sia spam palese
                if is_edited : # Ban più aggressivo per edit
                     self.logger.warning(f"Ban per {username} ({user_id}): messaggio editato in lingua non consentita.")
                     self.sheets_manager.ban_user(user_id, username, "Edit in lingua non consentita")
                     motivo_finale_rifiuto += " - Ban applicato (lingua edit)"


        # 7. Azione finale (delete, notifica) e salvataggio log
        if action_taken:
            self.sheets_manager.save_message(
                message_text, user_id, username, chat_id, group_name,
                approvato=False, domanda=is_question_ai, motivo_rifiuto=motivo_finale_rifiuto
            )
            try:
                await context.bot.delete_message(chat_id, message_id)
                self.bot_stats['messages_deleted_total'] += 1
                if is_edited: self.bot_stats['edited_messages_deleted'] += 1
                
                notif_text = "❌ Messaggio eliminato."
                if "parole/pattern bannati" in motivo_finale_rifiuto:
                    notif_text = "❌ Messaggio eliminato: contiene termini non permessi."
                elif "inappropriato (AI)" in motivo_finale_rifiuto:
                    notif_text = "❌ Messaggio eliminato: contenuto non conforme alle regole."
                elif "lingua non consentita" in motivo_finale_rifiuto:
                    notif_text = "❌ Messaggio eliminato: si prega di usare una lingua consentita."
                if "Ban applicato" in motivo_finale_rifiuto:
                    notif_text += " L'utente è stato sanzionato."

                await self._send_temporary_notification(context, chat_id, notif_text)

            except Exception as e:
                self.logger.error(f"Errore cancellazione messaggio ({motivo_finale_rifiuto}): {e}")
        else:
            # Messaggio approvato
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da {username} approvato. Domanda: {is_question_ai}.")
            self.sheets_manager.save_message(
                message_text, user_id, username, chat_id, group_name,
                approvato=True, domanda=is_question_ai, motivo_rifiuto=""
            )
            
    async def filter_new_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler per i nuovi messaggi testuali."""
        await self._handle_message_moderation(update, context, is_edited=False)

    async def filter_edited_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler per i messaggi testuali modificati."""
        await self._handle_message_moderation(update, context, is_edited=True)

    async def _delete_recent_user_messages_from_cache(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, chat_ids: List[int]):
        """Elimina i messaggi recenti di un utente (dalla cache) dai gruppi specificati."""
        deleted_count = 0
        for cid in chat_ids:
            # Ottieni il nome del gruppo per il foglio
            group_name_for_sheets = f"Chat {cid}"
            try:
                chat_details = await context.bot.get_chat(cid)
                group_name_for_sheets = chat_details.title or group_name_for_sheets
            except Exception: pass

            recent_messages_in_chat = self.message_cache.get_recent_messages(cid, user_id)
            for msg_id, msg_text in recent_messages_in_chat:
                try:
                    self.sheets_manager.save_message(
                        msg_text or "[Testo non disponibile]", user_id, username, cid, group_name_for_sheets,
                        approvato=False, domanda=False,
                        motivo_rifiuto="pulizia automatica per spam cross-gruppo"
                    )
                    await context.bot.delete_message(cid, msg_id)
                    deleted_count += 1
                    self.bot_stats['messages_deleted_total'] += 1
                except Exception as e:
                    self.logger.warning(f"Impossibile eliminare vecchio messaggio {msg_id} in chat {cid} per spam cross-gruppo: {e}")
        self.logger.info(f"Pulizia spam cross-gruppo: eliminati {deleted_count} messaggi precedenti di {username} ({user_id}).")


    async def _send_temporary_notification(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, duration_seconds: int = 7):
        """Invia un messaggio di notifica che si auto-elimina."""
        try:
            sent_msg = await context.bot.send_message(chat_id, text)
            # Crea un task per eliminare il messaggio dopo `duration_seconds`
            asyncio.create_task(self._delete_message_after_delay(context, chat_id, sent_msg.message_id, duration_seconds))
        except Forbidden: # Il bot non può scrivere in questa chat (es. rimosso, bannato)
            self.logger.warning(f"Impossibile inviare notifica temporanea a chat {chat_id}: permessi insufficienti.")
        except Exception as e:
            self.logger.error(f"Errore invio notifica temporanea a chat {chat_id}: {e}")

    async def _delete_message_after_delay(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay_seconds: int):
        """Coroutine helper per eliminare un messaggio dopo un ritardo."""
        await asyncio.sleep(delay_seconds)
        try:
            await context.bot.delete_message(chat_id, message_id)
        except Exception: # Il messaggio potrebbe essere già stato eliminato, o il bot non ha più i permessi
            pass # Silently ignore

    # --- Command Handlers ---
    async def _generic_admin_command_executor(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                            command_name: str,
                                            action_coroutine_provider: callable, # func(bot, chat_id?) -> coroutine
                                            success_message: str, failure_message: str,
                                            group_specific: bool = False):
        """Esecutore generico per comandi admin, con controllo permessi e lock."""
        user = update.effective_user
        if not user: return

        exempt_users_list = self.config_manager.get('exempt_users', [])
        if not (user.id in exempt_users_list or (user.username and user.username in exempt_users_list)):
            await update.message.reply_text("❌ Non sei autorizzato a eseguire questo comando.")
            return

        lock_name = command_name
        chat_id_for_action = None
        if group_specific:
            chat_id_for_action = update.effective_chat.id
            lock_name = f"{command_name}_{chat_id_for_action}"
        
        if not self._acquire_lock(lock_name):
            await update.message.reply_text(f"⏳ Operazione '{command_name}' già in corso. Riprova tra poco.")
            return
        
        try:
            await update.message.reply_text(f"⚙️ Esecuzione comando '{command_name}' in corso...")
            
            # L'action_coroutine_provider DEVE restituire una coroutine.
            # Se l'azione è per un gruppo specifico, passiamo il chat_id.
            if group_specific and chat_id_for_action is not None:
                action_coro = action_coroutine_provider(context.bot, chat_id_for_action)
            else:
                action_coro = action_coroutine_provider(context.bot) # Per azioni globali

            # Eseguiamo la coroutine direttamente dato che siamo in un handler async
            await action_coro
            
            await update.message.reply_text(success_message)
            self.logger.info(f"Comando '{command_name}' eseguito con successo da {user.username} ({user.id}).")

        except Exception as e:
            self.logger.error(f"Errore durante l'esecuzione del comando '{command_name}': {e}", exc_info=True)
            await update.message.reply_text(failure_message)
        finally:
            self._release_lock(lock_name)


    async def backup_now_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Attiva manualmente un backup dei dati di Google Sheets."""
        # backup_sheets_to_csv è sincrono, quindi va eseguito in un thread per non bloccare l'async loop
        def sync_backup_action(_bot_unused): # _bot_unused per compatibilità con provider
            return asyncio.to_thread(self.backup_manager.backup_sheets_to_csv)

        await self._generic_admin_command_executor(
            update, context, "backup_now",
            action_coroutine_provider=sync_backup_action,
            success_message="✅ Backup dei fogli Google Sheets completato!",
            failure_message="❌ Errore durante il backup manuale. Controlla i log.",
            group_specific=False
        )

    async def night_mode_on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Attiva manualmente la Night Mode nel gruppo corrente."""
        chat_id = update.effective_chat.id
        if chat_id not in self.get_night_mode_groups():
            await update.message.reply_text("⚠️ Questo gruppo non è configurato per la Night Mode. Aggiungilo in `config.json`.")
            return

        async def action(bot: Bot, cid: int): # cid è chat_id
            await self._apply_night_mode_permissions(bot, cid, activate=True)
            self.night_mode_transition_active = True # Abilita periodo di grazia
            self.night_mode_grace_period_end = datetime.now() + timedelta(seconds=self.config_manager.get_nested('night_mode', 'grace_period_seconds', default=15))


        await self._generic_admin_command_executor(
            update, context, "night_on_manual",
            action_coroutine_provider=action,
            success_message="🌙 Night Mode attivata manualmente per questo gruppo.",
            failure_message="❌ Errore attivazione manuale Night Mode.",
            group_specific=True
        )

    async def night_mode_off_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Disattiva manualmente la Night Mode nel gruppo corrente."""
        chat_id = update.effective_chat.id
        if chat_id not in self.get_night_mode_groups(): # Anche se non è in NM, permettiamo di "forzare" lo stato giorno
            self.logger.info(f"Comando /night_off in gruppo {chat_id} non in lista NM, ma procedo per sicurezza.")
            # await update.message.reply_text("ℹ️ Questo gruppo non sembra essere in modalità notturna programmata.")
            # return # O permettere comunque la disattivazione forzata. Scelgo quest'ultima.

        async def action(bot: Bot, cid: int):
            await self._apply_night_mode_permissions(bot, cid, activate=False)
            self.night_mode_transition_active = False # Disabilita periodo di grazia
            self.night_mode_grace_period_end = None


        await self._generic_admin_command_executor(
            update, context, "night_off_manual",
            action_coroutine_provider=action,
            success_message="☀️ Night Mode disattivata manualmente per questo gruppo.",
            failure_message="❌ Errore disattivazione manuale Night Mode.",
            group_specific=True
        )

    async def night_mode_all_on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Attiva manualmente la Night Mode su tutti i gruppi configurati."""
        await self._generic_admin_command_executor(
            update, context, "night_on_all_manual",
            action_coroutine_provider=lambda bot: self._task_manage_night_mode_for_all_groups(bot, activate=True),
            success_message="🌙 Night Mode attivata manualmente su tutti i gruppi configurati.",
            failure_message="❌ Errore attivazione manuale Night Mode globale.",
            group_specific=False
        )

    async def night_mode_all_off_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Disattiva manualmente la Night Mode su tutti i gruppi configurati."""
        await self._generic_admin_command_executor(
            update, context, "night_off_all_manual",
            action_coroutine_provider=lambda bot: self._task_manage_night_mode_for_all_groups(bot, activate=False),
            success_message="☀️ Night Mode disattivata manualmente su tutti i gruppi configurati.",
            failure_message="❌ Errore disattivazione manuale Night Mode globale.",
            group_specific=False
        )

    async def show_stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra statistiche di moderazione (solo per admin)."""
        user = update.effective_user
        if not user: return
        exempt_users_list = self.config_manager.get('exempt_users', [])
        if not (user.id in exempt_users_list or (user.username and user.username in exempt_users_list)):
            await update.message.reply_text("❌ Non sei autorizzato a visualizzare le statistiche.")
            return

        mod_stats = self.moderation_logic.get_stats()
        
        stats_msg = "📊 **Statistiche Moderazione Bot** 📊\n\n"
        stats_msg += f"🔹 **Messaggi Processati:** {self.bot_stats['total_messages_processed']}\n"
        stats_msg += f"🔹 **Messaggi Eliminati Totali:** {self.bot_stats['messages_deleted_total']}\n"
        stats_msg += f"  ▫️ Da Filtro Diretto: {self.bot_stats['messages_deleted_by_direct_filter']}\n"
        stats_msg += f"  ▫️ Da Analisi AI: {self.bot_stats['messages_deleted_by_ai_filter']}\n"
        stats_msg += f"🔹 **Messaggi Modificati Rilevati:** {self.bot_stats['edited_messages_detected']}\n"
        stats_msg += f"  ▫️ Modificati ed Eliminati: {self.bot_stats['edited_messages_deleted']}\n\n"
        
        stats_msg += "🔍 **Statistiche Analisi Contenuto (OpenAI & Cache):**\n"
        stats_msg += f"🔸 Richieste OpenAI Effettuate: {mod_stats['openai_requests']}\n"
        stats_msg += f"🔸 Risultati da Cache OpenAI: {mod_stats['openai_cache_hits']}\n"
        stats_msg += f"🔸 Tasso Hit Cache: {mod_stats['cache_hit_rate']:.2%}\n"
        stats_msg += f"🔸 Dimensione Cache Analisi: {mod_stats['cache_size']}\n"
        stats_msg += f"🔸 Violazioni rilevate da AI: {mod_stats['ai_filter_violations']}\n" # Dalla classe moderation_logic
        stats_msg += f"🔸 Match filtro diretto (logica): {mod_stats['direct_filter_matches']}\n\n" # Dalla classe moderation_logic

        # Potremmo aggiungere info sui lock attivi, stato night mode, etc.
        active_locks = [k for k, v in self._operation_locks.items() if v] # Non usato più os.path.exists
        if active_locks: # Ora _operation_locks non è più usato, i lock sono su file. Si potrebbe listare i file .lock
            stats_msg += f"🔒 Lock operativi attivi: {', '.join(active_locks)}\n"
        
        # Conteggio gruppi in Night Mode attuale
        nm_groups_now = [gid for gid in self.get_night_mode_groups() if self.is_night_mode_period_active(gid)]
        stats_msg += f"🌙 Gruppi attualmente in Night Mode (da orario): {len(nm_groups_now)}\n"

        await update.message.reply_text(stats_msg, parse_mode='Markdown')


    # --- Bot Lifecycle & Scheduler ---
    def _run_scheduler_thread(self):
        """Esegue i task programmati in un thread separato."""
        self.logger.info("Thread dello scheduler avviato.")
        while self.scheduler_active:
            try:
                schedule.run_pending()
            except Exception as e: # Cattura eccezioni generiche nello scheduler
                self.logger.error(f"Errore imprevisto nello scheduler: {e}", exc_info=True)
            time.sleep(self.config_manager.get('scheduler_check_interval_seconds', 60)) # Controlla ogni minuto
        self.logger.info("Thread dello scheduler terminato.")

    def _check_night_mode_on_startup(self):
        """Verifica e applica lo stato della Night Mode all'avvio del bot."""
        if not self._acquire_lock("startup_night_mode_check"):
            self.logger.info("Controllo Night Mode all'avvio già in corso o completato da altra istanza.")
            return
        try:
            self.logger.info("Controllo stato Night Mode all'avvio...")
            # Determina se globalmente dovrebbe essere attiva
            # Usiamo -1 per controllare solo l'orario, senza specificare un gruppo
            should_be_active = self.is_night_mode_period_active(-1)
            
            self._safe_run_coroutine(
                lambda bot: self._task_manage_night_mode_for_all_groups(bot, activate=should_be_active),
                description="controllo Night Mode all'avvio"
            )
        finally:
            self._release_lock("startup_night_mode_check")


    def start(self):
        """Avvia il bot, imposta gli handler e inizia il polling."""
        self.logger.info(f"Avvio del Bot di Moderazione Telegram (PID: {os.getpid()})...")
        self.logger.info(f"Directory di lavoro corrente: {os.getcwd()}")

        self.application = Application.builder().token(self.token).build()

        # Handlers per messaggi
        # Messaggi normali (testo, non comandi)
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, 
            self.filter_new_messages
        ))
        # Messaggi modificati (testo, non comandi)
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.UpdateType.EDITED_MESSAGE, 
            self.filter_edited_messages
        ))

        # Handlers per comandi admin
        self.application.add_handler(CommandHandler("stats", self.show_stats_command))
        self.application.add_handler(CommandHandler("backup", self.backup_now_command))
        self.application.add_handler(CommandHandler("nighton", self.night_mode_on_command))
        self.application.add_handler(CommandHandler("nightoff", self.night_mode_off_command))
        self.application.add_handler(CommandHandler("nightonall", self.night_mode_all_on_command))
        self.application.add_handler(CommandHandler("nightoffall", self.night_mode_all_off_command))

        # Pianifica Night Mode
        self._schedule_night_mode_jobs()

        # Avvia thread per scheduler (backup, pulizia cache, night mode)
        import threading
        self.scheduler_thread = threading.Thread(target=self._run_scheduler_thread, daemon=True)
        self.scheduler_thread.start()
        
        # Verifica Night Mode all'avvio (dopo un breve delay per permettere al bot di connettersi)
        # Lo facciamo in un thread separato per non bloccare l'avvio del polling
        # startup_nm_check_thread = threading.Thread(target=lambda: (time.sleep(30), self._check_night_mode_on_startup()), daemon=True)
        # startup_nm_check_thread.start()

        self.logger.info("Bot configurato e pronto. Avvio polling...")
        try:
            self.application.run_polling(
                allowed_updates=Update.ALL_TYPES, # o specifica i tipi per efficienza
                drop_pending_updates=self.config_manager.get('drop_pending_updates_on_start', True)
            )
        except KeyboardInterrupt:
            self.logger.info("Polling interrotto da tastiera (Ctrl+C).")
        except Exception as e:
            self.logger.critical(f"Errore critico durante il polling: {e}", exc_info=True)
        finally:
            self.stop()

    def stop(self):
        """Ferma lo scheduler e altre operazioni in preparazione alla chiusura."""
        self.logger.info("Arresto del bot in corso...")
        self.scheduler_active = False # Segnala al thread dello scheduler di terminare
        
        # Qui si potrebbero aggiungere altre logiche di cleanup, se necessario
        # ad esempio, aspettare che il thread dello scheduler termini (con un join e timeout)

        # Rimuovi i file di lock se esistono (opzionale, dipende dalla strategia)
        # Potrebbe essere meglio lasciarli e farli scadere per evitare race conditions su riavvii rapidi.
        # self._release_lock("night_mode_activation") # Esempio
        # self._release_lock("night_mode_deactivation")
        # self._release_lock("startup_night_mode_check")


        self.logger.info("Bot arrestato.")
