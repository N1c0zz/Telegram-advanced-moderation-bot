import os
import hashlib
from flask_login import UserMixin, LoginManager, login_user, logout_user, login_required, current_user
from functools import wraps
from flask import session, request, redirect, url_for, flash

class User(UserMixin):
    """Classe User per Flask-Login."""
    
    def __init__(self, username):
        self.id = username
        self.username = username
    
    def get_id(self):
        return self.username

class AuthManager:
    """Gestore dell'autenticazione semplice."""
    
    def __init__(self, app=None):
        self.login_manager = LoginManager()
        self.users = {}  # In memoria - per una versione più sicura usa un database
        
        if app:
            self.init_app(app)
    
    def init_app(self, app, config_data=None):
        """Inizializza l'autenticazione con Flask app."""
        self.login_manager.init_app(app)
        self.login_manager.login_view = 'login'
        self.login_manager.login_message = 'Per favore accedi per accedere a questa pagina.'
        self.login_manager.login_message_category = 'info'
        
        # User loader callback
        @self.login_manager.user_loader
        def load_user(username):
            if username in self.users:
                return User(username)
            return None
        
        # Carica utenti da config.json e/o variabili d'ambiente
        self._load_users_from_config(config_data)
    
    def _load_users_from_config(self, config_data=None):
        """Carica utenti da config.json e variabili d'ambiente."""
        
        # PRIORITÀ 1: Carica da config.json se disponibile
        if config_data and 'dashboard' in config_data:
            dashboard_config = config_data['dashboard']
            
            # Controlla se l'autenticazione è abilitata
            require_auth = dashboard_config.get('require_auth', True)
            if not require_auth:
                # Se auth disabilitato, crea un utente guest
                self.users['guest'] = self._hash_password('guest')
                return
            
            # Carica admin_users dal config (come username/password identici)
            admin_users = dashboard_config.get('admin_users', [])
            for user_id in admin_users:
                username = f"admin_{user_id}"
                # Password = user_id convertito in stringa (puoi cambiare questa logica)
                password = str(user_id)
                self.users[username] = self._hash_password(password)
        
        # PRIORITÀ 2: Carica da .env (override/aggiuntivi)
        admin_user = os.getenv('DASHBOARD_USERNAME')
        admin_pass = os.getenv('DASHBOARD_PASSWORD')
        
        if admin_user and admin_pass:
            self.users[admin_user] = self._hash_password(admin_pass)
        
        # PRIORITÀ 3: Utenti aggiuntivi da .env
        additional_users = os.getenv('DASHBOARD_ADDITIONAL_USERS', '')
        if additional_users:
            for user_pair in additional_users.split(','):
                if ':' in user_pair:
                    username, password = user_pair.strip().split(':', 1)
                    self.users[username.strip()] = self._hash_password(password.strip())
        
        # FALLBACK: Se nessun utente configurato, crea admin di default
        if not self.users:
            self.users['admin'] = self._hash_password('admin123')
    
    def _hash_password(self, password):
        """Hash sicuro della password."""
        # PRIORITÀ 1: Salt da config.json se disponibile
        salt = getattr(self, '_config_salt', None)
        if not salt:
            # PRIORITÀ 2: Salt da .env
            salt = os.getenv('DASHBOARD_SALT', 'default_salt_change_me')
        
        return hashlib.sha256((password + salt).encode()).hexdigest()
    
    def set_config_salt(self, salt):
        """Imposta il salt dalla configurazione."""
        self._config_salt = salt
    
    def verify_password(self, username, password):
        """Verifica username e password."""
        if username not in self.users:
            return False
        
        hashed_input = self._hash_password(password)
        return self.users[username] == hashed_input
    
    def authenticate_user(self, username, password, remember=False):
        """Autentica un utente e crea la sessione."""
        if self.verify_password(username, password):
            user = User(username)
            login_user(user, remember=remember)
            return True
        return False
    
    def add_user(self, username, password):
        """Aggiunge un nuovo utente (per uso futuro)."""
        self.users[username] = self._hash_password(password)
    
    def remove_user(self, username):
        """Rimuove un utente."""
        if username in self.users:
            del self.users[username]
            return True
        return False
    
    def get_all_users(self):
        """Restituisce lista di tutti gli username."""
        return list(self.users.keys())

# Decorator per proteggere route che richiedono autenticazione
def auth_required(f):
    """Decorator che richiede autenticazione per accedere alla route."""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated_function

# Funzione helper per check rapidi
def is_authenticated():
    """Restituisce True se l'utente corrente è autenticato."""
    return current_user.is_authenticated

def get_current_username():
    """Restituisce l'username dell'utente corrente."""
    return current_user.username if current_user.is_authenticated else None