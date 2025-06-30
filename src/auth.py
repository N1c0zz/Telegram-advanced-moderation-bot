import os
import hashlib
from flask_login import UserMixin, LoginManager, login_user, logout_user, login_required, current_user
from functools import wraps
from flask import session, request, redirect, url_for, flash, current_app

# Import del nuovo sistema sicuro
from .auth_security import (
    SecureAuthManager, 
    SecureUser, 
    SecurityLogger, 
    SecurityConfig,
    SecureSession,
    enhanced_auth_required,
    SecurityMiddleware,
    PasswordValidator
)

class AuthManager:
    """
    Wrapper per compatibilità con il sistema esistente.
    Usa internamente il nuovo SecureAuthManager.
    """
    
    def __init__(self, app=None):
        self.secure_auth = SecureAuthManager()
        self.login_manager = self.secure_auth.login_manager
        self.security_logger = SecurityLogger()
        
        if app:
            self.init_app(app)
    
    def init_app(self, app, config_data=None):
        """Inizializza l'autenticazione sicura."""
        
        # Configurazione sicurezza da environment o config
        if config_data and 'security' in config_data:
            security_config = config_data['security']
            
            # Aggiorna configurazione sicurezza
            SecurityConfig.SESSION_TIMEOUT_MINUTES = security_config.get('session_timeout_minutes', 30)
            SecurityConfig.MAX_LOGIN_ATTEMPTS = security_config.get('max_login_attempts', 5)
            SecurityConfig.ENABLE_IP_WHITELIST = security_config.get('enable_ip_whitelist', False)
            SecurityConfig.ALLOWED_IP_RANGES = security_config.get('allowed_ip_ranges', [])
            SecurityConfig.REQUIRE_2FA = security_config.get('require_2fa', False)
        
        # Inizializza sistema sicuro
        self.secure_auth.init_app(app)
        
        # Aggiungi middleware di sicurezza
        self.security_middleware = SecurityMiddleware(app, self.secure_auth)
        
        self.security_logger.log_event(
            "AUTH_SYSTEM_INITIALIZED", 
            "Secure authentication system initialized"
        )
    
    def verify_password(self, username, password):
        """Verifica password (compatibilità)."""
        user = self.secure_auth.users.get(username)
        return user and user.verify_password(password)
    
    def authenticate_user(self, username, password, remember=False):
        """Autentica utente con nuovo sistema sicuro."""
        ip = request.remote_addr
        
        success, message, user = self.secure_auth.authenticate(username, password, ip)
        
        if success and user:
            # Crea sessione sicura
            self.secure_auth.secure_session.create_secure_session(username)
            
            # Login con Flask-Login
            login_user(user, remember=remember)
            
            return True
        
        return False
    
    def add_user(self, username, password, is_admin=True):
        """Aggiunge un nuovo utente."""
        return self.secure_auth.create_user(username, password, is_admin)
    
    def remove_user(self, username):
        """Rimuove un utente."""
        if username in self.secure_auth.users:
            del self.secure_auth.users[username]
            self.secure_auth.user_store.save_users(self.secure_auth.users)
            
            self.security_logger.log_event(
                "USER_REMOVED", 
                f"User removed",
                user=username
            )
            return True
        return False
    
    def get_all_users(self):
        """Restituisce lista di tutti gli username."""
        return list(self.secure_auth.users.keys())
    
    def unlock_user(self, username):
        """Sblocca un utente."""
        return self.secure_auth.unlock_user(username)
    
    def change_password(self, username, old_password, new_password):
        """Cambia password utente."""
        return self.secure_auth.change_password(username, old_password, new_password)
    
    def get_security_status(self):
        """Ottieni stato di sicurezza."""
        return self.secure_auth.get_security_status()
    
    def get_user_info(self, username):
        """Ottieni informazioni utente."""
        user = self.secure_auth.users.get(username)
        if user:
            return {
                'username': user.username,
                'is_admin': user.is_admin,
                'created_at': user.created_at,
                'last_login': user.last_login,
                'failed_attempts': user.failed_attempts,
                'is_locked': user.is_locked,
                'has_2fa': bool(user.two_fa_secret)
            }
        return None

# Decorator per compatibilità (usa il nuovo sistema)
def auth_required(f):
    """Decorator per autenticazione semplice (compatibilità)."""
    return enhanced_auth_required(admin_only=False)(f)

def admin_required(f):
    """Decorator per autenticazione admin."""
    return enhanced_auth_required(admin_only=True)(f)

# Funzioni helper per compatibilità
def is_authenticated():
    """Restituisce True se l'utente corrente è autenticato."""
    return current_user.is_authenticated and session.get('logged_in', False)

def get_current_username():
    """Restituisce l'username dell'utente corrente."""
    if current_user.is_authenticated:
        return current_user.username
    return None

def force_logout():
    """Forza il logout dell'utente corrente."""
    if current_user.is_authenticated:
        SecurityLogger().log_event(
            "FORCED_LOGOUT", 
            "User forcefully logged out",
            user=current_user.username
        )
    
    logout_user()
    session.clear()

# Classe User per compatibilità
class User(SecureUser):
    """Classe User compatibile con il sistema esistente."""
    
    def get_id(self):
        return self.username