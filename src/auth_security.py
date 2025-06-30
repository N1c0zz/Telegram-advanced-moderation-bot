import os
import hashlib
import secrets
import time
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from functools import wraps
import ipaddress

from flask import request, session, redirect, url_for, flash, current_app
from flask_login import UserMixin, LoginManager, login_user, logout_user, login_required, current_user
import bcrypt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

class SecurityConfig:
    """Configurazione di sicurezza centralizzata."""
    
    # Password policy
    MIN_PASSWORD_LENGTH = 12
    REQUIRE_UPPERCASE = True
    REQUIRE_LOWERCASE = True
    REQUIRE_NUMBERS = True
    REQUIRE_SPECIAL_CHARS = True
    SPECIAL_CHARS = "!@#$%^&*()_+-=[]{}|;:,.<>?"
    
    # Session security
    SESSION_TIMEOUT_MINUTES = 30
    ABSOLUTE_SESSION_TIMEOUT_HOURS = 8
    SESSION_REGENERATE_INTERVAL_MINUTES = 15
    
    # Rate limiting
    MAX_LOGIN_ATTEMPTS = 5
    LOCKOUT_DURATION_MINUTES = 15
    RATE_LIMIT_WINDOW_MINUTES = 5
    MAX_REQUESTS_PER_WINDOW = 20
    
    # IP Security
    ENABLE_IP_WHITELIST = False
    ALLOWED_IP_RANGES = []  # Es: ["192.168.1.0/24", "10.0.0.0/8"]
    
    # 2FA
    REQUIRE_2FA = False
    TOTP_ISSUER = "Bot Moderazione Dashboard"
    
    # Security headers
    SECURITY_HEADERS = {
        'X-Frame-Options': 'DENY',
        'X-Content-Type-Options': 'nosniff',
        'X-XSS-Protection': '1; mode=block',
        'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
        'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline' cdn.jsdelivr.net cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline' cdn.jsdelivr.net; font-src 'self' cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'",
        'Referrer-Policy': 'strict-origin-when-cross-origin',
        'Permissions-Policy': 'geolocation=(), microphone=(), camera=()'
    }

class SecurityLogger:
    """Logger specializzato per eventi di sicurezza."""
    
    def __init__(self):
        self.logger = logging.getLogger("SecurityAudit")
        self.logger.setLevel(logging.INFO)
        
        # Handler per file di audit
        if not os.path.exists('logs'):
            os.makedirs('logs')
        
        handler = logging.FileHandler('logs/security_audit.log')
        formatter = logging.Formatter(
            '%(asctime)s - SECURITY - %(levelname)s - %(message)s - '
            'IP: %(ip)s - User: %(user)s - Session: %(session_id)s'
        )
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
    
    def log_event(self, event_type: str, message: str, level: str = "INFO", 
                  user: str = "anonymous", ip: str = None, extra_data: dict = None):
        """Log di eventi di sicurezza con context."""
        
        if ip is None:
            ip = request.remote_addr if request else "unknown"
        
        session_id = session.get('session_id', 'no-session')[:8] if session else 'no-session'
        
        log_data = {
            'event_type': event_type,
            'ip': ip,
            'user': user,
            'session_id': session_id,
            'user_agent': request.headers.get('User-Agent', 'unknown') if request else 'unknown',
            'timestamp': datetime.utcnow().isoformat(),
        }
        
        if extra_data:
            log_data.update(extra_data)
        
        message_with_context = f"{message} | Context: {json.dumps(log_data)}"
        
        if level.upper() == "ERROR":
            self.logger.error(message_with_context, extra={'ip': ip, 'user': user, 'session_id': session_id})
        elif level.upper() == "WARNING":
            self.logger.warning(message_with_context, extra={'ip': ip, 'user': user, 'session_id': session_id})
        else:
            self.logger.info(message_with_context, extra={'ip': ip, 'user': user, 'session_id': session_id})

