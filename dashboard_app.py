import os
import sys
import asyncio
import threading
import json
from datetime import datetime
from typing import Dict, Any, Optional

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file
from flask import session as flask_session
import logging

# Aggiungi il percorso src per importazioni
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.bot_core import TelegramModerationBot
from src.config_manager import ConfigManager
from src.user_management import UserManagementSystem, SystemPromptManager, ConfigurationManager


class DashboardApp:
    """
    Applicazione Flask per la dashboard di gestione del bot.
    """
    
    def __init__(self):
        self.app = Flask(__name__)
        self.app.secret_key = os.getenv("DASHBOARD_SECRET_KEY", "your-secret-key-change-this")
        
        # Configurazione Flask
        self.app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file upload
        
        # Setup logging per dashboard
        self.setup_dashboard_logging()
        
        # Bot instance (inizialmente None)
        self.bot: Optional[TelegramModerationBot] = None
        self.bot_thread: Optional[threading.Thread] = None
        
        # Managers
        self.config_manager = ConfigManager()
        self.user_manager: Optional[UserManagementSystem] = None
        self.prompt_manager: Optional[SystemPromptManager] = None
        self.config_editor: Optional[ConfigurationManager] = None
        
        # Setup routes
        self.setup_routes()
        
        self.logger = logging.getLogger("Dashboard")
        self.logger.info("Dashboard inizializzata")
    
    def setup_dashboard_logging(self):
        """Configura logging separato per la dashboard."""
        dashboard_logger = logging.getLogger("Dashboard")
        dashboard_logger.setLevel(logging.INFO)
        
        if not dashboard_logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - DASHBOARD - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            dashboard_logger.addHandler(handler)
    
    def setup_routes(self):
        """Configura tutte le route della dashboard."""
        
        # --- Route principali ---
        @self.app.route('/')
        def index():
            """Homepage della dashboard."""
            bot_status = self.get_bot_status()
            recent_stats = self.get_recent_activity_stats()
            
            return render_template('dashboard/index.html', 
                                 bot_status=bot_status,
                                 recent_stats=recent_stats)
        
        # --- Controllo Bot ---
        @self.app.route('/bot/start', methods=['POST'])
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
        
        @self.app.route('/bot/stop', methods=['POST'])
        def stop_bot():
            """Ferma il bot."""
            try:
                self.stop_bot_async()
                flash('Bot fermato con successo!', 'success')
            except Exception as e:
                self.logger.error(f"Errore stop bot: {e}")
                flash(f'Errore stop bot: {str(e)}', 'danger')
            
            return redirect(url_for('index'))
        
        @self.app.route('/api/bot/status')
        def api_bot_status():
            """API per stato del bot."""
            return jsonify(self.get_bot_status())
        
        # --- Gestione Messaggi ---
        @self.app.route('/messages')
        def messages():
            """Pagina messaggi recenti."""
            limit = request.args.get('limit', 30, type=int)
            
            if not self.user_manager:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            recent_messages = self.bot.get_recent_messages(limit) if self.bot else []
            return render_template('dashboard/messages.html', 
                                 messages=recent_messages, 
                                 limit=limit)
        
        @self.app.route('/messages/deleted')
        def deleted_messages():
            """Pagina messaggi eliminati."""
            limit = request.args.get('limit', 20, type=int)
            
            if not self.user_manager:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            deleted_msgs = self.bot.get_recent_deleted_messages(limit) if self.bot else []
            return render_template('dashboard/deleted_messages.html', 
                                 messages=deleted_msgs, 
                                 limit=limit)
        
        # --- Gestione Utenti ---
        @self.app.route('/users/banned')
        def banned_users():
            """Pagina utenti bannati."""
            limit = request.args.get('limit', 30, type=int)
            
            if not self.user_manager:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            banned_users_list = self.user_manager.get_banned_users_detailed(limit)
            return render_template('dashboard/banned_users.html', 
                                 banned_users=banned_users_list, 
                                 limit=limit)
        
        @self.app.route('/api/user/ban', methods=['POST'])
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
        def api_user_search(user_id):
            """API per cercare informazioni su un utente."""
            if not self.user_manager:
                return jsonify({'error': 'Sistema non inizializzato'}), 503
            
            user_data = self.user_manager.search_user_messages(user_id)
            return jsonify(user_data)
        
        # --- Configurazioni ---
        @self.app.route('/config')
        def config():
            """Pagina configurazioni."""
            if not self.config_editor:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            current_config = self.config_editor.get_editable_config()
            return render_template('dashboard/config.html', config=current_config)
        
        @self.app.route('/api/config/update', methods=['POST'])
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
        def system_prompt():
            """Pagina gestione system prompt."""
            if not self.prompt_manager:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            current_prompt = self.prompt_manager.get_current_prompt()
            prompt_history = self.prompt_manager.get_prompt_history()
            
            return render_template('dashboard/system_prompt.html', 
                                 current_prompt=current_prompt,
                                 prompt_history=prompt_history)
        
        @self.app.route('/api/prompt/update', methods=['POST'])
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
        def backup_page():
            """Pagina backup e download."""
            csv_stats = {}
            if self.bot and self.bot.csv_manager:
                csv_stats = self.bot.csv_manager.get_csv_stats()
            
            return render_template('dashboard/backup.html', csv_stats=csv_stats)
        
        @self.app.route('/api/backup/create', methods=['POST'])
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
        def analytics():
            """Pagina analytics e statistiche."""
            if not self.user_manager:
                flash('Sistema non inizializzato', 'danger')
                return redirect(url_for('index'))
            
            insights = self.user_manager.get_moderation_insights()
            activity_summary = self.user_manager.get_user_activity_summary(days=7)
            daily_stats = self.user_manager.get_message_statistics_by_timeframe('daily')
            
            return render_template('dashboard/analytics.html',
                                 insights=insights,
                                 activity_summary=activity_summary,
                                 daily_stats=daily_stats[-30:])  # Ultimi 30 giorni
        
        @self.app.route('/api/analytics/timeframe/<timeframe>')
        def api_analytics_timeframe(timeframe):
            """API per dati analytics per timeframe."""
            if not self.user_manager:
                return jsonify({'error': 'Sistema non inizializzato'}), 503
            
            if timeframe not in ['daily', 'weekly', 'monthly']:
                return jsonify({'error': 'Timeframe non valido'}), 400
            
            stats = self.user_manager.get_message_statistics_by_timeframe(timeframe)
            return jsonify(stats)
        
        @self.app.route('/api/analytics/unban-stats')
        def api_unban_stats():
            """API per statistiche unban."""
            if not self.user_manager:
                return jsonify({'error': 'Sistema non inizializzato'}), 503
            
            stats = self.user_manager.get_unban_statistics()
            return jsonify(stats)
        
        # --- Error Handlers ---
        @self.app.errorhandler(404)
        def not_found(error):
            return render_template('dashboard/error.html', 
                                 error_code=404, 
                                 error_message="Pagina non trovata"), 404
        
        @self.app.errorhandler(500)
        def internal_error(error):
            return render_template('dashboard/error.html', 
                                 error_code=500, 
                                 error_message="Errore interno del server"), 500
    
    # --- Metodi di gestione Bot ---
    
    def start_bot_async(self):
        """Avvia il bot in un thread separato."""
        if self.bot_thread and self.bot_thread.is_alive():
            raise Exception("Bot già in esecuzione")
        
        def run_bot():
            try:
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
                self.bot.start()
                
            except Exception as e:
                self.logger.error(f"Errore esecuzione bot: {e}")
                self.bot = None
        
        self.bot_thread = threading.Thread(target=run_bot, daemon=True)
        self.bot_thread.start()
        
        # Aspetta un momento per verificare che il bot si sia avviato
        import time
        time.sleep(2)
        
        if not self.bot:
            raise Exception("Errore avvio bot")
    
    def stop_bot_async(self):
        """Ferma il bot."""
        if self.bot:
            try:
                self.bot.stop()
                self.logger.info("Bot fermato dalla dashboard")
            except Exception as e:
                self.logger.error(f"Errore stop bot: {e}")
            finally:
                self.bot = None
        
        if self.bot_thread and self.bot_thread.is_alive():
            # Il thread dovrebbe terminare automaticamente quando il bot si ferma
            pass
    
    # --- Metodi helper ---
    
    def get_bot_status(self) -> Dict[str, Any]:
        """Restituisce lo stato del bot per la dashboard."""
        if self.bot:
            return self.bot.get_bot_status()
        else:
            return {
                'is_running': False,
                'start_time': None,
                'uptime_seconds': 0,
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
            'app_version': '1.0.0'
        }


# --- Funzione principale ---
def create_app():
    """Factory function per creare l'app Flask."""
    dashboard = DashboardApp()
    setup_template_context(dashboard.app)
    return dashboard


if __name__ == '__main__':
    # CARICA .env file prima di tutto
    from dotenv import load_dotenv
    load_dotenv()
    
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
🚀 Dashboard Bot Moderazione
📍 URL: http://{host}:{port}
🔧 Debug: {debug}
    """)
    
    try:
        dashboard.run(host=host, port=port, debug=debug)
    except KeyboardInterrupt:
        print("\n👋 Dashboard fermata dall'utente")
    except Exception as e:
        print(f"\n❌ Errore dashboard: {e}")
        sys.exit(1)