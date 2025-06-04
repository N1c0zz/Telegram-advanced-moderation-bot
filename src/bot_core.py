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
from telegram.error import NetworkError, TimedOut

# Importazioni locali dal package src
from .config_manager import ConfigManager
from .logger_config import LoggingConfigurator
from .moderation_rules import AdvancedModerationBotLogic
from .cache_utils import MessageCache
from .spam_detection import CrossGroupSpamDetector
from .user_counters import UserMessageCounters
from .csv_interface import CSVDataManager
# Nuova importazione per il sistema di gestione utenti
from .user_management import UserManagementSystem


class TelegramModerationBot:
    """
    Classe principale del bot Telegram per la moderazione avanzata.
    Orchestra i vari componenti come configurazione, logging, 
    logica di moderazione, e gestione dei comandi/messaggi Telegram.
    
    AGGIORNAMENTO: Rimosso Google Sheets, solo CSV. Aggiunto sistema di gestione utenti.
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

        # RIMOZIONE: Non più Google Sheets
        # self.sheets_manager = GoogleSheetsManager(self.logger, self.config_manager)

        # CSV Manager (ora sistema principale)
        self.csv_manager = CSVDataManager(self.logger, self.config_manager)
        
        # NUOVO: Sistema di gestione utenti integrato
        self.user_manager = UserManagementSystem(self.logger, self.csv_manager, self.config_manager)
        
        self.moderation_logic = AdvancedModerationBotLogic(self.config_manager, self.logger)

        # RIMOZIONE: Non più backup Google Sheets
        # Non serve più SheetBackupManager dato che gestiamo solo CSV

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

        self.user_counters = UserMessageCounters(integrity_check=True, logger=self.logger)

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
            'messages_deleted_by_direct_filter': 0,
            'messages_deleted_by_ai_filter': 0,
            'edited_messages_detected': 0,
            'edited_messages_deleted': 0,
            'users_banned_total': 0,  # NUOVO: Contatore ban
            'users_unbanned_total': 0,  # NUOVO: Contatore unban
        }
        self.application: Optional[Application] = None
        self._operation_locks: Dict[str, bool] = {}

        # NUOVO: Stato di running per dashboard
        self._is_running: bool = False
        self._start_time: Optional[datetime] = None

    # --- NUOVI METODI PER DASHBOARD ---
    
    def get_bot_status(self) -> Dict[str, Any]:
        """Restituisce lo stato attuale del bot per la dashboard."""
        uptime = None
        if self._start_time:
            uptime = int((datetime.now() - self._start_time).total_seconds())
            
        return {
            'is_running': self._is_running,
            'start_time': self._start_time.isoformat() if self._start_time else None,
            'uptime_seconds': uptime,
            'night_mode_active': self.is_night_mode_period_active(-1),
            'scheduler_active': self.scheduler_active,
            'stats': self.get_comprehensive_stats()
        }
    
    def get_comprehensive_stats(self) -> Dict[str, Any]:
        """Restituisce statistiche complete per la dashboard."""
        mod_stats = self.moderation_logic.get_stats()
        counter_stats = self.user_counters.get_stats()
        csv_stats = self.csv_manager.get_csv_stats()
        
        return {
            'bot_stats': self.bot_stats,
            'moderation_stats': mod_stats,
            'user_counter_stats': counter_stats,
            'csv_stats': csv_stats,
            'night_mode_groups_count': len(self.get_night_mode_groups()),
            'cache_stats': {
                'message_cache_size': len(self.message_cache.messages),
                'analysis_cache_size': len(self.moderation_logic.analysis_cache.cache)
            }
        }

    def get_recent_messages(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Restituisce gli ultimi messaggi processati per la dashboard."""
        return self.csv_manager.read_csv_data("messages", limit=limit)
    
    def get_recent_deleted_messages(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Restituisce gli ultimi messaggi eliminati per la dashboard."""
        all_messages = self.csv_manager.read_csv_data("messages")
        deleted_messages = [msg for msg in all_messages if msg.get('approvato') == 'NO']
        return deleted_messages[:limit] if deleted_messages else []
    
    def get_recent_banned_users(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Restituisce gli ultimi utenti bannati per la dashboard."""
        return self.csv_manager.read_csv_data("banned_users", limit=limit)

    def reload_configuration(self) -> bool:
        """Ricarica la configurazione da file per la dashboard."""
        try:
            old_config = self.config_manager.config.copy()
            self.config_manager.config = self.config_manager.load_config()
            
            # Aggiorna componenti che dipendono dalla configurazione
            self.moderation_logic.banned_words = self.config_manager.get('banned_words', [])
            self.moderation_logic.whitelist_words = self.config_manager.get('whitelist_words', [])
            self.moderation_logic.allowed_languages = self.config_manager.get('allowed_languages', ["it"])
            
            # Re-schedule night mode se gli orari sono cambiati
            if (old_config.get('night_mode', {}) != self.config_manager.get('night_mode', {})):
                schedule.clear('night_mode')
                self._schedule_night_mode_jobs()
            
            self.logger.info("Configurazione ricaricata con successo dalla dashboard")
            return True
        except Exception as e:
            self.logger.error(f"Errore ricaricamento configurazione: {e}")
            return False

    # --- API per ban/unban da dashboard ---
    
    async def ban_user_from_dashboard(self, user_id: int, reason: str = "Ban da dashboard") -> Dict[str, Any]:
        """Banna un utente da tutti i gruppi (chiamata da dashboard)."""
        try:
            # Ban logico nel database
            username = f"UserID_{user_id}"
            ban_success = self.csv_manager.ban_user(user_id, username, reason)
            
            if not ban_success:
                return {"success": False, "message": "Errore nel salvataggio del ban nel database"}
            
            # Ban fisico dai gruppi Telegram
            if self.application and self.application.bot:
                target_groups = self.get_night_mode_groups()
                results = await self._execute_multi_group_ban(self.application.bot, user_id, target_groups, reason)
                success_count = sum(results.values())
                
                self.bot_stats['users_banned_total'] += 1
                
                return {
                    "success": True,
                    "message": f"Utente {user_id} bannato con successo",
                    "groups_banned": success_count,
                    "total_groups": len(target_groups),
                    "details": results
                }
            else:
                # Solo ban logico se il bot non è attivo
                return {
                    "success": True,
                    "message": f"Ban logico applicato. Ban fisico dai gruppi verrà applicato al prossimo avvio del bot.",
                    "groups_banned": 0,
                    "total_groups": 0
                }
                
        except Exception as e:
            self.logger.error(f"Errore ban utente da dashboard: {e}")
            return {"success": False, "message": f"Errore: {str(e)}"}

    async def unban_user_from_dashboard(self, user_id: int, unban_reason: str = "Unban da dashboard") -> Dict[str, Any]:
        """Sbanna un utente da tutti i gruppi (chiamata da dashboard)."""
        try:
            # 1. UNBAN LOGICO: Rimuovi dal CSV banned_users
            csv_unban_success = self.csv_manager.unban_user(
                user_id=user_id, 
                unban_reason=unban_reason, 
                unbanned_by="dashboard"
            )
            
            if not csv_unban_success:
                return {
                    "success": False,
                    "message": f"Errore durante rimozione dell'utente {user_id} dal database CSV"
                }
            
            # 2. UNBAN FISICO: Rimuovi dai gruppi Telegram
            telegram_results = {}
            telegram_success_count = 0
            
            if self.application and self.application.bot:
                target_groups = self.get_night_mode_groups()
                
                for chat_id in target_groups:
                    try:
                        await self.application.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                        telegram_results[chat_id] = True
                        telegram_success_count += 1
                    except Exception as e:
                        telegram_results[chat_id] = False
                        self.logger.warning(f"Errore unban Telegram utente {user_id} da gruppo {chat_id}: {e}")
                
                self.bot_stats['users_unbanned_total'] += 1
                
                return {
                    "success": True,
                    "message": f"Utente {user_id} unbannato completamente",
                    "csv_unban": True,
                    "telegram_unban": {
                        "groups_unbanned": telegram_success_count,
                        "total_groups": len(target_groups),
                        "details": telegram_results
                    },
                    "note": "Utente rimosso dal database e dai gruppi Telegram. Storico mantenuto in unban_history."
                }
            else:
                # Solo unban logico se bot non attivo
                return {
                    "success": True,
                    "message": f"Utente {user_id} rimosso dal database",
                    "csv_unban": True,
                    "telegram_unban": {
                        "groups_unbanned": 0,
                        "total_groups": 0,
                        "details": {}
                    },
                    "note": "Bot non attivo. Solo unban logico completato. Eseguire unban Telegram manualmente quando bot sarà attivo."
                }
                
        except Exception as e:
            self.logger.error(f"Errore unban utente da dashboard: {e}")
            return {"success": False, "message": f"Errore: {str(e)}"}

    # --- Lock Management (invariato) ---
    def _acquire_lock(self, operation_name: str, timeout: int = 60) -> bool:
        """Tenta di acquisire un lock basato su file per un'operazione."""
        lock_file_path = f"{operation_name}.lock"
        
        if os.path.exists(lock_file_path):
            try:
                creation_time = os.path.getmtime(lock_file_path)
                if (time.time() - creation_time) > timeout:
                    self.logger.warning(f"Lock per '{operation_name}' ({lock_file_path}) scaduto. Rimozione forzata.")
                    os.remove(lock_file_path)
                else:
                    self.logger.debug(f"Lock per '{operation_name}' ({lock_file_path}) già attivo.")
                    return False
            except OSError as e:
                self.logger.error(f"Errore nel controllo del lock file '{lock_file_path}': {e}")
                return False

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

    # --- Safe Coroutine Execution (invariato) ---
    def _safe_run_coroutine(self, coroutine_func: callable, description: str = "operazione asincrona"):
        """Esegue una coroutine in modo sicuro."""
        if self.application and hasattr(self.application, '_loop') and self.application._loop and self.application._loop.is_running():
            bot_instance = self.application.bot
            actual_coroutine = coroutine_func(bot_instance)
            future = asyncio.run_coroutine_threadsafe(actual_coroutine, self.application.loop)
            try:
                return future.result(timeout=60)
            except concurrent.futures.TimeoutError:
                self.logger.error(f"Timeout durante l'esecuzione di '{description}' sul loop principale.")
            except Exception as e_future:
                self.logger.error(f"Errore futuro durante l'esecuzione di '{description}': {e_future}", exc_info=True)
        else:
            self.logger.warning(f"Loop dell'applicazione non disponibile per '{description}'. Esecuzione in un loop temporaneo.")
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            temp_bot_instance = None
            try:
                temp_bot_instance = Bot(token=self.token)
                actual_coroutine = coroutine_func(temp_bot_instance)
                result = new_loop.run_until_complete(actual_coroutine)
                if hasattr(temp_bot_instance, '_client') and hasattr(temp_bot_instance._client, 'shutdown'):
                     new_loop.run_until_complete(temp_bot_instance._client.shutdown())
                elif hasattr(temp_bot_instance, 'shutdown'):
                     new_loop.run_until_complete(temp_bot_instance.shutdown())
                return result
            except Exception as e_inner:
                self.logger.error(f"Errore durante l'esecuzione di '{description}' nel loop temporaneo: {e_inner}", exc_info=True)
            finally:
                if temp_bot_instance:
                    try:
                        if hasattr(temp_bot_instance, '_client') and hasattr(temp_bot_instance._client, 'shutdown') and not new_loop.is_closed():
                            new_loop.run_until_complete(temp_bot_instance._client.shutdown())
                    except Exception as e_shutdown:
                        self.logger.error(f"Errore chiusura finale risorse bot temporaneo per '{description}': {e_shutdown}")

                if not new_loop.is_closed():
                    new_loop.close()
                asyncio.set_event_loop(None)
        return None

    # --- Night Mode Logic (invariato) ---
    def get_night_mode_groups(self) -> List[int]:
        """Restituisce la lista degli ID dei gruppi configurati per la Night Mode."""
        return self.config_manager.get_nested('night_mode', 'night_mode_groups', default=[])

    def is_night_mode_period_active(self, chat_id_to_check_config_for: int = -1) -> bool:
        """Verifica se l'orario corrente rientra nel periodo di Night Mode."""
        nm_config = self.config_manager.get('night_mode', {})
        if not nm_config.get('enabled', True):
            return False

        if chat_id_to_check_config_for != -1:
            if chat_id_to_check_config_for not in nm_config.get('night_mode_groups', []):
                return False

        start_str = nm_config.get('start_hour', '23:00')
        end_str = nm_config.get('end_hour', '07:00')

        try:
            start_time = datetime.strptime(start_str, '%H:%M').time()
            end_time = datetime.strptime(end_str, '%H:%M').time()
        except ValueError:
            self.logger.error(f"Formato ora Night Mode non valido: start='{start_str}', end='{end_str}'. Usare HH:MM.")
            return False

        now_time = datetime.now().time()

        if start_time <= end_time:
            return start_time <= now_time < end_time
        else:
            return now_time >= start_time or now_time < end_time

    async def _apply_night_mode_permissions(self, bot: Bot, chat_id: int, activate: bool):
        """Applica o ripristina i permessi di un gruppo per la Night Mode."""
        group_name_for_log = f"Chat {chat_id}"
        try:
            chat_info = await bot.get_chat(chat_id)
            group_name_for_log = chat_info.title or group_name_for_log
        except Exception:
            pass
        
        nm_config = self.config_manager.get('night_mode', {})

        if activate:
            if chat_id not in self.original_group_permissions:
                try:
                    current_chat = await bot.get_chat(chat_id)
                    if current_chat.permissions:
                         self.original_group_permissions[chat_id] = current_chat.permissions
                         self.logger.info(f"Permessi originali salvati per {group_name_for_log} ({chat_id}).")
                except Exception as e:
                    self.logger.warning(f"Impossibile salvare i permessi originali per {group_name_for_log} ({chat_id}): {e}")
            
            try:
                restricted_permissions = ChatPermissions(
                    can_send_messages=False, can_send_audios=False, can_send_documents=False,
                    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                    can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_change_info=False, can_invite_users=True, can_pin_messages=False
                )
            except TypeError:
                 self.logger.debug("Usando ChatPermissions con meno parametri (fallback).")
                 restricted_permissions = ChatPermissions(can_send_messages=False, can_invite_users=True)

            await bot.set_chat_permissions(chat_id, restricted_permissions)
            self.logger.info(f"🌙 Night Mode ATTIVATA per {group_name_for_log} ({chat_id}).")

            start_msg_template = nm_config.get('start_message', "⛔ Night Mode attiva fino alle {end_hour}.")
            end_hour_str = nm_config.get('end_hour', '07:00')
            start_msg = start_msg_template.format(end_hour=end_hour_str)
            
            start_msg += (
                f"\n\nℹ️ Per i nuovi membri: questa è una restrizione temporanea. "
                f"Potrai scrivere dalle {end_hour_str}."
            )
            try:
                sent_notification = await bot.send_message(chat_id, start_msg)
                if chat_id in self.night_mode_messages_sent:
                    try:
                        await bot.unpin_chat_message(chat_id, self.night_mode_messages_sent[chat_id])
                    except Exception: pass
                self.night_mode_messages_sent[chat_id] = sent_notification.message_id
            except Forbidden:
                 self.logger.warning(f"Impossibile inviare/pinnare messaggio Night Mode in {group_name_for_log} ({chat_id}). Permessi insufficienti?")
            except Exception as e:
                self.logger.error(f"Errore invio messaggio Night Mode in {group_name_for_log} ({chat_id}): {e}")

        else: # Disattiva Night Mode
            if chat_id in self.original_group_permissions:
                await bot.set_chat_permissions(chat_id, self.original_group_permissions[chat_id])
                del self.original_group_permissions[chat_id]
                self.logger.info(f"Permessi originali ripristinati per {group_name_for_log} ({chat_id}).")
            else:
                try:
                    default_permissions = ChatPermissions(
                        can_send_messages=True, can_send_audios=True, can_send_documents=True,
                        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                        can_add_web_page_previews=True, can_change_info=False,
                        can_invite_users=True, can_pin_messages=False
                    )
                except TypeError:
                    self.logger.debug("Usando ChatPermissions con meno parametri per ripristino (fallback).")
                    default_permissions = ChatPermissions(can_send_messages=True, can_invite_users=True)
                await bot.set_chat_permissions(chat_id, default_permissions)
                self.logger.info(f"Permessi predefiniti ripristinati per {group_name_for_log} ({chat_id}).")

            self.logger.info(f"☀️ Night Mode DISATTIVATA per {group_name_for_log} ({chat_id}).")

            if chat_id in self.night_mode_messages_sent:
                try:
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
        
        if activate:
            self.night_mode_transition_active = True
            self.night_mode_grace_period_end = datetime.now() + timedelta(seconds=self.config_manager.get_nested('night_mode', 'grace_period_seconds', default=15))

        for chat_id in groups_to_manage:
            try:
                await self._apply_night_mode_permissions(bot, chat_id, activate)
            except Forbidden:
                self.logger.error(f"PERMESSI INSUFFICIENTI per {'attivare' if activate else 'disattivare'} Night Mode in chat {chat_id}. Il bot è admin con i permessi necessari?")
            except BadRequest as br:
                self.logger.error(f"Errore BadRequest (Telegram API) per chat {chat_id} durante {action_str} Night Mode: {br}")
            except Exception as e:
                self.logger.error(f"Errore generico per chat {chat_id} durante {action_str} Night Mode: {e}", exc_info=True)
        
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
            datetime.strptime(start_str, '%H:%M')
            datetime.strptime(end_str, '%H:%M')

            schedule.every().day.at(start_str).do(self._scheduled_activate_night_mode).tag('night_mode')
            schedule.every().day.at(end_str).do(self._scheduled_deactivate_night_mode).tag('night_mode')
            self.logger.info(f"Night Mode pianificata: ON @ {start_str}, OFF @ {end_str}.")
        except ValueError:
            self.logger.error(f"Formato ora non valido per Night Mode ('{start_str}' o '{end_str}'). Job non pianificati.")

    # --- Message Handlers (aggiornati per rimuovere Google Sheets) ---
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

        self.logger.info(f"🔍 DEBUG: Messaggio ricevuto da {username}: '{message_text}'")
        self.logger.info(f"🔍 DEBUG: Lunghezza messaggio: {len(message_text)} caratteri")

        if not is_edited:
            self.message_cache.add_message(chat_id, user_id, message_id, message_text)
            total_user_messages = self.user_counters.increment_and_get_count(user_id, chat_id)
        else:
            total_user_messages = self.user_counters.get_count(user_id, chat_id)

        # ===== CONTROLLI PRIORITARI =====
        
        # 1. UTENTI ESENTI (ADMIN)
        exempt_users_list = self.config_manager.get('exempt_users', [])
        if user_id in exempt_users_list or username in exempt_users_list:
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da utente esente {username} ({user_id}) in {group_name} ({chat_id}).")
            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.save_admin_message(message_text, user_id, username, chat_id, group_name)
            self.csv_manager.save_admin_message(message_text, user_id, username, chat_id, group_name)
            return

        # 2. UTENTI GIÀ BANNATI - CONTROLLO CRITICO DI SICUREZZA
        # RIMOZIONE: Non più Google Sheets
        # sheets_banned = self.sheets_manager.is_user_banned(user_id)
        csv_banned = self.csv_manager.is_user_banned(user_id)
        if csv_banned:
            self.logger.info(f"🚨 UTENTE BANNATO RILEVATO: {username} ({user_id}) ha tentato di scrivere: '{message_text}'")
            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.save_message(...)
            self.csv_manager.save_message(
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

        # 3. NIGHT MODE - CONTROLLO DI SICUREZZA
        if self.is_night_mode_period_active(chat_id):
            is_in_grace_period = self.night_mode_transition_active and \
                                self.night_mode_grace_period_end and \
                                datetime.now() < self.night_mode_grace_period_end
            if not is_in_grace_period:
                self.logger.warning(
                    f"Messaggio ricevuto da {username} ({user_id}) in {group_name} ({chat_id}) "
                    "durante Night Mode attiva (fuori periodo di grazia). Cancellazione."
                )
                try:
                    await context.bot.delete_message(chat_id, message_id)
                    self.bot_stats['messages_deleted_total'] += 1
                    if is_edited: self.bot_stats['edited_messages_deleted'] += 1
                except Exception as e:
                    self.logger.error(f"Errore cancellazione messaggio durante Night Mode anomala: {e}")
                return

        # ===== AUTO-APPROVAZIONI =====
        
        # 4. Messaggi molto brevi (≤4 caratteri)
        auto_approve_short = self.config_manager.get('auto_approve_short_messages', True)
        short_max_length = self.config_manager.get('short_message_max_length', 4)
        
        if auto_approve_short and len(message_text.strip()) <= short_max_length:
            self.logger.info(f"✅ Messaggio molto breve auto-approvato da {username}: '{message_text}' (lunghezza: {len(message_text.strip())})")
            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.save_message(...)
            self.csv_manager.save_message(
                message_text, user_id, username, chat_id, group_name,
                approvato=True, domanda=False, motivo_rifiuto="auto-approvato (messaggio breve)"
            )
            return

        # 5. Whitelist critica
        if self.moderation_logic.contains_whitelist_word(message_text):
            self.logger.info(f"✅ Messaggio whitelist critica auto-approvato da {username}: '{message_text[:50]}...'")
            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.save_message(...)
            self.csv_manager.save_message(
                message_text, user_id, username, chat_id, group_name,
                approvato=True, domanda=False, motivo_rifiuto="auto-approvato (whitelist critica)"
            )
            return

        # ===== CONTROLLI DI MODERAZIONE AVANZATI =====

        # 6. Spam Cross-Gruppo
        if not is_edited:
            is_spam, groups_involved, similarity = self.cross_group_spam_detector.add_message(user_id, message_text, chat_id)
            if is_spam:
                self.logger.warning(
                    f"Possibile SPAM CROSS-GRUPPO da {username} ({user_id}) in {len(groups_involved)} gruppi "
                    f"(similarità: {similarity:.2f}). Messaggio: '{message_text[:50]}...'"
                )
                is_inappropriate_content, _, _ = self.moderation_logic.analyze_with_openai(message_text)
                is_direct_banned = self.moderation_logic.contains_banned_word(message_text)

                if is_inappropriate_content or is_direct_banned:
                    self.logger.warning(f"Contenuto SPAM CROSS-GRUPPO confermato come inappropriato. Ban e pulizia.")
                    # RIMOZIONE: Non più Google Sheets
                    # self.sheets_manager.ban_user(user_id, username, f"Spam cross-gruppo (similarità {similarity:.2f})")
                    # self.sheets_manager.save_message(...)
                    self.csv_manager.ban_user(user_id, username, f"Spam cross-gruppo (similarità {similarity:.2f})")
                    self.csv_manager.save_message(
                        message_text, user_id, username, chat_id, group_name,
                        approvato=False, domanda=False,
                        motivo_rifiuto=f"spam cross-gruppo inappropriato (similarità {similarity:.2f})"
                    )
                    self.bot_stats['users_banned_total'] += 1
                    try:
                        await context.bot.delete_message(chat_id, message_id)
                        self.bot_stats['messages_deleted_total'] += 1
                        self.bot_stats['messages_deleted_by_direct_filter'] +=1
                    except Exception as e:
                        self.logger.error(f"Errore cancellazione msg spam cross-gruppo corrente: {e}")
                    
                    await self._delete_recent_user_messages_from_cache(context, user_id, username, groups_involved)
                    await self._send_temporary_notification(context, chat_id, "❌ Messaggio eliminato per spam.")
                    return

        # 7. Filtro diretto (parole/pattern bannati)
        is_banned_direct = self.moderation_logic.contains_banned_word(message_text)
        self.logger.info(f"🔍 DEBUG: Filtro diretto dice bannato: {is_banned_direct}")
        
        if is_banned_direct:
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da {username} contiene parole/pattern bannati.")
            motivo_finale_rifiuto = f"parole/pattern bannati (msg {'editato' if is_edited else 'nuovo'})"
            self.bot_stats['messages_deleted_by_direct_filter'] += 1
            self.moderation_logic.stats['direct_filter_matches'] +=1
            
            if is_edited:
                self.logger.warning(f"Ban per {username} ({user_id}): messaggio editato inappropriato - possibile elusione moderazione.")
                # RIMOZIONE: Non più Google Sheets
                # self.sheets_manager.ban_user(user_id, username, "Messaggio editato inappropriato - elusione moderazione")
                self.csv_manager.ban_user(user_id, username, "Messaggio editato inappropriato - elusione moderazione")
                self.bot_stats['users_banned_total'] += 1
                motivo_finale_rifiuto += " - Ban applicato (edit)"
            elif total_user_messages <= self.config_manager.get('first_messages_threshold', 3):
                self.logger.warning(f"Ban per {username} ({user_id}): primo messaggio ({total_user_messages}/{self.config_manager.get('first_messages_threshold', 3)}) inappropriato.")
                # RIMOZIONE: Non più Google Sheets
                # self.sheets_manager.ban_user(user_id, username, f"Primo messaggio inappropriato (msg #{total_user_messages})")
                self.csv_manager.ban_user(user_id, username, f"Primo messaggio inappropriato (msg #{total_user_messages})")
                self.bot_stats['users_banned_total'] += 1
                motivo_finale_rifiuto += f" - Ban applicato (primo msg #{total_user_messages})"

            # Cancella e salva
            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.save_message(...)
            self.csv_manager.save_message(
                message_text, user_id, username, chat_id, group_name,
                approvato=False, domanda=False, motivo_rifiuto=motivo_finale_rifiuto
            )
            try:
                await context.bot.delete_message(chat_id, message_id)
                self.bot_stats['messages_deleted_total'] += 1
                if is_edited: self.bot_stats['edited_messages_deleted'] += 1
                
                notif_text = "❌ Messaggio eliminato. Attenersi alle linee guida del gruppo.\nScrivimi in chat il comando /rules per conoscere le regole del gruppo!\n"
                if "Ban applicato" in motivo_finale_rifiuto:
                    notif_text += " L'utente è stato sanzionato."

                await self._send_temporary_notification(context, chat_id, notif_text)
            except Exception as e:
                self.logger.error(f"Errore cancellazione messaggio ({motivo_finale_rifiuto}): {e}")
            return

        # 8. Controllo lingua di base
        is_disallowed_lang_basic = self.moderation_logic.is_language_disallowed(message_text)
        self.logger.info(f"🔍 DEBUG: Lingua non consentita (controllo base): {is_disallowed_lang_basic}")
        
        if is_disallowed_lang_basic:
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da {username} in lingua non consentita.")
            motivo_finale_rifiuto = f"lingua non consentita (msg {'editato' if is_edited else 'nuovo'})"
            self.bot_stats['messages_deleted_by_ai_filter'] += 1
            
            if is_edited:
                self.logger.warning(f"Ban per {username} ({user_id}): messaggio editato in lingua non consentita.")
                # RIMOZIONE: Non più Google Sheets
                # self.sheets_manager.ban_user(user_id, username, "Edit in lingua non consentita")
                self.csv_manager.ban_user(user_id, username, "Edit in lingua non consentita")
                self.bot_stats['users_banned_total'] += 1
                motivo_finale_rifiuto += " - Ban applicato (lingua edit)"

            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.save_message(...)
            self.csv_manager.save_message(
                message_text, user_id, username, chat_id, group_name,
                approvato=False, domanda=False, motivo_rifiuto=motivo_finale_rifiuto
            )
            try:
                await context.bot.delete_message(chat_id, message_id)
                self.bot_stats['messages_deleted_total'] += 1
                if is_edited: self.bot_stats['edited_messages_deleted'] += 1
                await self._send_temporary_notification(context, chat_id, "❌ Messaggio eliminato (lingua non consentita).")
            except Exception as e:
                self.logger.error(f"Errore cancellazione messaggio (lingua): {e}")
            return

        # 9. Controllo messaggi brevi/emoji (skip analisi AI costosa)
        is_short_message = self._is_short_or_emoji_message(message_text)
        self.logger.info(f"🔍 DEBUG: Considerato messaggio breve: {is_short_message}")
        
        if is_short_message:
            self.logger.info(f"Messaggio breve/emoji da {username}: controlli sicurezza OK, skip analisi AI costosa.")
            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.save_message(...)
            self.csv_manager.save_message(
                message_text, user_id, username, chat_id, group_name,
                approvato=True, domanda=False, motivo_rifiuto=""
            )
            return

        # 10. Analisi AI completa (OpenAI o fallback)
        is_inappropriate_ai, is_question_ai, is_disallowed_lang_ai = self.moderation_logic.analyze_with_openai(message_text)
        
        action_taken = False
        motivo_finale_rifiuto = ""
        
        if is_inappropriate_ai:
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da {username} rilevato come inappropriato da AI.")
            motivo_finale_rifiuto = f"contenuto inappropriato (AI) (msg {'editato' if is_edited else 'nuovo'})"
            action_taken = True
            self.bot_stats['messages_deleted_by_ai_filter'] += 1
            
            if is_edited:
                self.logger.warning(f"Ban per {username} ({user_id}): messaggio editato inappropriato (AI).")
                # RIMOZIONE: Non più Google Sheets
                # self.sheets_manager.ban_user(user_id, username, "Edit inappropriato (AI)")
                self.csv_manager.ban_user(user_id, username, "Edit inappropriato (AI)")
                self.bot_stats['users_banned_total'] += 1
                motivo_finale_rifiuto += " - Ban applicato (AI edit)"
            elif total_user_messages <= self.config_manager.get('first_messages_threshold', 3):
                self.logger.warning(f"Ban per {username} ({user_id}): primo messaggio ({total_user_messages}/{self.config_manager.get('first_messages_threshold', 3)}) inappropriato (AI).")
                # RIMOZIONE: Non più Google Sheets
                # self.sheets_manager.ban_user(user_id, username, f"Primo messaggio inappropriato AI (msg #{total_user_messages})")
                self.csv_manager.ban_user(user_id, username, f"Primo messaggio inappropriato AI (msg #{total_user_messages})")
                self.bot_stats['users_banned_total'] += 1
                motivo_finale_rifiuto += f" - Ban applicato (AI primo msg #{total_user_messages})"

        elif is_disallowed_lang_ai:
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da {username} rilevato in lingua non consentita da AI/Langdetect.")
            motivo_finale_rifiuto = f"lingua non consentita AI (msg {'editato' if is_edited else 'nuovo'})"
            action_taken = True
            self.bot_stats['messages_deleted_by_ai_filter'] += 1
            
            if is_edited:
                self.logger.warning(f"Ban per {username} ({user_id}): messaggio editato in lingua non consentita (AI).")
                # RIMOZIONE: Non più Google Sheets
                # self.sheets_manager.ban_user(user_id, username, "Edit in lingua non consentita (AI)")
                self.csv_manager.ban_user(user_id, username, "Edit in lingua non consentita (AI)")
                self.bot_stats['users_banned_total'] += 1
                motivo_finale_rifiuto += " - Ban applicato (lingua AI edit)"

        # 11. Azione finale
        if action_taken:
            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.save_message(...)
            self.csv_manager.save_message(
                message_text, user_id, username, chat_id, group_name,
                approvato=False, domanda=is_question_ai, motivo_rifiuto=motivo_finale_rifiuto
            )
            try:
                await context.bot.delete_message(chat_id, message_id)
                self.bot_stats['messages_deleted_total'] += 1
                if is_edited: self.bot_stats['edited_messages_deleted'] += 1
                
                notif_text = "❌ Messaggio eliminato. Attenersi alle linee guida del gruppo.\nScrivimi in chat il comando /rules per conoscere le regole del gruppo!\n"
                if "Ban applicato" in motivo_finale_rifiuto:
                    notif_text += " L'utente è stato sanzionato."

                await self._send_temporary_notification(context, chat_id, notif_text)

            except Exception as e:
                self.logger.error(f"Errore cancellazione messaggio ({motivo_finale_rifiuto}): {e}")
        else:
            # Messaggio approvato
            self.logger.info(f"Messaggio {'modificato ' if is_edited else ''}da {username} approvato. Domanda: {is_question_ai}.")
            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.save_message(...)
            self.csv_manager.save_message(
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
            group_name_for_sheets = f"Chat {cid}"
            try:
                chat_details = await context.bot.get_chat(cid)
                group_name_for_sheets = chat_details.title or group_name_for_sheets
            except Exception: pass

            recent_messages_in_chat = self.message_cache.get_recent_messages(cid, user_id)
            for msg_id, msg_text in recent_messages_in_chat:
                try:
                    # RIMOZIONE: Non più Google Sheets
                    # self.sheets_manager.save_message(...)
                    self.csv_manager.save_message(
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
            asyncio.create_task(self._delete_message_after_delay(context, chat_id, sent_msg.message_id, duration_seconds))
        except Forbidden:
            self.logger.warning(f"Impossibile inviare notifica temporanea a chat {chat_id}: permessi insufficienti.")
        except Exception as e:
            self.logger.error(f"Errore invio notifica temporanea a chat {chat_id}: {e}")

    async def _delete_message_after_delay(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay_seconds: int):
        """Coroutine helper per eliminare un messaggio dopo un ritardo."""
        await asyncio.sleep(delay_seconds)
        try:
            await context.bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    # --- Command Handlers (aggiornati per rimuovere Google Sheets) ---
    async def _generic_admin_command_executor(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                            command_name: str,
                                            action_coroutine_provider: callable,
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
            
            if group_specific and chat_id_for_action is not None:
                action_coro = action_coroutine_provider(context.bot, chat_id_for_action)
            else:
                action_coro = action_coroutine_provider(context.bot)

            await action_coro
            
            await update.message.reply_text(success_message)
            self.logger.info(f"Comando '{command_name}' eseguito con successo da {user.username} ({user.id}).")

        except Exception as e:
            self.logger.error(f"Errore durante l'esecuzione del comando '{command_name}': {e}", exc_info=True)
            await update.message.reply_text(failure_message)
        finally:
            self._release_lock(lock_name)

    async def backup_now_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Attiva manualmente un backup dei dati CSV."""
        async def csv_backup_action(_bot_unused):
            return self.csv_manager.backup_csv_files()

        await self._generic_admin_command_executor(
            update, context, "backup_now",
            action_coroutine_provider=csv_backup_action,
            success_message="✅ Backup CSV completato!",
            failure_message="❌ Errore durante il backup manuale. Controlla i log.",
            group_specific=False
        )

    async def night_mode_on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Attiva manualmente la Night Mode nel gruppo corrente."""
        chat_id = update.effective_chat.id
        if chat_id not in self.get_night_mode_groups():
            await update.message.reply_text("⚠️ Questo gruppo non è configurato per la Night Mode. Aggiungilo in `config.json`.")
            return

        async def action(bot: Bot, cid: int):
            await self._apply_night_mode_permissions(bot, cid, activate=True)
            self.night_mode_transition_active = True
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
        if chat_id not in self.get_night_mode_groups():
            self.logger.info(f"Comando /night_off in gruppo {chat_id} non in lista NM, ma procedo per sicurezza.")

        async def action(bot: Bot, cid: int):
            await self._apply_night_mode_permissions(bot, cid, activate=False)
            self.night_mode_transition_active = False
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
        counter_stats = self.user_counters.get_stats()
        csv_stats = self.csv_manager.get_csv_stats()
        
        stats_msg = "📊 **Statistiche Moderazione Bot** 📊\n\n"
        stats_msg += f"🔹 **Messaggi Processati:** {self.bot_stats['total_messages_processed']}\n"
        stats_msg += f"🔹 **Messaggi Eliminati Totali:** {self.bot_stats['messages_deleted_total']}\n"
        stats_msg += f"  ▫️ Da Filtro Diretto: {self.bot_stats['messages_deleted_by_direct_filter']}\n"
        stats_msg += f"  ▫️ Da Analisi AI: {self.bot_stats['messages_deleted_by_ai_filter']}\n"
        stats_msg += f"🔹 **Messaggi Modificati Rilevati:** {self.bot_stats['edited_messages_detected']}\n"
        stats_msg += f"  ▫️ Modificati ed Eliminati: {self.bot_stats['edited_messages_deleted']}\n"
        stats_msg += f"🔹 **Utenti Bannati:** {self.bot_stats['users_banned_total']}\n"
        stats_msg += f"🔹 **Utenti Unbannati:** {self.bot_stats['users_unbanned_total']}\n\n"
        
        stats_msg += "🔍 **Statistiche Analisi Contenuto (OpenAI & Cache):**\n"
        stats_msg += f"🔸 Richieste OpenAI Effettuate: {mod_stats['openai_requests']}\n"
        stats_msg += f"🔸 Risultati da Cache OpenAI: {mod_stats['openai_cache_hits']}\n"
        stats_msg += f"🔸 Tasso Hit Cache: {mod_stats['cache_hit_rate']:.2%}\n"
        stats_msg += f"🔸 Dimensione Cache Analisi: {mod_stats['cache_size']}\n"
        stats_msg += f"🔸 Violazioni rilevate da AI: {mod_stats['ai_filter_violations']}\n"
        stats_msg += f"🔸 Match filtro diretto (logica): {mod_stats['direct_filter_matches']}\n\n"
        stats_msg += f"👥 **Contatori Utente:**\n"
        stats_msg += f"🔸 Utenti tracciati: {counter_stats['total_tracked_users']}\n"
        stats_msg += f"🔸 Nuovi utenti (≤3 msg): {counter_stats['first_time_users']}\n"
        stats_msg += f"🔸 Utenti veterani (>3 msg): {counter_stats['veteran_users']}\n\n"
        stats_msg += f"💾 **Sistema CSV:**\n"
        if csv_stats.get('csv_disabled'):
            stats_msg += f"🔸 CSV: DISABILITATO\n"
        else:
            stats_msg += f"🔸 Messaggi in CSV: {csv_stats.get('messages', 0)}\n"
            stats_msg += f"🔸 Admin in CSV: {csv_stats.get('admin', 0)}\n"
            stats_msg += f"🔸 Bannati in CSV: {csv_stats.get('banned_users', 0)}\n"
        stats_msg += "\n"

        nm_groups_now = [gid for gid in self.get_night_mode_groups() if self.is_night_mode_period_active(gid)]
        stats_msg += f"🌙 Gruppi attualmente in Night Mode (da orario): {len(nm_groups_now)}\n"

        await update.message.reply_text(stats_msg, parse_mode='Markdown')

    async def show_rules_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra le linee guida del gruppo (comando disponibile SOLO in chat privata)."""
        if not self.config_manager.get('rules_command_enabled', True):
            return

        if update.effective_chat.type != 'private':
            self.logger.info(f"Comando /rules ignorato in chat di gruppo {update.effective_chat.id} da utente {update.effective_user.id}")
            return
        
        rules_text = self.config_manager.get('rules_message', 
            "📋 **LINEE GUIDA DEL GRUPPO**\n\n")
        
        try:
            await context.bot.send_message(
                update.effective_chat.id, 
                rules_text, 
                parse_mode='Markdown'
            )
            
            self.logger.info(f"Regole inviate in privato a utente {update.effective_user.id} ({update.effective_user.username})")
            
        except Exception as e:
            self.logger.error(f"Errore invio regole in privato a utente {update.effective_user.id}: {e}")
            try:
                await context.bot.send_message(
                    update.effective_chat.id,
                    "❌ Errore nell'invio delle regole. Riprova più tardi."
                )
            except Exception:
                pass

    async def reset_ai_cache_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reset della cache analisi AI (solo per admin)."""
        user = update.effective_user
        if not user: return
        exempt_users_list = self.config_manager.get('exempt_users', [])
        if not (user.id in exempt_users_list or (user.username and user.username in exempt_users_list)):
            await update.message.reply_text("❌ Non sei autorizzato a eseguire questo comando.")
            return

        cache_size_before = len(self.moderation_logic.analysis_cache.cache)
        self.moderation_logic.analysis_cache.cache.clear()
        self.moderation_logic.analysis_cache.access_count.clear()
        
        await update.message.reply_text(f"🗑️ Cache AI resettata! Rimossi {cache_size_before} elementi dalla cache.")
        self.logger.info(f"Cache AI resettata da admin {user.username} ({user.id})")

    async def manual_ban_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /ban <user_id> per bannare manualmente un utente da tutti i gruppi."""
        user = update.effective_user
        if not user: 
            return
        
        exempt_users_list = self.config_manager.get('exempt_users', [])
        if not (user.id in exempt_users_list or (user.username and user.username in exempt_users_list)):
            await update.message.reply_text("❌ Non sei autorizzato a eseguire questo comando.")
            return
        
        try:
            args = context.args
            if len(args) < 1:
                await update.message.reply_text("❌ **Uso:** `/ban <user_id> [motivo]`\n\n**Esempio:** `/ban 123456789 spam`", parse_mode='Markdown')
                return
            
            try:
                target_user_id = int(args[0])
            except ValueError:
                await update.message.reply_text("❌ L'user_id deve essere un numero valido.")
                return
            
            motivo = " ".join(args[1:]) if len(args) > 1 else "Ban manuale da admin"
            
            if target_user_id == user.id:
                await update.message.reply_text("❌ Non puoi bannare te stesso!")
                return
            
            if target_user_id in exempt_users_list:
                await update.message.reply_text("❌ Non puoi bannare un altro amministratore!")
                return
            
            status_msg = await update.message.reply_text(f"🔨 **Ban in corso per utente:** `{target_user_id}`\n📝 **Motivo:** {motivo}\n\n⏳ Rimozione da tutti i gruppi configurati...", parse_mode='Markdown')
            
            target_groups = self.get_night_mode_groups()
            
            if not target_groups:
                await status_msg.edit_text("❌ Nessun gruppo configurato per il ban. Verifica la configurazione `night_mode_groups`.")
                return
            
            results = await self._execute_multi_group_ban(context.bot, target_user_id, target_groups, motivo)
            
            # RIMOZIONE: Non più Google Sheets
            # self.sheets_manager.ban_user(target_user_id, f"UserID_{target_user_id}", f"Ban manuale: {motivo}")
            self.csv_manager.ban_user(target_user_id, f"UserID_{target_user_id}", f"Ban manuale: {motivo}")
            self.bot_stats['users_banned_total'] += 1
            
            success_count = sum(1 for success in results.values() if success)
            total_groups = len(target_groups)
            
            result_text = f"🔨 **BAN COMPLETATO**\n\n"
            result_text += f"👤 **Utente:** `{target_user_id}`\n"
            result_text += f"📝 **Motivo:** {motivo}\n"
            result_text += f"📊 **Risultato:** {success_count}/{total_groups} gruppi\n\n"
            
            result_text += "📋 **Dettagli per gruppo:**\n"
            for chat_id, success in results.items():
                status_emoji = "✅" if success else "❌"
                result_text += f"{status_emoji} Gruppo `{chat_id}`\n"
            
            if success_count == total_groups:
                result_text += f"\n🎯 **Ban completato con successo!**"
            elif success_count > 0:
                result_text += f"\n⚠️ **Ban parzialmente riuscito.** Alcuni gruppi potrebbero richiedere permessi aggiuntivi."
            else:
                result_text += f"\n❌ **Ban fallito.** Verifica i permessi del bot nei gruppi."
            
            await status_msg.edit_text(result_text, parse_mode='Markdown')
            
            self.logger.info(f"🔨 BAN MANUALE eseguito da {user.username} ({user.id}): utente {target_user_id} - successo {success_count}/{total_groups} gruppi")
            
        except Exception as e:
            self.logger.error(f"Errore comando ban manuale: {e}", exc_info=True)
            try:
                await update.message.reply_text(f"❌ **Errore durante il ban:**\n`{str(e)}`", parse_mode='Markdown')
            except:
                pass

    async def _execute_multi_group_ban(self, bot: Bot, user_id: int, target_groups: List[int], motivo: str) -> Dict[int, bool]:
        """Esegue il ban di un utente da multiple chat."""
        results = {}
        
        self.logger.info(f"🔨 INIZIO BAN MULTI-GRUPPO per utente {user_id} da {len(target_groups)} gruppi. Motivo: {motivo}")
        
        for chat_id in target_groups:
            try:
                group_name = f"Chat {chat_id}"
                try:
                    chat_info = await bot.get_chat(chat_id)
                    group_name = chat_info.title or group_name
                except:
                    pass
                
                await bot.ban_chat_member(chat_id, user_id)
                results[chat_id] = True
                self.logger.info(f"✅ Utente {user_id} bannato da {group_name} ({chat_id})")
                
            except Forbidden:
                results[chat_id] = False
                self.logger.error(f"❌ PERMESSI INSUFFICIENTI per bannare utente {user_id} dal gruppo {chat_id}. Il bot deve essere admin con permessi di ban.")
                
            except BadRequest as e:
                error_msg = str(e).lower()
                if "user not found" in error_msg or "user_not_participant" in error_msg:
                    results[chat_id] = True
                    self.logger.info(f"ℹ️ Utente {user_id} non presente nel gruppo {chat_id} - considerato successo")
                elif "user_admin" in error_msg or "can't remove chat owner" in error_msg:
                    results[chat_id] = False
                    self.logger.warning(f"⚠️ Impossibile bannare utente {user_id} dal gruppo {chat_id}: è admin del gruppo")
                else:
                    results[chat_id] = False
                    self.logger.error(f"❌ ERRORE TELEGRAM BAN per utente {user_id} nel gruppo {chat_id}: {e}")
                    
            except Exception as e:
                results[chat_id] = False
                self.logger.error(f"❌ ERRORE GENERICO durante ban di utente {user_id} nel gruppo {chat_id}: {e}")
        
        success_count = sum(results.values())
        self.logger.info(f"🔨 BAN MULTI-GRUPPO COMPLETATO: {success_count}/{len(target_groups)} gruppi per utente {user_id}")
        
        return results

    async def manual_unban_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /unban <user_id> per rimuovere il ban da Telegram."""
        user = update.effective_user
        if not user: 
            return
        
        exempt_users_list = self.config_manager.get('exempt_users', [])
        if not (user.id in exempt_users_list or (user.username and user.username in exempt_users_list)):
            await update.message.reply_text("❌ Non sei autorizzato a eseguire questo comando.")
            return
        
        try:
            args = context.args
            if len(args) < 1:
                await update.message.reply_text("❌ **Uso:** `/unban <user_id>`\n\n**Esempio:** `/unban 123456789`", parse_mode='Markdown')
                return
            
            try:
                target_user_id = int(args[0])
            except ValueError:
                await update.message.reply_text("❌ L'user_id deve essere un numero valido.")
                return
            
            status_msg = await update.message.reply_text(f"🔓 **Unban in corso per utente:** `{target_user_id}`\n\n⏳ Rimozione ban da tutti i gruppi...", parse_mode='Markdown')
            
            target_groups = self.get_night_mode_groups()
            
            if not target_groups:
                await status_msg.edit_text("❌ Nessun gruppo configurato per l'unban.")
                return
            
            results = {}
            for chat_id in target_groups:
                try:
                    await context.bot.unban_chat_member(chat_id, target_user_id, only_if_banned=True)
                    results[chat_id] = True
                except Exception as e:
                    results[chat_id] = False
                    self.logger.warning(f"Errore unban utente {target_user_id} da gruppo {chat_id}: {e}")
            
            success_count = sum(results.values())
            self.bot_stats['users_unbanned_total'] += 1
            
            result_text = f"🔓 **UNBAN TELEGRAM COMPLETATO**\n\n"
            result_text += f"👤 **Utente:** `{target_user_id}`\n"
            result_text += f"📊 **Risultato:** {success_count}/{len(target_groups)} gruppi\n\n"
            result_text += f"⚠️ **Nota:** Il ban logico rimane attivo nel database."
            
            await status_msg.edit_text(result_text, parse_mode='Markdown')
            
            self.logger.info(f"🔓 UNBAN MANUALE eseguito da {user.username} ({user.id}): utente {target_user_id}")
            
        except Exception as e:
            self.logger.error(f"Errore comando unban: {e}")
            await update.message.reply_text(f"❌ **Errore durante l'unban:**\n`{str(e)}`", parse_mode='Markdown')

    # --- Bot Lifecycle & Scheduler ---
    def _run_scheduler_thread(self):
        """Esegue i task programmati in un thread separato."""
        self.logger.info("Thread dello scheduler avviato.")
        while self.scheduler_active:
            try:
                schedule.run_pending()
            except Exception as e:
                self.logger.error(f"Errore imprevisto nello scheduler: {e}", exc_info=True)
            time.sleep(self.config_manager.get('scheduler_check_interval_seconds', 60))
        self.logger.info("Thread dello scheduler terminato.")

    def _check_night_mode_on_startup(self):
        """Verifica e applica lo stato della Night Mode all'avvio del bot."""
        if not self._acquire_lock("startup_night_mode_check"):
            self.logger.info("Controllo Night Mode all'avvio già in corso o completato da altra istanza.")
            return
        try:
            self.logger.info("Controllo stato Night Mode all'avvio...")
            should_be_active = self.is_night_mode_period_active(-1)
            
            self._safe_run_coroutine(
                lambda bot: self._task_manage_night_mode_for_all_groups(bot, activate=should_be_active),
                description="controllo Night Mode all'avvio"
            )
        finally:
            self._release_lock("startup_night_mode_check")

    def _is_short_or_emoji_message(self, text: str) -> bool:
        """Verifica se il messaggio è davvero innocuo e breve."""
        import re
        
        clean_text = text.strip()
        short_max_length = self.config_manager.get('short_message_max_length', 4)
        
        english_words = {
            'hi', 'hello', 'hey', 'how', 'are', 'you', 'what', 'where', 'when', 
            'why', 'who', 'can', 'could', 'would', 'should', 'will', 'thanks', 
            'thank', 'please', 'sorry', 'yes', 'no', 'okay', 'ok', 'bye', 'goodbye',
            'good', 'bad', 'nice', 'great', 'welcome', 'see', 'the', 'and', 'but'
        }
        
        if clean_text.lower() in english_words:
            return False
        
        if len(clean_text) > short_max_length and len(clean_text) < 10:
            safe_pattern = r'^[a-zA-Z0-9\s.,!?;:()\-àèéìíîòóùú]*$'
            if re.match(safe_pattern, clean_text):
                return True
            else:
                return False
        
        if len(clean_text) <= 15:
            safe_short_patterns = [
                r'^(si|no|ok|ciao|grazie|prego|bene|male|buono|ottimo|perfetto)$',
                r'^[0-9\s\-+/().,]+$',
                r'^[.,!?;:\s]+$',
                r'^[👍👎❤️😊😢🎉✨🔥💪😍😂🤔😅]+$',
            ]
            
            for pattern in safe_short_patterns:
                if re.match(pattern, clean_text.lower()):
                    return True
        
        return False

    # SOSTITUISCI il metodo start() in bot_core.py con questo:

    def start(self):
        """Avvia il bot con gestione silenziosa degli errori di polling."""
        self.logger.info(f"Avvio del Bot di Moderazione Telegram (PID: {os.getpid()})...")
        self.logger.info(f"Directory di lavoro corrente: {os.getcwd()}")

        self._is_running = True
        self._start_time = datetime.now()

        self.application = Application.builder().token(self.token).build()

        # Handlers per messaggi
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, 
            self.filter_new_messages
        ))
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
        self.application.add_handler(CommandHandler("rules", self.show_rules_command))
        self.application.add_handler(CommandHandler("resetcache", self.reset_ai_cache_command))
        self.application.add_handler(CommandHandler("ban", self.manual_ban_command))
        self.application.add_handler(CommandHandler("unban", self.manual_unban_command))

        # Pianifica Night Mode
        self._schedule_night_mode_jobs()

        # Avvia thread per scheduler
        import threading
        self.scheduler_thread = threading.Thread(target=self._run_scheduler_thread, daemon=True)
        self.scheduler_thread.start()

        # Handler per errori di polling
        async def error_handler(update, context):
            """Gestisce silenziosamente gli errori di rete del polling."""
            error = context.error
            
            if isinstance(error, (NetworkError, TimedOut)):
                if not hasattr(self, '_network_error_count'):
                    self._network_error_count = 0
                self._network_error_count += 1
                
                if self._network_error_count % 10 == 1:
                    self.logger.debug(f"🌐 Errori di rete nel polling: #{self._network_error_count} (dettagli soppressi)")
                return
            
            self.logger.error(f"Errore non di rete: {error}", exc_info=True)

        self.application.add_error_handler(error_handler)

        self.logger.info("Bot configurato e pronto. Avvio polling...")
        try:
            # IMPORTANTE: Controlla se siamo nel thread principale
            import threading
            is_main_thread = threading.current_thread() is threading.main_thread()
            
            if is_main_thread:
                self.logger.info("Avvio nel thread principale - signal handlers abilitati")
                # Thread principale: usa configurazione normale
                self.application.run_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=self.config_manager.get('drop_pending_updates_on_start', True)
                )
            else:
                self.logger.info("Avvio in thread secondario - signal handlers disabilitati")
                # Thread secondario: usa approccio diverso per permettere stop controllato
                
                async def start_bot_async():
                    """Avvia il bot in modo asincrono senza signal handlers."""
                    try:
                        await self.application.initialize()
                        await self.application.start()
                        await self.application.updater.start_polling(
                            allowed_updates=Update.ALL_TYPES,
                            drop_pending_updates=self.config_manager.get('drop_pending_updates_on_start', True)
                        )
                        
                        self.logger.info("Bot polling avviato con successo in thread secondario")
                        
                        # NUOVO: Loop controllabile per permettere stop pulito
                        while self._is_running:
                            await asyncio.sleep(0.5)  # Check ogni 500ms se dobbiamo fermarci
                            
                            # Verifica se l'updater è ancora in running
                            if not self.application.updater.running:
                                self.logger.warning("Updater si è fermato inaspettatamente")
                                break
                                
                    except Exception as e:
                        self.logger.error(f"Errore durante polling asincrono: {e}", exc_info=True)
                    finally:
                        # Cleanup più robusto
                        self.logger.info("Inizio cleanup bot asincrono...")
                        try:
                            if hasattr(self.application, 'updater') and self.application.updater:
                                if self.application.updater.running:
                                    self.logger.info("Fermando updater...")
                                    await self.application.updater.stop()
                            
                            if hasattr(self.application, 'stop'):
                                self.logger.info("Fermando application...")
                                await self.application.stop()
                            
                            if hasattr(self.application, 'shutdown'):
                                self.logger.info("Shutdown application...")
                                await self.application.shutdown()
                                
                        except Exception as e:
                            self.logger.warning(f"Errore durante cleanup asincrono: {e}")
                        
                        self.logger.info("Cleanup asincrono completato")
                
                # Esegui nel loop asyncio del thread corrente con gestione stop
                try:
                    asyncio.run(start_bot_async())
                except KeyboardInterrupt:
                    self.logger.info("Bot fermato da KeyboardInterrupt in thread secondario")
                except Exception as e:
                    self.logger.error(f"Errore nel loop asyncio del thread secondario: {e}", exc_info=True)
                
        except KeyboardInterrupt:
            self.logger.info("Polling interrotto da tastiera (Ctrl+C).")
        except RuntimeError as e:
            if "set_wakeup_fd" in str(e) or "signal handler" in str(e):
                self.logger.error("Errore signal handler - il bot deve essere eseguito nel thread principale per gestire i segnali")
            elif "coroutine" in str(e):
                self.logger.warning(f"Errore asincrono gestito: {e}")
            else:
                self.logger.error(f"Errore runtime durante il polling: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Errore durante il polling: {e}", exc_info=True)
        finally:
            self.stop()

    def stop(self):
        """Ferma lo scheduler e altre operazioni in preparazione alla chiusura."""
        self.logger.info("Arresto del bot in corso...")
        
        # Imposta flag di stop PRIMA di tutto
        self._is_running = False
        self.scheduler_active = False
        
        # Ferma l'applicazione se esiste
        if hasattr(self, 'application') and self.application:
            try:
                import threading
                is_main_thread = threading.current_thread() is threading.main_thread()
                
                if is_main_thread:
                    # Thread principale: usa metodi sincroni
                    self.logger.info("Stop dal thread principale")
                    try:
                        if hasattr(self.application, 'stop') and callable(self.application.stop):
                            self.application.stop()
                    except Exception as e:
                        self.logger.warning(f"Errore stop application (main thread): {e}")
                else:
                    # Thread secondario: il cleanup sarà gestito dal finally di start_bot_async()
                    self.logger.info("Stop da thread secondario - delegando al cleanup asincrono")
                    # Non facciamo nulla qui, il flag _is_running = False farà uscire dal loop
                    # e il cleanup sarà gestito dal finally di start_bot_async()
                
            except Exception as e:
                self.logger.warning(f"Errore durante stop dell'applicazione: {e}")
        
        # Salva contatori
        if hasattr(self, 'user_counters'):
            try:
                self.user_counters.force_save()
                self.logger.info("Contatori utente salvati.")
            except Exception as e:
                self.logger.warning(f"Errore salvataggio contatori: {e}")

        self.logger.info("Bot arrestato.")


    def force_stop(self):
        """Forza l'arresto del bot anche se non risponde normalmente."""
        self.logger.warning("Force stop del bot richiesto")
        
        self._is_running = False
        self.scheduler_active = False
        
        # Prova a fermare tutto brutalmente
        if hasattr(self, 'application') and self.application:
            try:
                # Cancella tutti i task asyncio se possibile
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    if loop:
                        for task in asyncio.all_tasks(loop):
                            if not task.done():
                                task.cancel()
                except RuntimeError:
                    pass  # Nessun loop in esecuzione
                
                # Forza chiusura applicazione
                if hasattr(self.application, 'updater') and self.application.updater:
                    try:
                        if hasattr(self.application.updater, '_request'):
                            self.application.updater._request = None
                    except:
                        pass
                        
            except Exception as e:
                self.logger.error(f"Errore durante force stop: {e}")
        
        # Salva contatori se possibile
        try:
            if hasattr(self, 'user_counters'):
                self.user_counters.force_save()
        except:
            pass
            
        self.logger.warning("Force stop completato")