class PasswordValidator:
    """Validatore robusto per password."""
    
    @staticmethod
    def validate_password(password: str) -> Tuple[bool, List[str]]:
        """Valida una password secondo la policy di sicurezza."""
        errors = []
        
        if len(password) < SecurityConfig.MIN_PASSWORD_LENGTH:
            errors.append(f"Password deve essere almeno {SecurityConfig.MIN_PASSWORD_LENGTH} caratteri")
        
        if SecurityConfig.REQUIRE_UPPERCASE and not any(c.isupper() for c in password):
            errors.append("Password deve contenere almeno una lettera maiuscola")
        
        if SecurityConfig.REQUIRE_LOWERCASE and not any(c.islower() for c in password):
            errors.append("Password deve contenere almeno una lettera minuscola")
        
        if SecurityConfig.REQUIRE_NUMBERS and not any(c.isdigit() for c in password):
            errors.append("Password deve contenere almeno un numero")
        
        if SecurityConfig.REQUIRE_SPECIAL_CHARS and not any(c in SecurityConfig.SPECIAL_CHARS for c in password):
            errors.append(f"Password deve contenere almeno un carattere speciale: {SecurityConfig.SPECIAL_CHARS}")
        
        # Controlli aggiuntivi
        if password.lower() in ['password', 'admin', 'administrator', '123456', 'qwerty']:
            errors.append("Password troppo comune")
        
        return len(errors) == 0, errors
    
    @staticmethod
    def generate_secure_password(length: int = 16) -> str:
        """Genera una password sicura."""
        import string
        
        chars = string.ascii_letters + string.digits + SecurityConfig.SPECIAL_CHARS
        while True:
            password = ''.join(secrets.choice(chars) for _ in range(length))
            is_valid, _ = PasswordValidator.validate_password(password)
            if is_valid:
                return password

class SecureSession:
    """Gestione sicura delle sessioni."""
    
    def __init__(self, app=None):
        self.app = app
        if app:
            self.init_app(app)
    
    def init_app(self, app):
        """Inizializza la gestione sicura delle sessioni."""
        app.config.update({
            'SESSION_COOKIE_SECURE': True,  # Solo HTTPS
            'SESSION_COOKIE_HTTPONLY': True,  # No accesso JS
            'SESSION_COOKIE_SAMESITE': 'Strict',  # CSRF protection
            'PERMANENT_SESSION_LIFETIME': timedelta(minutes=SecurityConfig.SESSION_TIMEOUT_MINUTES),
            'SESSION_COOKIE_NAME': f'dashboard_session_{secrets.token_hex(4)}'  # Nome randomico
        })
        
        @app.before_request
        def validate_session():
            """Valida la sessione ad ogni request."""
            if 'logged_in' in session:
                self._validate_session_security()
    
    def _validate_session_security(self):
        """Valida la sicurezza della sessione corrente."""
        now = datetime.utcnow()
        
        # Timeout di inattività
        if 'last_activity' in session:
            last_activity = datetime.fromisoformat(session['last_activity'])
            if now - last_activity > timedelta(minutes=SecurityConfig.SESSION_TIMEOUT_MINUTES):
                SecurityLogger().log_event("SESSION_TIMEOUT", "Session expired due to inactivity", "INFO")
                self.destroy_session()
                return redirect(url_for('login'))
        
        # Timeout assoluto
        if 'login_time' in session:
            login_time = datetime.fromisoformat(session['login_time'])
            if now - login_time > timedelta(hours=SecurityConfig.ABSOLUTE_SESSION_TIMEOUT_HOURS):
                SecurityLogger().log_event("SESSION_ABSOLUTE_TIMEOUT", "Session expired due to absolute timeout", "INFO")
                self.destroy_session()
                return redirect(url_for('login'))
        
        # Regenera session ID periodicamente
        if 'last_regeneration' in session:
            last_regen = datetime.fromisoformat(session['last_regeneration'])
            if now - last_regen > timedelta(minutes=SecurityConfig.SESSION_REGENERATE_INTERVAL_MINUTES):
                self._regenerate_session_id()
        
        # Aggiorna last activity
        session['last_activity'] = now.isoformat()
    
    def _regenerate_session_id(self):
        """Rigenera l'ID di sessione per prevenire session fixation."""
        old_session_id = session.get('session_id', 'unknown')
        
        # Salva dati importanti
        user_data = {
            'username': session.get('username'),
            'logged_in': session.get('logged_in'),
            'login_time': session.get('login_time'),
            'ip_address': session.get('ip_address'),
            'user_agent_hash': session.get('user_agent_hash')
        }
        
        # Pulisci sessione
        session.clear()
        
        # Ripopola con nuovo ID
        session.update(user_data)
        session['session_id'] = secrets.token_hex(32)
        session['last_regeneration'] = datetime.utcnow().isoformat()
        session['last_activity'] = datetime.utcnow().isoformat()
        
        SecurityLogger().log_event(
            "SESSION_REGENERATED", 
            f"Session ID regenerated: {old_session_id[:8]} -> {session['session_id'][:8]}",
            user=user_data.get('username', 'unknown')
        )
    
    def create_secure_session(self, username: str):
        """Crea una nuova sessione sicura."""
        session.clear()
        
        session.update({
            'logged_in': True,
            'username': username,
            'session_id': secrets.token_hex(32),
            'login_time': datetime.utcnow().isoformat(),
            'last_activity': datetime.utcnow().isoformat(),
            'last_regeneration': datetime.utcnow().isoformat(),
            'ip_address': request.remote_addr,
            'user_agent_hash': hashlib.sha256(
                request.headers.get('User-Agent', '').encode()
            ).hexdigest()[:16]
        })
        
        SecurityLogger().log_event(
            "SESSION_CREATED", 
            f"New secure session created",
            user=username
        )
    
    def destroy_session(self):
        """Distrugge la sessione in modo sicuro."""
        username = session.get('username', 'unknown')
        session_id = session.get('session_id', 'unknown')[:8]
        
        session.clear()
        
        SecurityLogger().log_event(
            "SESSION_DESTROYED", 
            f"Session destroyed: {session_id}",
            user=username
        )

