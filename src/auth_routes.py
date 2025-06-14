from flask import render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, current_user
from .auth import User

def setup_auth_routes(app, auth_manager):
    """Configura le route di autenticazione."""
    
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """Pagina di login."""
        # Se già loggato, redirect alla dashboard
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            remember = request.form.get('remember', False)
            
            if not username or not password:
                flash('Username e password sono obbligatori', 'danger')
                return render_template('login.html')
            
            # Tentativo di autenticazione
            if auth_manager.authenticate_user(username, password, remember):
                flash(f'Bentornato, {username}!', 'success')
                
                # Redirect alla pagina richiesta o alla homepage
                next_page = request.args.get('next')
                if next_page:
                    return redirect(next_page)
                return redirect(url_for('index'))
            else:
                flash('Username o password non corretti', 'danger')
        
        return render_template('login.html')
    
    @app.route('/logout')
    def logout():
        """Logout dell'utente."""
        if current_user.is_authenticated:
            username = current_user.username
            logout_user()
            flash(f'Arrivederci, {username}!', 'info')
        
        return redirect(url_for('login'))
    
    @app.route('/auth/status')
    def auth_status():
        """API per controllare stato autenticazione (per AJAX)."""
        return {
            'authenticated': current_user.is_authenticated,
            'username': current_user.username if current_user.is_authenticated else None
        }