from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash
import os
import json
import psutil
import subprocess
import signal
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import logging
import csv
import zipfile
import tempfile
from pathlib import Path
import threading
import time

# Import delle classi del bot
from src.config_manager import ConfigManager
from src.csv_interface import CSVDataManager
from src.logger_config import LoggingConfigurator

app = Flask(__name__)
app.secret_key = os.environ.get('DASHBOARD_SECRET_KEY', 'your-secret-key-change-me')

class BotDashboard:
    def __init__(self):
        self.config_manager = ConfigManager()
        self.logger = LoggingConfigurator.setup_logging(
            log_dir='logs/dashboard',
            disable_console=False
        )
        self.csv_manager = CSVDataManager(self.logger, self.config_manager)
        
        # Percorsi importanti
        self.bot_pid_file = "bot.pid"
        self.bot_script = "main.py"
        
        # Cache per ottimizzare le performance
        self.last_status_check = None
        self.cached_status = None
        self.cache_duration = 2  # secondi
        
        self.logger.info("Dashboard inizializzata")

    def get_bot_status(self) -> Dict[str, Any]:
        """Ottiene lo stato del bot con cache per performance"""
        now = time.time()
        if (self.cached_status and self.last_status_check and 
            now - self.last_status_check < self.cache_duration):
            return self.cached_status
        
        status = {
            'online': False,
            'pid': None,
            'uptime': None,
            'memory_usage': None,
            'cpu_usage': None,
            'last_check': datetime.now().isoformat()
        }
        
        try:
            # Controlla se esiste il file PID
            if os.path.exists(self.bot_pid_file):
                with open(self.bot_pid_file, 'r') as f:
                    pid = int(f.read().strip())
                
                # Verifica se il processo è ancora attivo
                if psutil.pid_exists(pid):
                    process = psutil.Process(pid)
                    if 'python' in process.name().lower() and 'main.py' in ' '.join(process.cmdline()):
                        status['online'] = True
                        status['pid'] = pid
                        status['uptime'] = datetime.now() - datetime.fromtimestamp(process.create_time())
                        status['memory_usage'] = process.memory_info().rss / 1024 / 1024  # MB
                        status['cpu_usage'] = process.cpu_percent()
                else:
                    # PID file obsoleto, rimuovilo
                    os.remove(self.bot_pid_file)
            
        except Exception as e:
            self.logger.error(f"Errore nel controllo stato bot: {e}")
        
        self.cached_status = status
        self.last_status_check = now
        return status

    def start_bot(self) -> Dict[str, Any]:
        """Avvia il bot"""
        try:
            if self.get_bot_status()['online']:
                return {'success': False, 'message': 'Il bot è già in esecuzione'}
            
            # Avvia il bot in background
            process = subprocess.Popen(
                ['python', self.bot_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid  # Crea un nuovo gruppo di processi
            )
            
            # Salva il PID
            with open(self.bot_pid_file, 'w') as f:
                f.write(str(process.pid))
            
            # Aspetta un momento per verificare che sia partito
            time.sleep(2)
            
            if self.get_bot_status()['online']:
                self.logger.info(f"Bot avviato con PID {process.pid}")
                return {'success': True, 'message': 'Bot avviato con successo', 'pid': process.pid}
            else:
                return {'success': False, 'message': 'Errore durante l\'avvio del bot'}
                
        except Exception as e:
            self.logger.error(f"Errore nell'avvio del bot: {e}")
            return {'success': False, 'message': f'Errore: {str(e)}'}

    def stop_bot(self) -> Dict[str, Any]:
        """Ferma il bot"""
        try:
            status = self.get_bot_status()
            if not status['online']:
                return {'success': False, 'message': 'Il bot non è in esecuzione'}
            
            pid = status['pid']
            
            # Invia SIGTERM per arresto pulito
            os.kill(pid, signal.SIGTERM)
            
            # Aspetta che il processo termini
            for _ in range(10):  # Aspetta fino a 10 secondi
                time.sleep(1)
                if not psutil.pid_exists(pid):
                    break
            
            # Se ancora non è terminato, forza la terminazione
            if psutil.pid_exists(pid):
                os.kill(pid, signal.SIGKILL)
                time.sleep(1)
            
            # Rimuovi il file PID
            if os.path.exists(self.bot_pid_file):
                os.remove(self.bot_pid_file)
            
            self.cached_status = None  # Invalida cache
            self.logger.info(f"Bot fermato (PID {pid})")
            return {'success': True, 'message': 'Bot fermato con successo'}
            
        except Exception as e:
            self.logger.error(f"Errore nell'arresto del bot: {e}")
            return {'success': False, 'message': f'Errore: {str(e)}'}

    def restart_bot(self) -> Dict[str, Any]:
        """Riavvia il bot"""
        try:
            stop_result = self.stop_bot()
            if not stop_result['success'] and 'non è in esecuzione' not in stop_result['message']:
                return stop_result
            
            time.sleep(2)  # Pausa tra stop e start
            
            start_result = self.start_bot()
            if start_result['success']:
                return {'success': True, 'message': 'Bot riavviato con successo'}
            else:
                return start_result
                
        except Exception as e:
            self.logger.error(f"Errore nel riavvio del bot: {e}")
            return {'success': False, 'message': f'Errore: {str(e)}'}

    def get_recent_deleted_messages(self, limit: int = 10) -> List[Dict]:
        """Ottiene gli ultimi messaggi cancellati"""
        try:
            messages_data = self.csv_manager.read_csv_data("messages", limit=100)
            
            # Filtra solo i messaggi non approvati (cancellati)
            deleted_messages = [
                msg for msg in messages_data 
                if msg.get('approvato', '').upper() == 'NO'
            ]
            
            # Ordina per timestamp (più recenti prima) e prendi i primi N
            deleted_messages.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            return deleted_messages[:limit]
            
        except Exception as e:
            self.logger.error(f"Errore nel recupero messaggi cancellati: {e}")
            return []

    def get_recent_bans(self, limit: int = 10) -> List[Dict]:
        """Ottiene i ban più recenti"""
        try:
            banned_data = self.csv_manager.read_csv_data("banned_users", limit=limit)
            
            # Ordina per timestamp (più recenti prima)
            banned_data.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            return banned_data
            
        except Exception as e:
            self.logger.error(f"Errore nel recupero utenti bannati: {e}")
            return []

    def unban_user(self, user_id: str) -> Dict[str, Any]:
        """Sbanna un utente rimuovendolo dal CSV"""
        try:
            # Leggi tutti i dati degli utenti bannati
            banned_file = os.path.join(self.csv_manager.data_dir, "banned_users.csv")
            
            if not os.path.exists(banned_file):
                return {'success': False, 'message': 'File utenti bannati non trovato'}
            
            # Leggi e filtra i dati
            updated_rows = []
            user_found = False
            
            with open(banned_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames
                
                for row in reader:
                    if row['user_id'] != user_id:
                        updated_rows.append(row)
                    else:
                        user_found = True
            
            if not user_found:
                return {'success': False, 'message': 'Utente non trovato nella lista dei bannati'}
            
            # Riscrivi il file senza l'utente sbannato
            with open(banned_file, 'w', newline='', encoding='utf-8') as f:
                if headers:
                    writer = csv.DictWriter(f, fieldnames=headers)
                    writer.writeheader()
                    writer.writerows(updated_rows)
            
            self.logger.info(f"Utente {user_id} sbannato tramite dashboard")
            return {'success': True, 'message': f'Utente {user_id} sbannato con successo'}
            
        except Exception as e:
            self.logger.error(f"Errore nello sbannare utente {user_id}: {e}")
            return {'success': False, 'message': f'Errore: {str(e)}'}

    def update_config(self, config_data: Dict) -> Dict[str, Any]:
        """Aggiorna la configurazione"""
        try:
            # Valida i dati prima di salvare
            validated_config = self._validate_config(config_data)
            
            # Aggiorna la configurazione
            current_config = self.config_manager.config
            current_config.update(validated_config)
            
            # Salva la configurazione
            self.config_manager.save_config(current_config)
            
            self.logger.info("Configurazione aggiornata via dashboard")
            return {'success': True, 'message': 'Configurazione aggiornata con successo'}
            
        except Exception as e:
            self.logger.error(f"Errore nell'aggiornamento configurazione: {e}")
            return {'success': False, 'message': f'Errore: {str(e)}'}

    def _validate_config(self, config_data: Dict) -> Dict:
        """Valida i dati di configurazione"""
        validated = {}
        
        # Validazione banned_words
        if 'banned_words' in config_data:
            banned_words = config_data['banned_words']
            if isinstance(banned_words, str):
                validated['banned_words'] = [word.strip() for word in banned_words.split('\n') if word.strip()]
            elif isinstance(banned_words, list):
                validated['banned_words'] = banned_words
        
        # Validazione exempt_users
        if 'exempt_users' in config_data:
            exempt_users = config_data['exempt_users']
            if isinstance(exempt_users, str):
                # Supporta sia ID numerici che username
                users = []
                for user in exempt_users.split('\n'):
                    user = user.strip()
                    if user:
                        try:
                            users.append(int(user))  # ID numerico
                        except ValueError:
                            users.append(user)  # Username
                validated['exempt_users'] = users
            elif isinstance(exempt_users, list):
                validated['exempt_users'] = exempt_users
        
        # Altri campi semplici
        simple_fields = ['allowed_languages', 'first_messages_threshold', 'backup_interval_days']
        for field in simple_fields:
            if field in config_data:
                validated[field] = config_data[field]
        
        # Night mode
        if 'night_mode' in config_data:
            night_mode = config_data['night_mode']
            if isinstance(night_mode, dict):
                validated['night_mode'] = night_mode
        
        return validated

    def get_system_info(self) -> Dict[str, Any]:
        """Ottiene informazioni di sistema"""
        try:
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            return {
                'cpu_usage': cpu_percent,
                'memory_usage': memory.percent,
                'memory_total': memory.total / 1024 / 1024 / 1024,  # GB
                'memory_used': memory.used / 1024 / 1024 / 1024,    # GB
                'disk_usage': disk.percent,
                'disk_total': disk.total / 1024 / 1024 / 1024,      # GB
                'disk_used': disk.used / 1024 / 1024 / 1024,        # GB
                'uptime': datetime.now() - datetime.fromtimestamp(psutil.boot_time())
            }
        except Exception as e:
            self.logger.error(f"Errore nel recupero info sistema: {e}")
            return {}

    def create_csv_download_package(self) -> Optional[str]:
        """Crea un package ZIP con tutti i file CSV"""
        try:
            # Crea un file ZIP temporaneo
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_filename = f"bot_data_{timestamp}.zip"
            zip_path = os.path.join(tempfile.gettempdir(), zip_filename)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # Aggiungi tutti i file CSV
                csv_dir = Path(self.csv_manager.data_dir)
                for csv_file in csv_dir.glob("*.csv"):
                    if csv_file.exists() and csv_file.stat().st_size > 0:
                        zipf.write(csv_file, csv_file.name)
                
                # Aggiungi anche i backup se esistono
                backup_dir = Path(self.csv_manager.backup_dir)
                if backup_dir.exists():
                    for backup_file in backup_dir.rglob("*.csv"):
                        arcname = f"backups/{backup_file.relative_to(backup_dir)}"
                        zipf.write(backup_file, arcname)
            
            return zip_path
            
        except Exception as e:
            self.logger.error(f"Errore nella creazione package CSV: {e}")
            return None

# Inizializzazione globale
dashboard = BotDashboard()

# Routes
@app.route('/')
def index():
    """Dashboard principale"""
    return render_template('dashboard.html')

@app.route('/api/status')
def api_status():
    """API per ottenere lo stato del bot"""
    bot_status = dashboard.get_bot_status()
    system_info = dashboard.get_system_info()
    
    return jsonify({
        'bot': bot_status,
        'system': system_info,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/bot/<action>', methods=['POST'])
def api_bot_control(action):
    """API per controllare il bot (start/stop/restart)"""
    if action == 'start':
        result = dashboard.start_bot()
    elif action == 'stop':
        result = dashboard.stop_bot()
    elif action == 'restart':
        result = dashboard.restart_bot()
    else:
        result = {'success': False, 'message': 'Azione non valida'}
    
    return jsonify(result)

@app.route('/api/recent-deleted')
def api_recent_deleted():
    """API per ottenere messaggi cancellati recenti"""
    limit = request.args.get('limit', 10, type=int)
    messages = dashboard.get_recent_deleted_messages(limit)
    return jsonify(messages)

@app.route('/api/recent-bans')
def api_recent_bans():
    """API per ottenere ban recenti"""
    limit = request.args.get('limit', 10, type=int)
    bans = dashboard.get_recent_bans(limit)
    return jsonify(bans)

@app.route('/api/unban/<user_id>', methods=['POST'])
def api_unban_user(user_id):
    """API per sbannare un utente"""
    result = dashboard.unban_user(user_id)
    return jsonify(result)

@app.route('/config')
def config_page():
    """Pagina di configurazione"""
    try:
        config = dashboard.config_manager.config
        # Assicurati che config non sia None
        if config is None:
            config = dashboard.config_manager._get_default_config()
        return render_template('config.html', config=config)
    except Exception as e:
        dashboard.logger.error(f"Errore nel caricamento pagina config: {e}")
        # Usa configurazione di default in caso di errore
        default_config = dashboard.config_manager._get_default_config()
        return render_template('config.html', config=default_config)

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """API per gestire la configurazione"""
    if request.method == 'GET':
        return jsonify(dashboard.config_manager.config)
    
    elif request.method == 'POST':
        config_data = request.get_json()
        result = dashboard.update_config(config_data)
        return jsonify(result)

@app.route('/logs')
def logs_page():
    """Pagina per visualizzare i log"""
    return render_template('logs.html')

@app.route('/api/logs')
def api_logs():
    """API per ottenere i log più recenti"""
    try:
        log_file = 'logs/moderation_bot.log'
        if not os.path.exists(log_file):
            return jsonify({'logs': [], 'message': 'File di log non trovato'})
        
        # Leggi le ultime 100 righe del log
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            recent_lines = lines[-100:] if len(lines) > 100 else lines
        
        return jsonify({'logs': recent_lines})
        
    except Exception as e:
        return jsonify({'logs': [], 'error': str(e)})

@app.route('/download')
def download_page():
    """Pagina per scaricare i dati"""
    csv_stats = dashboard.csv_manager.get_csv_stats()
    return render_template('download.html', csv_stats=csv_stats)

@app.route('/api/download/csv')
def api_download_csv():
    """API per scaricare tutti i CSV in un ZIP"""
    zip_path = dashboard.create_csv_download_package()
    if zip_path and os.path.exists(zip_path):
        return send_file(
            zip_path,
            as_attachment=True,
            download_name=os.path.basename(zip_path),
            mimetype='application/zip'
        )
    else:
        return jsonify({'error': 'Impossibile creare il package di download'}), 500

@app.route('/openai-prompt')
def openai_prompt_page():
    """Pagina per modificare il prompt OpenAI"""
    try:
        # Leggi il prompt dal file moderation_rules.py
        with open('src/moderation_rules.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Estrai il system_prompt (molto semplificato - potresti voler migliorare questo parsing)
        start_marker = 'system_prompt = ('
        end_marker = ')'
        
        start_idx = content.find(start_marker)
        if start_idx != -1:
            start_idx += len(start_marker)
            # Trova la parentesi di chiusura corrispondente
            paren_count = 1
            end_idx = start_idx
            while end_idx < len(content) and paren_count > 0:
                if content[end_idx] == '(':
                    paren_count += 1
                elif content[end_idx] == ')':
                    paren_count -= 1
                end_idx += 1
            
            if paren_count == 0:
                prompt_section = content[start_idx:end_idx-1]
                # Rimuovi le triple quotes e unisci le righe
                lines = prompt_section.split('\n')
                cleaned_lines = []
                for line in lines:
                    line = line.strip()
                    if line.startswith('"""') or line.startswith('"'):
                        line = line.lstrip('"').rstrip('"')
                    if line and not line.startswith('#'):
                        cleaned_lines.append(line)
                
                current_prompt = '\n'.join(cleaned_lines)
            else:
                current_prompt = "Errore nel parsing del prompt"
        else:
            current_prompt = "Prompt non trovato"
        
        return render_template('openai_prompt.html', current_prompt=current_prompt)
        
    except Exception as e:
        dashboard.logger.error(f"Errore nel caricamento prompt OpenAI: {e}")
        return render_template('openai_prompt.html', current_prompt="Errore nel caricamento del prompt")

@app.route('/api/openai-prompt', methods=['POST'])
def api_update_openai_prompt():
    """API per aggiornare il prompt OpenAI"""
    try:
        new_prompt = request.get_json().get('prompt', '')
        
        if not new_prompt.strip():
            return jsonify({'success': False, 'message': 'Il prompt non può essere vuoto'})
        
        # Backup del file originale
        backup_path = f"src/moderation_rules.py.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        os.system(f"cp src/moderation_rules.py {backup_path}")
        
        # Leggi il file attuale
        with open('src/moderation_rules.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Sostituisci il prompt (implementazione semplificata)
        # In produzione, useresti un parser AST più robusto
        start_marker = 'system_prompt = ('
        end_marker = ')'
        
        start_idx = content.find(start_marker)
        if start_idx != -1:
            # Trova la fine del prompt
            paren_count = 1
            end_idx = start_idx + len(start_marker)
            while end_idx < len(content) and paren_count > 0:
                if content[end_idx] == '(':
                    paren_count += 1
                elif content[end_idx] == ')':
                    paren_count -= 1
                end_idx += 1
            
            if paren_count == 0:
                # Formatta il nuovo prompt
                formatted_prompt = f'(\n            """{new_prompt}"""\n        )'
                
                # Sostituisci nel contenuto
                new_content = content[:start_idx + len(start_marker) - 1] + formatted_prompt + content[end_idx:]
                
                # Scrivi il file aggiornato
                with open('src/moderation_rules.py', 'w', encoding='utf-8') as f:
                    f.write(new_content)
                
                dashboard.logger.info("Prompt OpenAI aggiornato via dashboard")
                return jsonify({
                    'success': True, 
                    'message': 'Prompt aggiornato con successo. Riavvia il bot per applicare le modifiche.',
                    'backup_created': backup_path
                })
            else:
                return jsonify({'success': False, 'message': 'Errore nel parsing del file'})
        else:
            return jsonify({'success': False, 'message': 'Prompt non trovato nel file'})
            
    except Exception as e:
        dashboard.logger.error(f"Errore nell'aggiornamento prompt OpenAI: {e}")
        return jsonify({'success': False, 'message': f'Errore: {str(e)}'})

if __name__ == '__main__':
    # Configurazione per sviluppo
    app.run(host='0.0.0.0', port=5000, debug=False)