class RateLimiter:
    """Rate limiter per prevenire attacchi brute force."""
    
    def __init__(self):
        self.attempts = {}  # IP -> [(timestamp, attempt_type), ...]
        self.locked_ips = {}  # IP -> lock_until_timestamp
    
    def is_rate_limited(self, ip: str, attempt_type: str = "login") -> Tuple[bool, int]:
        """Controlla se un IP è rate limited."""
        now = time.time()
        
        # Controlla se IP è lockato
        if ip in self.locked_ips:
            if now < self.locked_ips[ip]:
                remaining = int(self.locked_ips[ip] - now)
                return True, remaining
            else:
                del self.locked_ips[ip]
        
        # Pulisci tentativi vecchi
        if ip in self.attempts:
            self.attempts[ip] = [
                (ts, at) for ts, at in self.attempts[ip]
                if now - ts < SecurityConfig.RATE_LIMIT_WINDOW_MINUTES * 60
            ]
        
        # Conta tentativi recenti
        recent_attempts = len(self.attempts.get(ip, []))
        
        if recent_attempts >= SecurityConfig.MAX_LOGIN_ATTEMPTS:
            # Locka l'IP
            self.locked_ips[ip] = now + (SecurityConfig.LOCKOUT_DURATION_MINUTES * 60)
            
            SecurityLogger().log_event(
                "IP_LOCKED", 
                f"IP locked due to {recent_attempts} failed attempts",
                "WARNING",
                ip=ip,
                extra_data={"attempts": recent_attempts, "lockout_duration": SecurityConfig.LOCKOUT_DURATION_MINUTES}
            )
            
            return True, SecurityConfig.LOCKOUT_DURATION_MINUTES * 60
        
        return False, 0
    
    def record_attempt(self, ip: str, attempt_type: str = "login", success: bool = False):
        """Registra un tentativo."""
        now = time.time()
        
        if ip not in self.attempts:
            self.attempts[ip] = []
        
        if not success:
            self.attempts[ip].append((now, attempt_type))
        else:
            # Reset tentativi su login riuscito
            if ip in self.attempts:
                del self.attempts[ip]
            if ip in self.locked_ips:
                del self.locked_ips[ip]

