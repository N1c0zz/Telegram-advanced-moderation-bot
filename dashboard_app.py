import os
import sys
import asyncio
import threading
import signal
import json
from datetime import datetime
from typing import Dict, Any, Optional

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file
from flask import session as flask_session
import logging

# Aggiungi il percorso src per importazioni
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.auth import AuthManager, auth_required, is_authenticated, get_current_username
from src.auth_routes import setup_auth_routes
from flask_login import current_user

from src.bot_core import TelegramModerationBot
from src.config_manager import ConfigManager
from src.user_management import UserManagementSystem, SystemPromptManager, ConfigurationManager

class DashboardApp:
    """
    Applicazione Flask per la dashboard di gestione del bot.
    """
    
    def __init__(self):
        """Inizializzazione con gestione sicura degli import."""
        self.app = Flask(__name__)
        self.app.secret_key = os.getenv("DASHBOARD_SECRET_KEY", "your-secret-key-change-this")

        # Inizializza autenticazione
        self.auth_manager = AuthManager()
        self.auth_manager.init_app(self.app)
        setup_auth_routes(self.app, self.auth_manager)
        
        # Configurazione Flask
        self.app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file upload
        
        # Setup logging per dashboard PRIMA di tutto
        self.setup_dashboard_logging()
        
        # IMPORTANTE: Inizializza il logger SUBITO dopo setup_dashboard_logging
        self.logger = logging.getLogger("Dashboard")
        
        # Setup funzioni template PRIMA di setup_routes
        self.setup_template_functions()
        
        # Bot instance (inizialmente None)
        self.bot: Optional[TelegramModerationBot] = None
        self.bot_thread: Optional[threading.Thread] = None
        
        # Managers - Inizializza con valori sicuri
        self.config_manager = ConfigManager()
        self.user_manager: Optional[UserManagementSystem] = None
        self.prompt_manager: Optional[SystemPromptManager] = None
        self.config_editor: Optional[ConfigurationManager] = None
        
        # Prova a inizializzare i manager basic senza bot per la dashboard
        try:
            # Import sicuri dei manager
            try:
                from src.user_management import ConfigurationManager, SystemPromptManager
            except ImportError:
                # Fallback per import senza package
                from user_management import ConfigurationManager, SystemPromptManager
            
            # Ora self.logger è disponibile e gli import sono riusciti
            self.config_editor = ConfigurationManager(self.config_manager, self.logger)
            
            # Per il prompt manager, se non c'è bot, crea un manager basic
            self.prompt_manager = SystemPromptManager(self.logger, None)
            
            self.logger.info("Manager inizializzati con successo senza bot")
            
        except ImportError as e:
            self.logger.error(f"Impossibile importare manager: {e}")
            self.logger.warning("Dashboard funzionerà con funzionalità limitate")
            # I manager rimarranno None e verranno gestiti nelle route
        except Exception as e:
            self.logger.warning(f"Impossibile inizializzare manager senza bot: {e}")
            # I manager rimarranno None e verranno gestiti nelle route
        
        # Stats iniziali
        self.bot_stats: Dict[str, int] = {
            'total_messages_processed': 0,
            'messages_deleted_total': 0,
            'messages_deleted_by_direct_filter': 0,
            'messages_deleted_by_ai_filter': 0,
            'edited_messages_detected': 0,
            'edited_messages_deleted': 0,
            'users_banned_total': 0,
            'users_unbanned_total': 0,
        }
        self.application: Optional[Application] = None
        self._operation_locks: Dict[str, bool] = {}

        # Stato di running per dashboard
        self._is_running: bool = False
        self._start_time: Optional[datetime] = None
        
        # Setup routes
        self.setup_routes()
        
        self.logger.info("Dashboard inizializzata con gestione errori migliorata")
    
    def setup_dashboard_logging(self):
        """Configura logging separato per la dashboard."""
        dashboard_logger = logging.getLogger("Dashboard")
        dashboard_logger.setLevel(logging.INFO)
        
        if not dashboard_logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - DASHBOARD - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            dashboard_logger.addHandler(handler)

    def setup_template_functions(self):
        """Configura funzioni personalizzate per i template Jinja2 (versione sicura)."""
        
        @self.app.template_filter('format_number')
        def format_number_filter(value):
            """Formatta un numero con separatori delle migliaia."""
            try:
                if value is None:
                    return '0'
                return f"{int(value):,}".replace(',', '.')
            except (ValueError, TypeError):
                return str(value) if value is not None else '0'
        
        @self.app.template_global()
        def formatNumber(value):
            """Funzione globale per formattare numeri nei template."""
            try:
                if value is None:
                    return '0'
                return f"{int(value):,}".replace(',', '.')
            except (ValueError, TypeError):
                return str(value) if value is not None else '0'

        @self.app.template_global()
        def is_user_banned_helper(user_id):
            """Funzione helper per verificare se un utente è bannato (per use nei template)."""
            try:
                if self.bot and self.bot.csv_manager:
                    return self.bot.csv_manager.is_user_banned(int(user_id))
                return False
            except Exception as e:
                self.logger.error(f"Errore verifica ban per user {user_id}: {e}")
                return False
        
        @self.app.template_global()
        def formatPercentage(value, decimals=1):
            """Formatta una percentuale."""
            try:
                if value is None:
                    return '0%'
                return f"{float(value):.{decimals}f}%"
            except (ValueError, TypeError):
                return str(value) if value is not None else '0%'
        
        @self.app.template_global()
        def formatDateTime(timestamp_str, format='%d/%m/%Y %H:%M'):
            """Formatta una data/ora."""
            try:
                if not timestamp_str:
                    return 'N/A'
                if isinstance(timestamp_str, str):
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    return dt.strftime(format)
                return str(timestamp_str)
            except:
                return str(timestamp_str) if timestamp_str else 'N/A'
        
        @self.app.template_global()
        def timeAgo(timestamp_str):
            """Calcola il tempo trascorso."""
            try:
                if not timestamp_str:
                    return 'N/A'
                if isinstance(timestamp_str, str):
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    dt = dt.replace(tzinfo=None)  # Remove timezone
                    diff = datetime.now() - dt
                    
                    if diff.days > 0:
                        return f"{diff.days} giorni fa"
                    elif diff.seconds > 3600:
                        hours = diff.seconds // 3600
                        return f"{hours} ore fa"
                    elif diff.seconds > 60:
                        minutes = diff.seconds // 60
                        return f"{minutes} minuti fa"
                    else:
                        return "Ora"
                return str(timestamp_str)
            except:
                return str(timestamp_str) if timestamp_str else 'N/A'

        @self.app.template_global()
        def formatDate(timestamp_str, format='%d/%m/%Y %H:%M'):
            """Alias per formatDateTime per compatibilità."""
            return formatDateTime(timestamp_str, format)

        @self.app.template_global()
        def formatUptime(seconds):
            """Formatta l'uptime in modo user-friendly."""
            try:
                seconds = int(seconds) if seconds else 0
                days = seconds // 86400
                hours = (seconds % 86400) // 3600
                mins = (seconds % 3600) // 60
                secs = seconds % 60
                
                if days > 0:
                    return f"{days}g {hours}h {mins}m"
                elif hours > 0:
                    return f"{hours}h {mins}m {secs}s"
                elif mins > 0:
                    return f"{mins}m {secs}s"
                else:
                    return f"{secs}s"
            except:
                return "0s"

        @self.app.template_global()
        def get_auth_info():
            """Fornisce informazioni di autenticazione ai template."""
            return {
                'is_authenticated': is_authenticated(),
                'current_username': get_current_username(),
                'auth_user': current_user if current_user.is_authenticated else None
            }
    
    def setup_routes(self):
        """Configura tutte le route della dashboard."""
        
        # --- Route principali ---
        @self.app.route('/')
        @auth_required
        def index():
            """Homepage della dashboard."""
            bot_status = self.get_bot_status()
            recent_stats = self.get_recent_activity_stats()
            
            return render_template('index.html', 
                                 bot_status=bot_status,
                                 recent_stats=recent_stats)
        
        # --- Controllo Bot ---
        @self.app.route('/bot/start', methods=['POST'])
        @auth_required
        def start_bot():
            """Avvia il bot."""
            if self.bot and hasattr(self.bot, '_is_running') and self.bot._is_running:
                flash('Il bot è già in esecuzione!', 'warning')
                return redirect(url_for('index'))
            
            try:
                self.start_bot_async()
                flash('Bot avviato con successo!', 'success')
            except Exception as e:
                self.logger.error(f"Errore avvio bot: {e}")
                flash(f'Errore avvio bot: {str(e)}', 'danger')
            
            return redirect(url_for('index'))
        
        @self.app.route('/api/bot/force-stop', methods=['POST'])
        @auth_required
        def api_force_stop_bot():
            """API per force stop del bot."""
            try:
                if self.bot and hasattr(self.bot, 'force_stop'):
                    self.bot.force_stop()
                    
                    # Aspetta un momento e reset
                    import time
                    time.sleep(2)
                    
                    self.bot = None
                    self.user_manager = None
                    self.prompt_manager = None
                    self.config_editor = None
                    
                    return jsonify({
                        'success': True,
                        'message': 'Force stop eseguito'
                    })
                else:
                    return jsonify({
                        'success': False,
                        'message': 'Bot non attivo o force stop non disponibile'
                    })
            except Exception as e:
                return jsonify({
                    'success': False,
                    'message': f'Errore force stop: {str(e)}'
                })
        
        @self.app.route('/bot/stop', methods=['POST'])
        @auth_required
        def stop_bot():
            """Ferma il bot."""
            try:
                self.stop_bot_async()
                flash('Bot fermato con successo!', 'success')
            except Exception as e:
                self.logger.error(f"Errore stop bot: {e}")
                
                # Fallback: prova force stop se disponibile
                try:
                    if self.bot and hasattr(self.bot, 'force_stop'):
                        self.logger.info("Tentando force stop come fallback...")
                        self.bot.force_stop()
                        self.bot = None
                        flash('Bot fermato con force stop!', 'warning')
                    else:
                        flash(f'Errore stop bot: {str(e)}', 'danger')
                except Exception as e2:
                    flash(f'Errore stop bot: {str(e)} - Force stop fallito: {str(e2)}', 'danger')
            
            return redirect(url_for('index'))
        
        @self.app.route('/api/bot/status')
        @auth_required
        def api_bot_status():
            """API per stato del bot."""
            return jsonify(self.get_bot_status())
        
        # --- Gestione Messaggi ---
        @self.app.route('/messages')
        @auth_required
        def messages():
            """Pagina messaggi recenti."""
            limit = request.args.get('limit', 30, type=int)
            
            if not self.user_manager:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            # FIX: Riusa la stessa logica dell'API recent-activity che già funziona
            recent_messages = []
            
            if self.bot and self.bot.csv_manager:
                try:
                    # Stessa logica di /api/recent-activity ma senza formattazione
                    all_messages = self.bot.csv_manager.read_csv_data("messages")
                    
                    if all_messages:
                        # Ordina per timestamp (più recente prima) - STESSA LOGICA DELLA HOMEPAGE
                        def parse_timestamp(msg):
                            timestamp = msg.get('timestamp', '')
                            try:
                                if timestamp:
                                    if 'T' in timestamp:  # ISO format
                                        return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                                    else:
                                        return datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                                return datetime.min
                            except:
                                return datetime.min
                        
                        # Ordina dal più recente al più vecchio
                        sorted_messages = sorted(all_messages, key=parse_timestamp, reverse=True)
                        
                        # Prendi solo i primi N (i più recenti)
                        recent_messages = sorted_messages[:limit]
                        
                except Exception as e:
                    self.logger.error(f"Errore recupero messaggi per /messages: {e}")
                    recent_messages = []
            
            # FIX: Aggiungi bot_status per la sidebar
            bot_status = self.get_bot_status()
            
            return render_template('messages.html', 
                                messages=recent_messages, 
                                limit=limit,
                                bot_status=bot_status)  # <-- AGGIUNTO
        
        @self.app.route('/messages/deleted')
        @auth_required
        def deleted_messages():
            """Pagina messaggi eliminati."""
            limit = request.args.get('limit', 20, type=int)
            
            if not self.user_manager:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            # FIX: IDENTICA logica ai messaggi processati + filtro eliminati
            deleted_msgs = []
            
            if self.bot and self.bot.csv_manager:
                try:
                    # STEP 1: Leggi TUTTI i messaggi (IDENTICO)
                    all_messages = self.bot.csv_manager.read_csv_data("messages")
                    
                    if all_messages:
                        # STEP 2: Filtra SOLO messaggi eliminati
                        deleted_messages_list = [
                            msg for msg in all_messages 
                            if str(msg.get('approvato', '')).strip().upper() == 'NO'
                        ]
                        
                        # STEP 3: Ordina per timestamp - IDENTICA logica ai messaggi processati
                        def parse_timestamp(msg):
                            timestamp = msg.get('timestamp', '')
                            try:
                                if timestamp:
                                    if 'T' in timestamp:  # ISO format
                                        return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                                    else:
                                        return datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                                return datetime.min
                            except:
                                return datetime.min
                        
                        # STEP 4: Ordina dal più recente al più vecchio (IDENTICO)
                        sorted_deleted = sorted(deleted_messages_list, key=parse_timestamp, reverse=True)
                        
                        # STEP 5: Prendi solo i primi N (IDENTICO)
                        deleted_msgs = sorted_deleted[:limit]
                        
                        self.logger.info(f"Messaggi totali: {len(all_messages)}, eliminati: {len(deleted_messages_list)}, mostrati: {len(deleted_msgs)}")
                        
                except Exception as e:
                    self.logger.error(f"Errore recupero messaggi eliminati: {e}", exc_info=True)
                    deleted_msgs = []
            
            # Aggiungi bot_status per la sidebar (IDENTICO)
            bot_status = self.get_bot_status()
            
            return render_template('deleted_messages.html', 
                                messages=deleted_msgs, 
                                limit=limit,
                                bot_status=bot_status)
        
        # --- Gestione Utenti ---
        @self.app.route('/users/banned')
        @auth_required
        def banned_users():
            """Pagina utenti bannati."""
            limit = request.args.get('limit', 50, type=int)  # Aumentato il default a 50
            
            if not self.user_manager:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            # Ottieni utenti bannati con ordinamento corretto (più recenti prima)
            banned_users_list = self.user_manager.get_banned_users_detailed(limit)
            
            # Aggiungi bot_status per la sidebar
            bot_status = self.get_bot_status()
            
            # Log per debug
            self.logger.info(f"Pagina banned_users: mostrati {len(banned_users_list)} utenti (limit: {limit})")
            
            return render_template('banned_users.html', 
                                 banned_users=banned_users_list,
                                 limit=limit,
                                 bot_status=bot_status)
        
        @self.app.route('/api/user/ban', methods=['POST'])
        @auth_required
        def api_ban_user():
            """API per bannare un utente."""
            data = request.get_json()
            user_id = data.get('user_id')
            reason = data.get('reason', 'Ban da dashboard')
            
            if not user_id:
                return jsonify({'success': False, 'message': 'user_id richiesto'}), 400
            
            if self.bot:
                # Esegui ban asincrono
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(
                        self.bot.ban_user_from_dashboard(int(user_id), reason)
                    )
                    return jsonify(result)
                finally:
                    loop.close()
            else:
                return jsonify({'success': False, 'message': 'Bot non attivo'}), 503
        
        @self.app.route('/api/user/unban', methods=['POST'])
        @auth_required
        def api_unban_user():
            """API per sbannare un utente."""
            data = request.get_json()
            user_id = data.get('user_id')
            
            if not user_id:
                return jsonify({'success': False, 'message': 'user_id richiesto'}), 400
            
            if self.bot:
                # Esegui unban asincrono
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(
                        self.bot.unban_user_from_dashboard(int(user_id))
                    )
                    return jsonify(result)
                finally:
                    loop.close()
            else:
                return jsonify({'success': False, 'message': 'Bot non attivo'}), 503
        
        @self.app.route('/api/user/search/<int:user_id>')
        @auth_required
        def api_user_search(user_id):
            """API per cercare informazioni su un utente."""
            if not self.user_manager:
                return jsonify({'error': 'Sistema non inizializzato'}), 503
            
            user_data = self.user_manager.search_user_messages(user_id)
            return jsonify(user_data)

        @self.app.route('/api/recent-activity')
        @auth_required
        def api_recent_activity():
            """API dedicata per attività recente sulla homepage."""
            try:
                from datetime import datetime

                if not self.bot or not self.bot.csv_manager:
                    return jsonify({'error': 'Bot non attivo', 'messages': []})
                
                # Leggi TUTTI i messaggi dal CSV (senza limit)
                all_messages = self.bot.csv_manager.read_csv_data("messages")
                
                if not all_messages:
                    return jsonify({'success': True, 'messages': [], 'total': 0})
                
                # Ordina per timestamp (più recente prima)
                def parse_timestamp(msg):
                    timestamp = msg.get('timestamp', '')
                    try:
                        if timestamp:
                            # Prova diversi formati di timestamp
                            if 'T' in timestamp:  # ISO format
                                return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            else:  # Altri formati
                                return datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                        return datetime.min  # Se non c'è timestamp, metti alla fine
                    except:
                        return datetime.min
                
                # Ordina dal più recente al più vecchio
                sorted_messages = sorted(all_messages, key=parse_timestamp, reverse=True)
                
                # Prendi solo i primi 5
                recent_messages = sorted_messages[:5]
                
                # Debug log per verificare l'ordinamento
                self.logger.info(f"Messaggi trovati: {len(all_messages)}, primi 5 timestamps:")
                for i, msg in enumerate(recent_messages):
                    timestamp = msg.get('timestamp', 'N/A')
                    self.logger.info(f"  {i+1}. {timestamp}")
                
                # Formatta i dati per la homepage
                formatted_messages = []
                for msg in recent_messages:
                    # Converti il timestamp in formato più leggibile
                    timestamp = msg.get('timestamp', '')
                    try:
                        from datetime import datetime
                        if timestamp:
                            if 'T' in timestamp:  # ISO format
                                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            else:
                                dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                            formatted_time = dt.strftime('%d/%m %H:%M')
                        else:
                            formatted_time = 'N/A'
                    except Exception as e:
                        self.logger.warning(f"Errore parsing timestamp '{timestamp}': {e}")
                        formatted_time = timestamp[:16] if timestamp else 'N/A'
                    
                    # Tronca il messaggio se troppo lungo
                    message_text = msg.get('messaggio', '')
                    if len(message_text) > 50:
                        message_text = message_text[:50] + '...'
                    
                    # Determina lo stato
                    approvato = msg.get('approvato', 'NO')
                    if approvato == 'SI':
                        status_badge = '<span class="badge bg-success">Approvato</span>'
                    else:
                        status_badge = '<span class="badge bg-danger">Eliminato</span>'
                    
                    formatted_messages.append({
                        'timestamp': formatted_time,
                        'username': msg.get('username', 'N/A'),
                        'message': message_text,
                        'status': status_badge,
                        'group': msg.get('group_name', 'N/A'),
                        'raw_timestamp': timestamp  # Per debug
                    })
                
                return jsonify({
                    'success': True,
                    'messages': formatted_messages,
                    'total': len(formatted_messages),
                    'total_in_db': len(all_messages)  # Per debug
                })
                
            except Exception as e:
                self.logger.error(f"Errore API recent activity: {e}", exc_info=True)
                return jsonify({
                    'error': str(e),
                    'messages': []
                })
        
        # --- Configurazioni ---
        @self.app.route('/config')
        @auth_required
        def config():
            """Pagina configurazioni con fallback sicuro."""
            try:
                if not self.config_editor:
                    # Fallback: carica configurazione base direttamente
                    self.logger.warning("ConfigurationManager non disponibile, usando fallback")
                    
                    # Carica configurazione base
                    try:
                        basic_config = self.config_manager.config
                    except:
                        basic_config = {}
                    
                    # Crea configurazione sicura con defaults
                    safe_config = {
                        'banned_words': basic_config.get('banned_words', []),
                        'whitelist_words': basic_config.get('whitelist_words', []),
                        'exempt_users': basic_config.get('exempt_users', []),
                        'allowed_languages': basic_config.get('allowed_languages', ['it']),
                        'auto_approve_short_messages': basic_config.get('auto_approve_short_messages', True),
                        'short_message_max_length': basic_config.get('short_message_max_length', 4),
                        'first_messages_threshold': basic_config.get('first_messages_threshold', 3),
                        'rules_command_enabled': basic_config.get('rules_command_enabled', True),
                        'rules_message': basic_config.get('rules_message', ''),
                        'night_mode': {
                            'enabled': basic_config.get('night_mode', {}).get('enabled', True),
                            'start_hour': basic_config.get('night_mode', {}).get('start_hour', '23:00'),
                            'end_hour': basic_config.get('night_mode', {}).get('end_hour', '07:00'),
                            'night_mode_groups': basic_config.get('night_mode', {}).get('night_mode_groups', []),
                            'grace_period_seconds': basic_config.get('night_mode', {}).get('grace_period_seconds', 15)
                        },
                        'spam_detector': {
                            'time_window_hours': basic_config.get('spam_detector', {}).get('time_window_hours', 1),
                            'similarity_threshold': basic_config.get('spam_detector', {}).get('similarity_threshold', 0.85),
                            'min_groups': basic_config.get('spam_detector', {}).get('min_groups', 2)
                        }
                    }
                    
                    bot_status = self.get_bot_status()
                    return render_template('config.html', 
                                        config=safe_config,
                                        bot_status=bot_status)
                
                # Uso normale del ConfigurationManager
                current_config = self.config_editor.get_editable_config()
                bot_status = self.get_bot_status()
                
                return render_template('config.html', 
                                    config=current_config,
                                    bot_status=bot_status)
                                    
            except Exception as e:
                self.logger.error(f"Errore nella route /config: {e}", exc_info=True)
                flash(f'Errore nel caricamento delle configurazioni: {str(e)}', 'danger')
                return redirect(url_for('index'))
        
        @self.app.route('/api/config/update', methods=['POST'])
        @auth_required
        def api_config_update():
            """API per aggiornare configurazioni."""
            if not self.config_editor:
                return jsonify({'success': False, 'message': 'Sistema non inizializzato'}), 503
            
            data = request.get_json()
            section = data.get('section')
            new_values = data.get('values', {})
            
            if not section:
                return jsonify({'success': False, 'message': 'Sezione richiesta'}), 400
            
            # Valida modifiche
            is_valid, errors = self.config_editor.validate_config_changes(section, new_values)
            if not is_valid:
                return jsonify({'success': False, 'message': 'Errori di validazione', 'errors': errors}), 400
            
            # Crea backup
            backup_path = self.config_editor.backup_current_config()
            
            # Applica modifiche
            success, message = self.config_editor.update_config_section(section, new_values)
            
            if success and self.bot:
                # Ricarica configurazione nel bot
                self.bot.reload_configuration()
            
            return jsonify({
                'success': success,
                'message': message,
                'backup_created': backup_path
            })
        
        # --- System Prompt ---
        @self.app.route('/prompt')
        @auth_required
        def system_prompt():
            """Pagina gestione system prompt con fallback sicuro."""
            try:
                if not self.prompt_manager:
                    self.logger.warning("SystemPromptManager non disponibile, usando fallback")
                    # Fallback: prompt di default
                    temp_manager = SystemPromptManager(self.logger, None)
                    default_prompt = temp_manager._get_default_prompt()
                    
                    bot_status = self.get_bot_status()
                    return render_template('system_prompt.html', 
                                        current_prompt=default_prompt,
                                        bot_status=bot_status)
                
                # Uso normale del SystemPromptManager
                current_prompt = self.prompt_manager.get_current_prompt()
                bot_status = self.get_bot_status()
                
                return render_template('system_prompt.html', 
                                    current_prompt=current_prompt,
                                    bot_status=bot_status)
                                    
            except Exception as e:
                self.logger.error(f"Errore nella route /prompt: {e}", exc_info=True)
                flash(f'Errore nel caricamento del system prompt: {str(e)}', 'danger')
                return redirect(url_for('index'))

        @self.app.route('/api/prompt/test', methods=['POST'])
        @auth_required
        def api_prompt_test():
            """API per testare il prompt con OpenAI."""
            data = request.get_json()
            message = data.get('message', '').strip()
            prompt = data.get('prompt', '').strip()
            
            if not message:
                return jsonify({'success': False, 'message': 'Messaggio richiesto'}), 400
            
            if not prompt:
                return jsonify({'success': False, 'message': 'Prompt richiesto'}), 400
            
            # Usa la stessa logica del bot per testare con OpenAI
            if self.bot and self.bot.moderation_logic and self.bot.moderation_logic.openai_client:
                try:
                    # Temporaneamente sostituisci il system prompt per il test
                    original_prompt = self.bot.moderation_logic.system_prompt if hasattr(self.bot.moderation_logic, 'system_prompt') else None
                    
                    # Usa il prompt custom per il test
                    response = self.bot.moderation_logic.openai_client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": message}
                        ],
                        temperature=0.0,
                        max_tokens=50,
                        timeout=15
                    )
                    
                    result_text = response.choices[0].message.content.strip() if response.choices[0].message.content else ""
                    
                    # Parsing della risposta (stessa logica del bot)
                    is_inappropriate = "INAPPROPRIATO: SI" in result_text
                    is_question = "DOMANDA: SI" in result_text
                    is_disallowed_language = "LINGUA: NON CONSENTITA" in result_text
                    
                    return jsonify({
                        'success': True,
                        'result': {
                            'inappropriate': is_inappropriate,
                            'question': is_question,
                            'language': 'CONSENTITA' if not is_disallowed_language else 'NON CONSENTITA',
                            'raw_response': result_text,
                            'tokens_used': response.usage.total_tokens if hasattr(response, 'usage') else 'N/A'
                        }
                    })
                    
                except Exception as e:
                    self.logger.error(f"Errore test OpenAI: {e}")
                    return jsonify({
                        'success': False, 
                        'message': f'Errore API OpenAI: {str(e)}'
                    }), 500
            else:
                return jsonify({
                    'success': False, 
                    'message': 'Bot non attivo o OpenAI non configurato'
                }), 503
        
        @self.app.route('/api/prompt/update', methods=['POST'])
        @auth_required
        def api_prompt_update():
            """API per aggiornare system prompt."""
            if not self.prompt_manager:
                return jsonify({'success': False, 'message': 'Sistema non inizializzato'}), 503
            
            data = request.get_json()
            new_prompt = data.get('prompt', '').strip()
            
            if not new_prompt:
                return jsonify({'success': False, 'message': 'Prompt non può essere vuoto'}), 400
            
            success = self.prompt_manager.update_prompt(new_prompt)
            
            return jsonify({
                'success': success,
                'message': 'Prompt aggiornato con successo' if success else 'Errore aggiornamento prompt'
            })
        
        @self.app.route('/api/prompt/reset', methods=['POST'])
        @auth_required
        def api_prompt_reset():
            """API per reset prompt a default."""
            if not self.prompt_manager:
                return jsonify({'success': False, 'message': 'Sistema non inizializzato'}), 503
            
            success = self.prompt_manager.reset_to_default()
            
            return jsonify({
                'success': success,
                'message': 'Prompt ripristinato alle impostazioni predefinite' if success else 'Errore reset prompt'
            })
        
        # --- Backup e Download ---
        @self.app.route('/backup')
        @auth_required
        def backup_page():
            """Pagina backup e download con fallback sicuro."""
            try:
                csv_stats = {}
                if self.bot and self.bot.csv_manager:
                    csv_stats = self.bot.csv_manager.get_csv_stats()
                else:
                    # Fallback: stats vuote
                    csv_stats = {
                        'messages': 0,
                        'admin': 0,
                        'banned_users': 0,
                        'unban_history': 0,
                        'csv_disabled': True
                    }
                
                bot_status = self.get_bot_status()
                
                return render_template('backup.html', 
                                    csv_stats=csv_stats,
                                    bot_status=bot_status)
                                    
            except Exception as e:
                self.logger.error(f"Errore nella route /backup: {e}", exc_info=True)
                flash(f'Errore nel caricamento della pagina backup: {str(e)}', 'danger')
                return redirect(url_for('index'))
        
        @self.app.route('/api/backup/create', methods=['POST'])
        @auth_required
        def api_create_backup():
            """API per creare backup."""
            if not self.bot or not self.bot.csv_manager:
                return jsonify({'success': False, 'message': 'Bot non attivo'}), 503
            
            try:
                success = self.bot.csv_manager.backup_csv_files()
                return jsonify({
                    'success': success,
                    'message': 'Backup creato con successo' if success else 'Errore creazione backup'
                })
            except Exception as e:
                return jsonify({'success': False, 'message': f'Errore: {str(e)}'}), 500
        
        @self.app.route('/download/csv/<table_name>')
        @auth_required
        def download_csv(table_name):
            """Download file CSV."""
            if not self.bot or not self.bot.csv_manager:
                flash('Bot non attivo', 'danger')
                return redirect(url_for('backup_page'))
            
            try:
                csv_structure = self.bot.csv_manager.csv_structure
                if table_name not in csv_structure:
                    flash('Tabella non trovata', 'danger')
                    return redirect(url_for('backup_page'))
                
                file_path = os.path.join(
                    self.bot.csv_manager.data_dir,
                    csv_structure[table_name]['filename']
                )
                
                if not os.path.exists(file_path):
                    flash('File non trovato', 'danger')
                    return redirect(url_for('backup_page'))
                
                return send_file(
                    file_path,
                    as_attachment=True,
                    download_name=f"{table_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                )
                
            except Exception as e:
                self.logger.error(f"Errore download CSV {table_name}: {e}")
                flash(f'Errore download: {str(e)}', 'danger')
                return redirect(url_for('backup_page'))
        
        # --- Analytics ---
        @self.app.route('/analytics')
        @auth_required
        def analytics():
            """Pagina analytics e statistiche."""
            if not self.user_manager:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            insights = self.user_manager.get_moderation_insights()
            activity_summary = self.user_manager.get_user_activity_summary(days=7)
            daily_stats = self.user_manager.get_message_statistics_by_timeframe('daily')
            
            return render_template('analytics.html',
                                 insights=insights,
                                 activity_summary=activity_summary,
                                 daily_stats=daily_stats[-30:])  # Ultimi 30 giorni
        
        @self.app.route('/api/analytics/timeframe/<timeframe>')
        @auth_required
        def api_analytics_timeframe(timeframe):
            """API per dati analytics per timeframe."""
            if not self.user_manager:
                return jsonify({'error': 'Sistema non inizializzato'}), 503
            
            if timeframe not in ['daily', 'weekly', 'monthly']:
                return jsonify({'error': 'Timeframe non valido'}), 400
            
            stats = self.user_manager.get_message_statistics_by_timeframe(timeframe)
            return jsonify(stats)
        
        @self.app.route('/api/analytics/unban-stats')
        @auth_required
        def api_unban_stats():
            """API per statistiche unban."""
            if not self.user_manager:
                return jsonify({'error': 'Sistema non inizializzato'}), 503
            
            stats = self.user_manager.get_unban_statistics()
            return jsonify(stats)
        
        # --- Error Handlers ---
        @self.app.errorhandler(404)
        def not_found(error):
            bot_status = self.get_bot_status()
            return render_template('error.html', 
                                error_code=404, 
                                error_message="Pagina non trovata",
                                bot_status=bot_status), 404
        
        @self.app.errorhandler(500)
        def internal_error(error):
            bot_status = self.get_bot_status()
            self.logger.error(f"Errore 500: {error}", exc_info=True)
            return render_template('error.html', 
                                error_code=500, 
                                error_message="Errore interno del server",
                                bot_status=bot_status), 500
        
        @self.app.errorhandler(401)
        def unauthorized(error):
            """Handler per errori di autenticazione."""
            flash('Accesso non autorizzato. Effettua il login.', 'warning')
            return redirect(url_for('login'))

        @self.app.errorhandler(403)
        def forbidden(error):
            """Handler per accesso negato."""
            flash('Accesso negato. Privilegi insufficienti.', 'danger')
            return redirect(url_for('index'))
    
    # --- Metodi di gestione Bot ---
    
    def start_bot_async(self):
        """Avvia il bot in un thread separato."""
        if self.bot_thread and self.bot_thread.is_alive():
            raise Exception("Bot già in esecuzione")
        
        def run_bot():
            """Funzione che esegue il bot in un thread separato con loop asyncio dedicato."""
            try:
                # Crea un nuovo loop asyncio per questo thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                self.bot = TelegramModerationBot()
                
                # Inizializza managers con il bot
                self.user_manager = UserManagementSystem(
                    self.bot.logger, 
                    self.bot.csv_manager, 
                    self.bot.config_manager
                )
                
                self.prompt_manager = SystemPromptManager(
                    self.bot.logger,
                    self.bot.moderation_logic
                )
                
                self.config_editor = ConfigurationManager(
                    self.bot.config_manager,
                    self.bot.logger
                )
                
                self.logger.info("Bot avviato dalla dashboard")
                
                # Avvia il bot nel loop asyncio del thread
                try:
                    self.bot.start()
                except KeyboardInterrupt:
                    self.logger.info("Bot interrotto da KeyboardInterrupt")
                except Exception as e:
                    self.logger.error(f"Errore durante l'esecuzione del bot: {e}", exc_info=True)
                finally:
                    # Cleanup
                    try:
                        # Chiudi il loop asyncio
                        loop.close()
                    except:
                        pass
                    
            except Exception as e:
                self.logger.error(f"Errore critico nel thread del bot: {e}", exc_info=True)
                self.bot = None
        
        self.bot_thread = threading.Thread(target=run_bot, daemon=True)
        self.bot_thread.start()
        
        # Aspetta più tempo per il bot in thread secondario
        import time
        self.logger.info("Attendendo inizializzazione bot...")
        
        for i in range(10):  # Aspetta fino a 10 secondi
            time.sleep(1)
            if self.bot and hasattr(self.bot, '_is_running') and self.bot._is_running:
                self.logger.info(f"Bot inizializzato con successo dopo {i+1} secondi")
                return
        
        # Se arriviamo qui, il bot potrebbe non essersi avviato
        if not self.bot:
            raise Exception("Errore avvio bot - bot instance non creata")
        elif not hasattr(self.bot, '_is_running') or not self.bot._is_running:
            raise Exception("Errore avvio bot - bot non in stato running")
        else:
            self.logger.warning("Bot avviato ma stato incerto")
    
    def stop_bot_async(self):
        """Ferma il bot con timeout e force stop se necessario."""
        if self.bot:
            try:
                self.logger.info("Inizio stop del bot...")
                
                # 1. Stop normale
                self.bot.stop()
                
                # 2. Aspetta che il thread termini con timeout più lungo
                if self.bot_thread and self.bot_thread.is_alive():
                    self.logger.info("Attendendo terminazione thread bot...")
                    self.bot_thread.join(timeout=15)  # Aumentato a 15 secondi
                    
                    # 3. Se il thread è ancora vivo, prova force stop
                    if self.bot_thread.is_alive():
                        self.logger.warning("Thread non si è fermato, tentando force stop...")
                        
                        if hasattr(self.bot, 'force_stop'):
                            self.bot.force_stop()
                        
                        # Aspetta ancora un po'
                        self.bot_thread.join(timeout=5)
                        
                        if self.bot_thread.is_alive():
                            self.logger.error("Thread del bot non si è fermato nemmeno con force stop")
                            # In questo caso, semplicemente procediamo
                            # Il thread daemon si fermerà quando l'applicazione si chiude
                        else:
                            self.logger.info("Force stop riuscito")
                    else:
                        self.logger.info("Thread bot terminato correttamente")
                
                self.logger.info("Bot fermato dalla dashboard")
                
            except Exception as e:
                self.logger.error(f"Errore durante stop bot: {e}", exc_info=True)
            finally:
                # Reset stato sempre
                self.bot = None
                self.user_manager = None
                self.prompt_manager = None
                self.config_editor = None
    
    # --- Metodi helper ---

    def get_bot_debug_info(self) -> Dict[str, Any]:
        """Restituisce informazioni di debug sul bot (versione corretta)."""
        if not self.bot:
            return {
                'bot_exists': False, 
                'thread_alive': bool(self.bot_thread and self.bot_thread.is_alive()),
                'is_running': False,
                'has_application': False,
                'application_running': False,
                'scheduler_active': False
            }
        
        return {
            'bot_exists': True,
            'thread_alive': bool(self.bot_thread and self.bot_thread.is_alive()),
            'is_running': getattr(self.bot, '_is_running', False),
            'has_application': hasattr(self.bot, 'application'),
            'application_running': hasattr(self.bot, 'application') and self.bot.application and hasattr(self.bot.application, 'updater') and getattr(self.bot.application.updater, 'running', False),
            'scheduler_active': getattr(self.bot, 'scheduler_active', False)
        }
    
    def get_bot_status(self) -> Dict[str, Any]:
        """Restituisce lo stato del bot per la dashboard."""
        if self.bot:
            return self.bot.get_bot_status()
        else:
            return {
                'is_running': False,
                'start_time': None,
                'uptime_seconds': 0,  # Sempre 0 se bot non attivo
                'night_mode_active': False,
                'scheduler_active': False,
                'stats': {}
            }
    
    def get_recent_activity_stats(self) -> Dict[str, Any]:
        """Restituisce statistiche recenti per la homepage."""
        if not self.user_manager:
            return {
                'total_messages_24h': 0,
                'rejected_messages_24h': 0,
                'new_bans_24h': 0,
                'approval_rate_24h': 0
            }
        
        try:
            activity = self.user_manager.get_user_activity_summary(days=1)
            return {
                'total_messages_24h': activity.get('total_messages', 0),
                'rejected_messages_24h': activity.get('rejected_messages', 0),
                'new_bans_24h': activity.get('total_bans', 0),
                'approval_rate_24h': activity.get('approval_rate', 0)
            }
        except Exception as e:
            self.logger.error(f"Errore calcolo statistiche recenti: {e}")
            return {
                'total_messages_24h': 0,
                'rejected_messages_24h': 0,
                'new_bans_24h': 0,
                'approval_rate_24h': 0
            }
    
    def run(self, host='127.0.0.1', port=5000, debug=False):
        """Avvia l'applicazione Flask."""
        self.logger.info(f"Avvio dashboard su {host}:{port}")
        self.app.run(host=host, port=port, debug=debug)


# --- Context Processors per template ---
def setup_template_context(app):
    """Configura context processors per i template."""
    
    @app.context_processor
    def inject_common_vars():
        return {
            'current_time': datetime.now(),
            'app_name': 'Bot Moderazione Dashboard',
            'app_version': '1.0.0',
            'is_authenticated': is_authenticated(),
            'current_username': get_current_username(),
            'auth_user': current_user if current_user.is_authenticated else None
        }

# --- Funzione principale ---
def create_app():
    """Factory function per creare l'app Flask."""
    dashboard = DashboardApp()
    # NOTA: setup_template_context non è più necessario qui 
    # perché le funzioni sono già configurate in __init__
    setup_template_context(dashboard.app)
    return dashboard

if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    
    required_vars = ["TELEGRAM_BOT_TOKEN", "DASHBOARD_SECRET_KEY", "DASHBOARD_USERNAME", "DASHBOARD_PASSWORD"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        print(f"❌ ERRORE: Variabili d'ambiente mancanti: {missing_vars}")
        print("\nControlla il file .env e assicurati di avere:")
        for var in missing_vars:
            print(f"  {var}=your-value")
        sys.exit(1)
    
    # Verifica variabili d'ambiente
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        print("ERRORE: TELEGRAM_BOT_TOKEN non impostato!")
        print("Controlla il file .env o imposta la variabile d'ambiente:")
        print("export TELEGRAM_BOT_TOKEN='your-bot-token'")
        sys.exit(1)
    
    # Crea e avvia dashboard
    dashboard = create_app()
    
    # Configurazione da environment o default
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    debug = os.getenv("DASHBOARD_DEBUG", "False").lower() == "true"
    
    print(f"""
        🔐 Dashboard Bot Moderazione (PROTETTA)
        📍 URL: http://{host}:{port}
        🔧 Debug: {debug}
        🔑 Login richiesto per l'accesso
            """)
    
    try:
        dashboard.run(host=host, port=port, debug=debug)
    except KeyboardInterrupt:
        print("\n👋 Dashboard fermata dall'utente")
    except Exception as e:
        print(f"\n❌ Errore dashboard: {e}")
        sys.exit(1)