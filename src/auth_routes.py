from flask import render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import login_user, logout_user, current_user
from .auth import User, force_logout, admin_required
from .auth_security import SecurityLogger, PasswordValidator, SecurityConfig
import secrets
import time

def setup_auth_routes(app, auth_manager):
    """Configura le route di autenticazione sicure."""
    
    security_logger = SecurityLogger()
    
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """Pagina di login con sicurezza avanzata."""
        
        # Se già loggato, redirect alla dashboard
        if current_user.is_authenticated and session.get('logged_in'):
            return redirect(url_for('index'))
        
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            remember = bool(request.form.get('remember'))
            
            # Validazione input
            if not username or not password:
                flash('Username e password sono obbligatori', 'danger')
                security_logger.log_event(
                    "LOGIN_VALIDATION_FAILED", 
                    "Empty username or password",
                    "WARNING"
                )
                return render_template('login_secure.html')
            
            # Tentativo di autenticazione
            if auth_manager.authenticate_user(username, password, remember):
                flash(f'Bentornato, {username}!', 'success')
                
                # Redirect alla pagina richiesta o alla homepage
                next_page = request.args.get('next')
                if next_page and next_page.startswith('/'):  # Sicurezza: solo URL relativi
                    return redirect(next_page)
                return redirect(url_for('index'))
            else:
                flash('Credenziali non valide o account bloccato', 'danger')
        
        return render_template('login_secure.html')
    
    @app.route('/logout')
    def logout():
        """Logout sicuro dell'utente."""
        username = current_user.username if current_user.is_authenticated else 'unknown'
        
        if current_user.is_authenticated:
            security_logger.log_event(
                "LOGOUT", 
                "User logged out",
                user=username
            )
            flash(f'Arrivederci, {username}!', 'info')
        
        # Logout sicuro
        force_logout()
        
        return redirect(url_for('login'))
    
    @app.route('/auth/status')
    def auth_status():
        """API per controllare stato autenticazione."""
        return jsonify({
            'authenticated': current_user.is_authenticated and session.get('logged_in', False),
            'username': current_user.username if current_user.is_authenticated else None,
            'is_admin': getattr(current_user, 'is_admin', False) if current_user.is_authenticated else False,
            'session_timeout': SecurityConfig.SESSION_TIMEOUT_MINUTES,
            'last_activity': session.get('last_activity')
        })
    
    @app.route('/auth/security-status')
    @admin_required
    def security_status():
        """API per stato di sicurezza (solo admin)."""
        return jsonify(auth_manager.get_security_status())
    
    @app.route('/auth/change-password', methods=['GET', 'POST'])
    @admin_required
    def change_password():
        """Cambio password utente."""
        if request.method == 'POST':
            data = request.get_json() if request.is_json else request.form
            
            current_password = data.get('current_password', '')
            new_password = data.get('new_password', '')
            confirm_password = data.get('confirm_password', '')
            
            # Validazioni
            if not all([current_password, new_password, confirm_password]):
                return jsonify({
                    'success': False, 
                    'message': 'Tutti i campi sono obbligatori'
                }), 400
            
            if new_password != confirm_password:
                return jsonify({
                    'success': False, 
                    'message': 'Le nuove password non corrispondono'
                }), 400
            
            # Valida nuova password
            is_valid, errors = PasswordValidator.validate_password(new_password)
            if not is_valid:
                return jsonify({
                    'success': False, 
                    'message': 'Password non valida',
                    'errors': errors
                }), 400
            
            # Cambia password
            success, message = auth_manager.change_password(
                current_user.username, 
                current_password, 
                new_password
            )
            
            if success:
                return jsonify({'success': True, 'message': message})
            else:
                return jsonify({'success': False, 'message': message}), 400
        
        return render_template('change_password.html')
    
    @app.route('/auth/users')
    @admin_required
    def list_users():
        """Lista utenti (solo admin)."""
        users_info = []
        for username in auth_manager.get_all_users():
            user_info = auth_manager.get_user_info(username)
            if user_info:
                users_info.append(user_info)
        
        return jsonify({
            'users': users_info,
            'total': len(users_info)
        })
    
    @app.route('/auth/users/<username>/unlock', methods=['POST'])
    @admin_required
    def unlock_user(username):
        """Sblocca un utente (solo admin)."""
        if username == current_user.username:
            return jsonify({
                'success': False, 
                'message': 'Non puoi sbloccare te stesso'
            }), 400
        
        success, message = auth_manager.unlock_user(username)
        
        if success:
            security_logger.log_event(
                "USER_UNLOCKED_BY_ADMIN", 
                f"User unlocked by admin",
                user=username,
                extra_data={'admin': current_user.username}
            )
        
        return jsonify({
            'success': success, 
            'message': message
        })
    
    @app.route('/auth/create-user', methods=['POST'])
    @admin_required
    def create_user():
        """Crea nuovo utente (solo admin)."""
        data = request.get_json() if request.is_json else request.form
        
        username = data.get('username', '').strip()
        password = data.get('password', '')
        is_admin = bool(data.get('is_admin', False))
        
        if not username or not password:
            return jsonify({
                'success': False, 
                'message': 'Username e password sono obbligatori'
            }), 400
        
        # Valida password
        is_valid, errors = PasswordValidator.validate_password(password)
        if not is_valid:
            return jsonify({
                'success': False, 
                'message': 'Password non valida',
                'errors': errors
            }), 400
        
        success, message = auth_manager.add_user(username, password, is_admin)
        
        if success:
            security_logger.log_event(
                "USER_CREATED_BY_ADMIN", 
                f"New user created by admin (admin: {is_admin})",
                user=username,
                extra_data={'creator': current_user.username}
            )
        
        return jsonify({
            'success': success, 
            'message': message
        })
    
    @app.route('/auth/delete-user/<username>', methods=['DELETE'])
    @admin_required
    def delete_user(username):
        """Elimina utente (solo admin)."""
        if username == current_user.username:
            return jsonify({
                'success': False, 
                'message': 'Non puoi eliminare te stesso'
            }), 400
        
        success = auth_manager.remove_user(username)
        
        if success:
            security_logger.log_event(
                "USER_DELETED_BY_ADMIN", 
                f"User deleted by admin",
                user=username,
                extra_data={'admin': current_user.username}
            )
            return jsonify({
                'success': True, 
                'message': f'Utente {username} eliminato'
            })
        else:
            return jsonify({
                'success': False, 
                'message': 'Utente non trovato'
            }), 404
    
    @app.route('/auth/generate-password')
    @admin_required
    def generate_password():
        """Genera password sicura (solo admin)."""
        length = request.args.get('length', 16, type=int)
        length = max(12, min(32, length))  # Limita tra 12 e 32 caratteri
        
        password = PasswordValidator.generate_secure_password(length)
        
        return jsonify({
            'password': password,
            'length': len(password),
            'strength': 'strong'
        })
    
    @app.route('/auth/validate-password', methods=['POST'])
    @admin_required
    def validate_password():
        """Valida una password (solo admin)."""
        data = request.get_json()
        password = data.get('password', '')
        
        is_valid, errors = PasswordValidator.validate_password(password)
        
        return jsonify({
            'valid': is_valid,
            'errors': errors,
            'requirements': {
                'min_length': SecurityConfig.MIN_PASSWORD_LENGTH,
                'require_uppercase': SecurityConfig.REQUIRE_UPPERCASE,
                'require_lowercase': SecurityConfig.REQUIRE_LOWERCASE,
                'require_numbers': SecurityConfig.REQUIRE_NUMBERS,
                'require_special': SecurityConfig.REQUIRE_SPECIAL_CHARS
            }
        })
    
    @app.route('/auth/session-extend', methods=['POST'])
    def extend_session():
        """Estende la sessione corrente."""
        if current_user.is_authenticated and session.get('logged_in'):
            # Aggiorna last_activity
            session['last_activity'] = time.time()
            
            return jsonify({
                'success': True,
                'message': 'Sessione estesa',
                'timeout_minutes': SecurityConfig.SESSION_TIMEOUT_MINUTES
            })
        
        return jsonify({
            'success': False,
            'message': 'Sessione non valida'
        }), 401
    
    # Route per la pagina di gestione sicurezza
    @app.route('/security-management')
    @admin_required
    def security_management():
        """Pagina di gestione sicurezza (solo admin)."""
        security_status = auth_manager.get_security_status()
        
        return render_template('security_management.html', 
                             security_status=security_status)
    
    # Handler per errori di autenticazione
    @app.errorhandler(401)
    def unauthorized(error):
        """Handler per errori di autenticazione."""
        if request.is_json:
            return jsonify({
                'error': 'Unauthorized',
                'message': 'Autenticazione richiesta'
            }), 401
        
        flash('Accesso non autorizzato. Effettua il login.', 'warning')
        return redirect(url_for('login'))
    
    @app.errorhandler(403)
    def forbidden(error):
        """Handler per accesso negato."""
        if request.is_json:
            return jsonify({
                'error': 'Forbidden',
                'message': 'Privilegi insufficienti'
            }), 403
        
        flash('Accesso negato. Privilegi insufficienti.', 'danger')
        return redirect(url_for('index'))
    
    # API di supporto per frontend
    @app.route('/auth/password-policy')
    def password_policy():
        """Restituisce la policy delle password."""
        return jsonify({
            'min_length': SecurityConfig.MIN_PASSWORD_LENGTH,
            'require_uppercase': SecurityConfig.REQUIRE_UPPERCASE,
            'require_lowercase': SecurityConfig.REQUIRE_LOWERCASE,
            'require_numbers': SecurityConfig.REQUIRE_NUMBERS,
            'require_special_chars': SecurityConfig.REQUIRE_SPECIAL_CHARS,
            'special_chars': SecurityConfig.SPECIAL_CHARS
        })
    
    @app.route('/auth/security-info')
    def security_info():
        """Informazioni di sicurezza pubbliche."""
        return jsonify({
            'session_timeout_minutes': SecurityConfig.SESSION_TIMEOUT_MINUTES,
            'max_login_attempts': SecurityConfig.MAX_LOGIN_ATTEMPTS,
            'lockout_duration_minutes': SecurityConfig.LOCKOUT_DURATION_MINUTES,
            'security_features': [
                'Rate Limiting',
                'Session Security', 
                'Password Policy',
                'Audit Logging',
                'CSRF Protection',
                'Security Headers'
            ]
        })