class SecureUser(UserMixin):
    """Classe User con funzionalità di sicurezza avanzate."""
    
    def __init__(self, username: str, password_hash: str = None, 
                 is_admin: bool = True, created_at: str = None,
                 last_login: str = None, failed_attempts: int = 0,
                 is_locked: bool = False, two_fa_secret: str = None):
        self.id = username
        self.username = username
        self.password_hash = password_hash
        self.is_admin = is_admin
        self.created_at = created_at or datetime.utcnow().isoformat()
        self.last_login = last_login
        self.failed_attempts = failed_attempts
        self.is_locked = is_locked
        self.two_fa_secret = two_fa_secret
    
    def verify_password(self, password: str) -> bool:
        """Verifica la password con bcrypt."""
        if not self.password_hash:
            return False
        
        try:
            return bcrypt.checkpw(password.encode('utf-8'), self.password_hash.encode('utf-8'))
        except Exception:
            return False
    
    def set_password(self, password: str):
        """Imposta una nuova password con bcrypt."""
        is_valid, errors = PasswordValidator.validate_password(password)
        if not is_valid:
            raise ValueError(f"Password non valida: {', '.join(errors)}")
        
        salt = bcrypt.gensalt(rounds=12)  # Costo computazionale alto
        self.password_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    
    def to_dict(self) -> dict:
        """Converte l'utente in dizionario per storage."""
        return {
            'username': self.username,
            'password_hash': self.password_hash,
            'is_admin': self.is_admin,
            'created_at': self.created_at,
            'last_login': self.last_login,
            'failed_attempts': self.failed_attempts,
            'is_locked': self.is_locked,
            'two_fa_secret': self.two_fa_secret
        }
    
    @classmethod
    def from_dict(cls, data: dict):
        """Crea un utente da dizionario."""
        return cls(**data)

class SecureUserStore:
    """Storage sicuro per utenti con crittografia."""
    
    def __init__(self, storage_path: str = "config/users_secure.enc"):
        self.storage_path = storage_path
        self.encryption_key = self._get_or_create_key()
        self.cipher = Fernet(self.encryption_key)
        
        # Assicurati che la directory esista
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
    
    def _get_or_create_key(self) -> bytes:
        """Ottiene o crea la chiave di crittografia."""
        key_path = "config/storage.key"
        
        if os.path.exists(key_path):
            with open(key_path, 'rb') as f:
                return f.read()
        else:
            # Genera nuova chiave
            key = Fernet.generate_key()
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            
            with open(key_path, 'wb') as f:
                f.write(key)
            
            # Proteggi il file
            os.chmod(key_path, 0o600)
            return key
    
    def save_users(self, users: Dict[str, SecureUser]):
        """Salva gli utenti in modo crittografato."""
        data = {username: user.to_dict() for username, user in users.items()}
        
        json_data = json.dumps(data, indent=2)
        encrypted_data = self.cipher.encrypt(json_data.encode())
        
        with open(self.storage_path, 'wb') as f:
            f.write(encrypted_data)
        
        # Proteggi il file
        os.chmod(self.storage_path, 0o600)
    
    def load_users(self) -> Dict[str, SecureUser]:
        """Carica gli utenti decrittando il file."""
        if not os.path.exists(self.storage_path):
            return {}
        
        try:
            with open(self.storage_path, 'rb') as f:
                encrypted_data = f.read()
            
            decrypted_data = self.cipher.decrypt(encrypted_data)
            data = json.loads(decrypted_data.decode())
            
            return {username: SecureUser.from_dict(user_data) for username, user_data in data.items()}
        except Exception as e:
            SecurityLogger().log_event(
                "USER_STORE_ERROR", 
                f"Failed to load users: {str(e)}", 
                "ERROR"
            )
            return {}

class SecureAuthManager:
    """Gestore di autenticazione robusto e sicuro."""
    
    def __init__(self, app=None):
        self.login_manager = LoginManager()
        self.user_store = SecureUserStore()
        self.rate_limiter = RateLimiter()
        self.secure_session = SecureSession()
        self.security_logger = SecurityLogger()
        self.users = self.user_store.load_users()
        
        if app:
            self.init_app(app)
    
    def init_app(self, app):
        """Inizializza l'autenticazione con Flask app."""
        
        # Configurazione sicura
        app.config.update({
            'SECRET_KEY': os.getenv('DASHBOARD_SECRET_KEY', secrets.token_hex(32)),
            'WTF_CSRF_ENABLED': True,
            'WTF_CSRF_TIME_LIMIT': 3600,
        })
        
        self.login_manager.init_app(app)
        self.secure_session.init_app(app)
        
        self.login_manager.login_view = 'login'
        self.login_manager.login_message = 'Accesso richiesto per questa risorsa.'
        self.login_manager.login_message_category = 'warning'
        
        @self.login_manager.user_loader
        def load_user(username):
            return self.users.get(username)
        
        # Security headers
        @app.after_request
        def add_security_headers(response):
            for header, value in SecurityConfig.SECURITY_HEADERS.items():
                response.headers[header] = value
            return response
        
        # Controllo IP se abilitato
        if SecurityConfig.ENABLE_IP_WHITELIST and SecurityConfig.ALLOWED_IP_RANGES:
            @app.before_request
            def check_ip_whitelist():
                client_ip = ipaddress.ip_address(request.remote_addr)
                allowed = False
                
                for ip_range in SecurityConfig.ALLOWED_IP_RANGES:
                    if client_ip in ipaddress.ip_network(ip_range):
                        allowed = True
                        break
                
                if not allowed:
                    self.security_logger.log_event(
                        "IP_BLOCKED", 
                        f"Access denied for IP not in whitelist", 
                        "WARNING"
                    )
                    return "Access Denied", 403
        
        # Inizializza utenti default se non esistono
        self._initialize_default_users()
    
    def _initialize_default_users(self):
        """Inizializza utenti default se il sistema è vuoto."""
        if not self.users:
            admin_username = os.getenv('DASHBOARD_USERNAME', 'admin')
            admin_password = os.getenv('DASHBOARD_PASSWORD')
            
            if not admin_password:
                # Genera password sicura se non fornita
                admin_password = PasswordValidator.generate_secure_password()
                print(f"\n{'='*60}")
                print(f"🔐 PASSWORD ADMIN GENERATA AUTOMATICAMENTE:")
                print(f"Username: {admin_username}")
                print(f"Password: {admin_password}")
                print(f"⚠️  SALVA QUESTA PASSWORD! Non verrà mostrata di nuovo.")
                print(f"{'='*60}\n")
            
            admin_user = SecureUser(admin_username)
            admin_user.set_password(admin_password)
            
            self.users[admin_username] = admin_user
            self.user_store.save_users(self.users)
            
            self.security_logger.log_event(
                "INITIAL_SETUP", 
                "Default admin user created",
                user=admin_username
            )
    
    def authenticate(self, username: str, password: str, ip: str) -> Tuple[bool, str, SecureUser]:
        """Autentica un utente con controlli di sicurezza."""
        
        # Rate limiting
        is_limited, remaining = self.rate_limiter.is_rate_limited(ip)
        if is_limited:
            self.security_logger.log_event(
                "RATE_LIMITED", 
                f"Login attempt blocked - rate limited ({remaining}s remaining)",
                "WARNING",
                user=username,
                ip=ip
            )
            return False, f"Troppi tentativi. Riprova tra {remaining // 60 + 1} minuti.", None
        
        # Trova utente
        user = self.users.get(username)
        if not user:
            self.rate_limiter.record_attempt(ip, "login", False)
            self.security_logger.log_event(
                "LOGIN_FAILED", 
                "Login failed - user not found",
                "WARNING",
                user=username,
                ip=ip
            )
            return False, "Credenziali non valide.", None
        
        # Controlla se utente è bloccato
        if user.is_locked:
            self.security_logger.log_event(
                "LOGIN_BLOCKED", 
                "Login attempt on locked account",
                "WARNING",
                user=username,
                ip=ip
            )
            return False, "Account bloccato. Contatta l'amministratore.", None
        
        # Verifica password
        if not user.verify_password(password):
            user.failed_attempts += 1
            
            # Blocca dopo troppi tentativi
            if user.failed_attempts >= SecurityConfig.MAX_LOGIN_ATTEMPTS:
                user.is_locked = True
                self.security_logger.log_event(
                    "ACCOUNT_LOCKED", 
                    f"Account locked after {user.failed_attempts} failed attempts",
                    "WARNING",
                    user=username,
                    ip=ip
                )
            
            self.user_store.save_users(self.users)
            self.rate_limiter.record_attempt(ip, "login", False)
            
            self.security_logger.log_event(
                "LOGIN_FAILED", 
                f"Login failed - invalid password (attempt {user.failed_attempts})",
                "WARNING",
                user=username,
                ip=ip
            )
            
            return False, "Credenziali non valide.", None
        
        # Login riuscito
        user.failed_attempts = 0
        user.last_login = datetime.utcnow().isoformat()
        self.user_store.save_users(self.users)
        
        self.rate_limiter.record_attempt(ip, "login", True)
        
        self.security_logger.log_event(
            "LOGIN_SUCCESS", 
            "Successful login",
            "INFO",
            user=username,
            ip=ip
        )
        
        return True, "Login effettuato con successo.", user
    
    def create_user(self, username: str, password: str, is_admin: bool = False) -> Tuple[bool, str]:
        """Crea un nuovo utente."""
        if username in self.users:
            return False, "Utente già esistente."
        
        try:
            user = SecureUser(username, is_admin=is_admin)
            user.set_password(password)
            
            self.users[username] = user
            self.user_store.save_users(self.users)
            
            self.security_logger.log_event(
                "USER_CREATED", 
                f"New user created (admin: {is_admin})",
                user=username
            )
            
            return True, "Utente creato con successo."
        except ValueError as e:
            return False, str(e)
    
    def change_password(self, username: str, old_password: str, new_password: str) -> Tuple[bool, str]:
        """Cambia la password di un utente."""
        user = self.users.get(username)
        if not user:
            return False, "Utente non trovato."
        
        if not user.verify_password(old_password):
            self.security_logger.log_event(
                "PASSWORD_CHANGE_FAILED", 
                "Password change failed - invalid old password",
                "WARNING",
                user=username
            )
            return False, "Password attuale non corretta."
        
        try:
            user.set_password(new_password)
            self.user_store.save_users(self.users)
            
            self.security_logger.log_event(
                "PASSWORD_CHANGED", 
                "Password changed successfully",
                user=username
            )
            
            return True, "Password cambiata con successo."
        except ValueError as e:
            return False, str(e)
    
    def unlock_user(self, username: str) -> Tuple[bool, str]:
        """Sblocca un utente."""
        user = self.users.get(username)
        if not user:
            return False, "Utente non trovato."
        
        user.is_locked = False
        user.failed_attempts = 0
        self.user_store.save_users(self.users)
        
        self.security_logger.log_event(
            "USER_UNLOCKED", 
            "User unlocked by admin",
            user=username
        )
        
        return True, "Utente sbloccato."
    
    def get_security_status(self) -> dict:
        """Restituisce lo stato di sicurezza del sistema."""
        return {
            'total_users': len(self.users),
            'locked_users': sum(1 for user in self.users.values() if user.is_locked),
            'active_rate_limits': len(self.rate_limiter.locked_ips),
            'password_policy': {
                'min_length': SecurityConfig.MIN_PASSWORD_LENGTH,
                'require_uppercase': SecurityConfig.REQUIRE_UPPERCASE,
                'require_lowercase': SecurityConfig.REQUIRE_LOWERCASE,
                'require_numbers': SecurityConfig.REQUIRE_NUMBERS,
                'require_special': SecurityConfig.REQUIRE_SPECIAL_CHARS,
            },
            'session_config': {
                'timeout_minutes': SecurityConfig.SESSION_TIMEOUT_MINUTES,
                'absolute_timeout_hours': SecurityConfig.ABSOLUTE_SESSION_TIMEOUT_HOURS,
            },
            'security_features': {
                'rate_limiting': True,
                'password_hashing': 'bcrypt',
                'session_encryption': True,
                'audit_logging': True,
                'ip_whitelist': SecurityConfig.ENABLE_IP_WHITELIST,
                'csrf_protection': True,
                'security_headers': True,
            }
        }

# Decorator per controlli di sicurezza avanzati
def enhanced_auth_required(admin_only: bool = False):
    """Decorator per autenticazione con controlli di sicurezza avanzati."""
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated_function(*args, **kwargs):
            # Verifica sessione sicura
            if not session.get('logged_in'):
                SecurityLogger().log_event(
                    "UNAUTHORIZED_ACCESS", 
                    f"Access attempt without valid session to {request.endpoint}",
                    "WARNING"
                )
                logout_user()
                return redirect(url_for('login'))
            
            # Verifica admin se richiesto
            if admin_only and not getattr(current_user, 'is_admin', False):
                SecurityLogger().log_event(
                    "INSUFFICIENT_PRIVILEGES", 
                    f"Non-admin user attempted to access admin resource {request.endpoint}",
                    "WARNING",
                    user=current_user.username if current_user.is_authenticated else 'unknown'
                )
                flash('Accesso negato. Privilegi insufficienti.', 'danger')
                return redirect(url_for('index'))
            
            # Verifica consistenza sessione (anti session hijacking)
            stored_ip = session.get('ip_address')
            stored_ua_hash = session.get('user_agent_hash')
            current_ua_hash = hashlib.sha256(
                request.headers.get('User-Agent', '').encode()
            ).hexdigest()[:16]
            
            if stored_ip != request.remote_addr or stored_ua_hash != current_ua_hash:
                SecurityLogger().log_event(
                    "SESSION_HIJACK_ATTEMPT", 
                    f"Session mismatch detected - IP or UA changed",
                    "ERROR",
                    user=current_user.username if current_user.is_authenticated else 'unknown',
                    extra_data={
                        'stored_ip': stored_ip,
                        'current_ip': request.remote_addr,
                        'ua_changed': stored_ua_hash != current_ua_hash
                    }
                )
                logout_user()
                session.clear()
                flash('Sessione invalidata per motivi di sicurezza.', 'warning')
                return redirect(url_for('login'))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# Middleware per sicurezza aggiuntiva
class SecurityMiddleware:
    """Middleware per controlli di sicurezza aggiuntivi."""
    
    def __init__(self, app, auth_manager):
        self.app = app
        self.auth_manager = auth_manager
        
        @app.before_request
        def security_checks():
            # Skip per endpoint di login
            if request.endpoint in ['login', 'static']:
                return
            
            # Rate limiting generale
            ip = request.remote_addr
            is_limited, remaining = self.auth_manager.rate_limiter.is_rate_limited(
                ip, "general_request"
            )
            
            if is_limited:
                SecurityLogger().log_event(
                    "GENERAL_RATE_LIMITED", 
                    f"Request rate limited ({remaining}s remaining)",
                    "WARNING",
                    ip=ip
                )
                return "Rate limit exceeded. Please try again later.", 429
            
            # Registra richiesta generale (non login)
            self.auth_manager.rate_limiter.record_attempt(ip, "general_request", True)
            
            # Controllo Content-Type per POST
            if request.method == 'POST' and request.content_type:
                if not request.content_type.startswith(('application/json', 'application/x-www-form-urlencoded', 'multipart/form-data')):
                    SecurityLogger().log_event(
                        "SUSPICIOUS_CONTENT_TYPE", 
                        f"Unusual content type: {request.content_type}",
                        "WARNING"
                    )
                    return "Invalid content type", 400