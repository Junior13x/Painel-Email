# ===============================================================
# == IMPORTAÇÕES E CONFIGURAÇÕES INICIAIS ==
# ===============================================================
import os
import requests
import re
import json
import smtplib
import time
from email.message import EmailMessage
from functools import wraps
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import create_engine, text, exc as sqlalchemy_exc
from dotenv import load_dotenv

# Importações para o worker assíncrono
import gevent
import mercadopago

# Carrega variáveis de ambiente de um arquivo .env (para desenvolvimento local)
load_dotenv()

app = Flask(__name__)
# É crucial ter uma chave secreta forte vinda das variáveis de ambiente
app.secret_key = os.environ.get('SECRET_KEY', 'uma-chave-secreta-padrao-para-desenvolvimento')

# ===============================================================
# == BANCO DE DADOS E INICIALIZAÇÃO ==
# ===============================================================

def get_db_connection():
    """Cria e retorna uma conexão com o banco de dados PostgreSQL."""
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise ValueError("Variável de ambiente DATABASE_URL não foi configurada.")
    
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
        
    engine = create_engine(db_url)
    conn = engine.connect()
    return conn

@app.cli.command('init-db')
def init_db_command():
    """Cria as tabelas do banco de dados e o usuário admin inicial."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.begin(): # Transações garantem que ou tudo funciona ou nada é salvo.
            print("Conectado ao banco de dados. Iniciando a criação das tabelas...")
            conn.execute(text("DROP TABLE IF EXISTS users, features, plans, plan_features, envio_historico, scheduled_emails, email_templates, mass_send_jobs CASCADE;"))
            print("Tabelas antigas removidas (se existiam).")

            # --- Criação de Todas as Tabelas ---
            conn.execute(text("CREATE TABLE features (id SERIAL PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, description TEXT);"))
            conn.execute(text("CREATE TABLE plans (id SERIAL PRIMARY KEY, name TEXT NOT NULL, price NUMERIC(10, 2) NOT NULL, validity_days INTEGER NOT NULL, daily_send_limit INTEGER DEFAULT 25, is_active BOOLEAN DEFAULT TRUE);"))
            conn.execute(text("""
                CREATE TABLE users (
                    id SERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user', 
                    plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL, 
                    plan_expiration_date DATE,
                    baserow_host TEXT, baserow_api_key TEXT, baserow_table_id TEXT,
                    smtp_host TEXT, smtp_port INTEGER, smtp_user TEXT, smtp_password TEXT,
                    batch_size INTEGER, delay_seconds INTEGER, automations_config TEXT,
                    sends_today INTEGER DEFAULT 0, last_send_date DATE
                );
            """))
            conn.execute(text("CREATE TABLE plan_features (plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE, feature_id INTEGER REFERENCES features(id) ON DELETE CASCADE, PRIMARY KEY (plan_id, feature_id));"))
            conn.execute(text("CREATE TABLE envio_historico (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), recipient_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT, sent_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);"))
            conn.execute(text("CREATE TABLE scheduled_emails (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), schedule_type TEXT NOT NULL, status_target TEXT, manual_recipients TEXT, subject TEXT NOT NULL, body TEXT NOT NULL, send_at TIMESTAMP NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_sent BOOLEAN DEFAULT FALSE);"))
            conn.execute(text("CREATE TABLE email_templates (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), name TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(user_id, name));"))
            # NOVA TABELA PARA ENVIOS EM MASSA
            conn.execute(text("""
                CREATE TABLE mass_send_jobs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    recipients_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', -- pending, processing, completed, failed
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP WITH TIME ZONE
                );
            """))
            print("Tabelas criadas com sucesso.")

            # --- Dados Iniciais ---
            conn.execute(text("INSERT INTO features (name, slug, description) VALUES ('Envio em Massa e por Status', 'mass-send', 'Permite o envio de e-mails em massa e por status de cliente.');"))
            conn.execute(text("INSERT INTO features (name, slug, description) VALUES ('Agendamentos de Campanhas', 'schedules', 'Permite agendar envios de e-mail para o futuro.');"))
            conn.execute(text("INSERT INTO features (name, slug, description) VALUES ('Automações Inteligentes', 'automations', 'Configura e-mails automáticos de boas-vindas e lembretes de expiração.');"))
            print("Features iniciais inseridas.")

            conn.execute(text("INSERT INTO plans (name, price, validity_days, daily_send_limit, is_active) VALUES ('VIP', 99.99, 30, -1, TRUE);"))
            vip_plan_id = conn.execute(text("SELECT id FROM plans WHERE name = 'VIP';")).scalar()
            
            all_feature_ids = conn.execute(text("SELECT id FROM features;")).mappings().fetchall()
            for feature_id_row in all_feature_ids:
                conn.execute(text("INSERT INTO plan_features (plan_id, feature_id) VALUES (:pid, :fid);"), {'pid': vip_plan_id, 'fid': feature_id_row['id']})
            print("Plano VIP inicial criado e associado a todas as features.")

            default_email = 'junior@admin.com'
            default_pass = '130896'
            password_hash = generate_password_hash(default_pass)
            conn.execute(text("INSERT INTO users (email, password_hash, role) VALUES (:email, :password_hash, 'admin')"), {'email': default_email, 'password_hash': password_hash})
            print(f"ADMIN PADRÃO CRIADO! E-mail: {default_email} | Senha: {default_pass}")
        
        print("\nBanco de dados inicializado com sucesso!")
    except Exception as e:
        print(f"\nOcorreu um erro durante a inicialização do banco de dados: {e}")
    finally:
        if conn and not conn.closed:
            conn.close()
            print("Conexão com o banco de dados fechada.")


# ===============================================================
# == DECORATORS (AUTENTICAÇÃO E PERMISSÕES) ==
# ===============================================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            flash("Por favor, faça login para acessar esta página.", "warning")
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('role') != 'admin':
            flash("Você não tem permissão para acessar esta página.", "danger")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def feature_required(feature_slug):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if session.get('role') == 'admin':
                return f(*args, **kwargs)

            user_features = session.get('user_features', [])
            
            if feature_slug in user_features:
                return f(*args, **kwargs)
            else:
                flash("Seu plano atual não dá acesso a este recurso.", "danger")
                return redirect(url_for('dashboard'))
        return decorated_function
    return decorator

# ===============================================================
# == FUNÇÕES DE LÓGICA (HELPERS) ==
# ===============================================================
@app.context_processor
def inject_user_info():
    if 'user_id' not in session: return {}
    conn = None
    try:
        conn = get_db_connection()
        user = conn.execute(text("SELECT * FROM users WHERE id = :id"), {'id': session['user_id']}).mappings().fetchone()
        if not user:
            session.clear()
            return {}
        
        enabled_features = set()
        if user['role'] == 'admin':
            all_features = conn.execute(text("SELECT slug FROM features")).mappings().fetchall()
            enabled_features = {row['slug'] for row in all_features}
        elif user.get('plan_id'):
            is_expired = user.get('plan_expiration_date') and user['plan_expiration_date'] < datetime.now().date()
            if not is_expired:
                feature_rows = conn.execute(
                    text("SELECT f.slug FROM features f JOIN plan_features pf ON f.id = pf.feature_id WHERE pf.plan_id = :plan_id"),
                    {'plan_id': user['plan_id']}
                ).mappings().fetchall()
                enabled_features = {row['slug'] for row in feature_rows}
        session['user_features'] = list(enabled_features)

        plan_status = {'plan_name': 'Grátis', 'badge_class': 'secondary', 'days_left': None}
        if user['role'] == 'admin':
            plan_status = {'plan_name': 'Admin', 'badge_class': 'danger', 'days_left': 9999}
        elif user.get('plan_id') and user.get('plan_expiration_date'):
            expiration_date = user['plan_expiration_date']
            days_left = (expiration_date - datetime.now().date()).days
            plan = conn.execute(text("SELECT name FROM plans WHERE id = :plan_id"), {'plan_id': user['plan_id']}).mappings().fetchone()
            plan_name = plan['name'] if plan else 'Expirado'
            if days_left >= 0:
                plan_status = {'plan_name': plan_name, 'badge_class': 'success', 'days_left': days_left}
            else:
                plan_status = {'plan_name': f"{plan_name} (Expirado)", 'badge_class': 'warning', 'days_left': days_left}
        
        sends_remaining = -1
        daily_limit = -1
        if user['role'] != 'admin':
            plan = conn.execute(text("SELECT daily_send_limit FROM plans WHERE id = :pid"), {'pid': user['plan_id']}).mappings().fetchone() if user['plan_id'] else None
            daily_limit = plan['daily_send_limit'] if plan else 25
            sends_today = 0
            if user['last_send_date'] and user['last_send_date'] == datetime.now().date():
                sends_today = user['sends_today']
            sends_remaining = daily_limit - sends_today if daily_limit != -1 else -1

        return dict(user_plan_status=plan_status, sends_remaining=sends_remaining, daily_limit=daily_limit, is_admin=(user['role'] == 'admin'), user_features=list(enabled_features))
    except Exception as e:
        print(f"Erro ao injetar dados do plano: {e}")
        return {}
    finally:
        if conn: conn.close()

def load_user_settings(user_id):
    conn = None
    try:
        conn = get_db_connection()
        user_data = conn.execute(text("SELECT * FROM users WHERE id = :id"), {'id': user_id}).mappings().fetchone()
        return dict(user_data) if user_data else {}
    finally:
        if conn: conn.close()

def parse_date_string(date_string):
    if not date_string: return None
    for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d']:
        try:
            return datetime.strptime(date_string.strip().split('T')[0], fmt)
        except (ValueError, TypeError): continue
    return None

def process_contacts_status(contacts):
    processed_contacts = []
    today = datetime.now()
    for contact in contacts:
        status_text, status_badge_class, dias_restantes_calculado, remaining_days_int = 'Status Indefinido', 'secondary', 'N/A', 99999
        try:
            dias_validade_str, pagamento_str = contact.get('Dias'), contact.get('Pagamento')
            if dias_validade_str is not None and int(dias_validade_str) == 1:
                status_text, status_badge_class, dias_restantes_calculado, remaining_days_int = 'Expirado / Free', 'danger', 'FREE', -999
            elif pagamento_str and dias_validade_str:
                pagamento_date = parse_date_string(pagamento_str)
                if pagamento_date:
                    expiration_date = pagamento_date + timedelta(days=int(dias_validade_str))
                    remaining_days = (expiration_date.date() - today.date()).days
                    remaining_days_int, dias_restantes_calculado = remaining_days, f"{remaining_days} dia(s)"
                    if remaining_days < 0: status_text, status_badge_class = 'Expirado / Free', 'danger'
                    elif remaining_days <= 7: status_text, status_badge_class = 'Expirando', 'warning'
                    else: status_text, status_badge_class = 'Vip / Em Dia', 'success'
        except (ValueError, TypeError) as e:
            print(f"Aviso ao processar contato ID {contact.get('id')}: {e}")
        contact.update({'status_text': status_text, 'status_badge_class': status_badge_class, 'dias_restantes_calculado': dias_restantes_calculado, 'remaining_days_int': remaining_days_int})
        processed_contacts.append(contact)
    return processed_contacts

def get_all_contacts_from_baserow(settings):
    if not all(settings.get(k) for k in ['baserow_host', 'baserow_api_key', 'baserow_table_id']):
        raise Exception("Configurações do Baserow incompletas.")
    all_rows, page = [], 1
    base_url = f"{settings['baserow_host']}/api/database/rows/table/{settings['baserow_table_id']}/?user_field_names=true&size=200"
    headers = {"Authorization": f"Token {settings['baserow_api_key']}"}
    while True:
        try:
            response = requests.get(f"{base_url}&page={page}", headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            rows = data.get('results', [])
            if not rows: break
            all_rows.extend(rows)
            if not data.get('next'): break
            page += 1
        except requests.RequestException as e:
            print(f"Erro ao buscar página {page} do Baserow: {e}")
            raise e
    return all_rows

def send_emails_in_batches(recipients, subject, body, settings, user_id):
    batch_size, delay_seconds, smtp_port = int(settings.get('batch_size') or 15), int(settings.get('delay_seconds') or 60), int(settings.get('smtp_port') or 587)
    sent_count, fail_count = 0, 0
    conn = None
    try:
        conn = get_db_connection()
        with conn.begin():
            for i in range(0, len(recipients), batch_size):
                batch = recipients[i:i + batch_size]
                for recipient in batch:
                    recipient_email = recipient.get("Email")
                    if not recipient_email: continue
                    try:
                        msg = EmailMessage()
                        msg['Subject'], msg['From'], msg['To'] = subject, settings.get('smtp_user'), recipient_email
                        msg.add_alternative(body, subtype='html')
                        with smtplib.SMTP(str(settings.get('smtp_host')), smtp_port) as server:
                            server.starttls()
                            server.login(settings.get('smtp_user'), settings.get('smtp_password'))
                            server.send_message(msg)
                        sent_count += 1
                        conn.execute(text("INSERT INTO envio_historico (user_id, recipient_email, subject, body) VALUES (:uid, :re, :s, :b)"), {'uid': user_id, 're': recipient_email, 's': subject, 'b': body})
                    except Exception as e:
                        fail_count += 1
                        print(f"FALHA ao enviar para {recipient_email}: {e}")
                if i + batch_size < len(recipients): gevent.sleep(delay_seconds)
    except Exception as e:
        print(f"Erro crítico no envio em lote: {e}")
    finally:
        if conn: conn.close()
    return sent_count, fail_count

# ===============================================================
# == ROTAS DA APLICAÇÃO ==
# ===============================================================

@app.route('/')
def home(): return redirect(url_for('login_page'))

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if 'logged_in' in session: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email, password, conn = request.form.get('email'), request.form.get('password'), None
        try:
            conn = get_db_connection()
            user = conn.execute(text("SELECT * FROM users WHERE email = :email"), {'email': email}).mappings().fetchone()
            if user and check_password_hash(user['password_hash'], password):
                session.update({'logged_in': True, 'user_id': user['id'], 'user_email': user['email'], 'role': user['role']})
                return redirect(url_for('dashboard'))
            else:
                flash("E-mail ou senha inválidos.", "danger")
        finally:
            if conn: conn.close()
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if 'logged_in' in session: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email, password, conn = request.form.get('email'), request.form.get('password'), None
        try:
            conn = get_db_connection()
            with conn.begin():
                conn.execute(text("INSERT INTO users (email, password_hash) VALUES (:email, :ph)"), {'email': email, 'ph': generate_password_hash(password)})
            flash("Conta criada com sucesso! Faça seu login.", "success")
            return redirect(url_for('login_page'))
        except sqlalchemy_exc.IntegrityError:
            flash("Este e-mail já está cadastrado.", "danger")
        finally:
            if conn: conn.close()
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    session.clear()
    flash("Você saiu com sucesso.", "info")
    return redirect(url_for('login_page'))

@app.route('/dashboard')
@login_required
def dashboard(): return render_template('dashboard.html')

@app.route('/ajuda')
@login_required
def help_page(): return render_template('ajuda.html')

# --- ROTAS DE ADMIN ---
@app.route('/users', methods=['GET', 'POST'])
@login_required
@admin_required
def users_page():
    conn = None
    try:
        conn = get_db_connection()
        if request.method == 'POST':
            email, password, role = request.form.get('email'), request.form.get('password'), request.form.get('role', 'user')
            if email and password:
                try:
                    with conn.begin():
                        conn.execute(text("INSERT INTO users (email, password_hash, role) VALUES (:e, :ph, :r)"), {'e': email, 'ph': generate_password_hash(password), 'r': role})
                    flash(f"Usuário '{email}' criado!", "success")
                except sqlalchemy_exc.IntegrityError:
                    flash(f"O e-mail '{email}' já existe.", "danger")
            else:
                flash("E-mail e senha são obrigatórios.", "warning")
            return redirect(url_for('users_page'))
        users = conn.execute(text("SELECT u.*, p.name as plan_name FROM users u LEFT JOIN plans p ON u.plan_id = p.id ORDER BY u.id ASC")).mappings().fetchall()
        return render_template('users.html', users=users)
    finally:
        if conn: conn.close()

@app.route('/users/edit/<int:user_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user_page(user_id):
    conn = None
    try:
        conn = get_db_connection()
        if request.method == 'POST':
            plan_id_str, validity_days_str = request.form.get('plan_id'), request.form.get('validity_days')
            with conn.begin():
                if not plan_id_str or plan_id_str == 'free':
                    conn.execute(text("UPDATE users SET plan_id = NULL, plan_expiration_date = NULL WHERE id = :uid"), {'uid': user_id})
                    flash(f"Usuário ID {user_id} definido como Grátis.", "info")
                else:
                    validity_days = int(validity_days_str or 30)
                    expiration_date = datetime.now() + timedelta(days=validity_days)
                    conn.execute(text("UPDATE users SET plan_id = :pid, plan_expiration_date = :exp WHERE id = :uid"), {'pid': int(plan_id_str), 'exp': expiration_date.date(), 'uid': user_id})
                    plan_name = conn.execute(text("SELECT name FROM plans WHERE id = :pid"), {'pid': int(plan_id_str)}).scalar_one_or_none() or "desconhecido"
                    flash(f"Usuário ID {user_id} atualizado para o plano '{plan_name}'!", "success")
            return redirect(url_for('users_page'))
        user = conn.execute(text("SELECT * FROM users WHERE id = :uid"), {'uid': user_id}).mappings().fetchone()
        all_plans = conn.execute(text("SELECT * FROM plans WHERE is_active = TRUE")).mappings().fetchall()
        if not user:
            flash("Usuário não encontrado.", "warning")
            return redirect(url_for('users_page'))
        return render_template('edit_user.html', user=user, all_plans=all_plans)
    finally:
        if conn: conn.close()

@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    if session['user_id'] == user_id:
        flash("Você não pode excluir a própria conta.", "danger")
    else:
        conn = None
        try:
            conn = get_db_connection()
            with conn.begin():
                conn.execute(text("DELETE FROM users WHERE id = :id"), {'id': user_id})
            flash("Usuário excluído.", "info")
        finally:
            if conn: conn.close()
    return redirect(url_for('users_page'))

@app.route('/admin/plans', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_plans_page():
    conn = None
    try:
        conn = get_db_connection()
        if request.method == 'POST':
            name, price, validity, limit = request.form.get('name'), float(request.form.get('price', 0)), int(request.form.get('validity_days', 30)), int(request.form.get('daily_send_limit', 50))
            features = request.form.getlist('features')
            if not name:
                flash("O nome do plano é obrigatório.", "danger")
            elif not features:
                 flash("Atenção: Você deve selecionar pelo menos um recurso para o plano.", "warning")
            else:
                with conn.begin():
                    result = conn.execute(text("INSERT INTO plans (name, price, validity_days, daily_send_limit) VALUES (:n, :p, :v, :l) RETURNING id"), {'n': name, 'p': price, 'v': validity, 'l': limit})
                    new_plan_id = result.scalar_one()
                    for fid in features:
                        conn.execute(text("INSERT INTO plan_features (plan_id, feature_id) VALUES (:pid, :fid)"), {'pid': new_plan_id, 'fid': int(fid)})
                flash(f"Plano '{name}' criado com sucesso!", "success")
            return redirect(url_for('manage_plans_page'))
        
        plans = conn.execute(text("SELECT p.*, (SELECT COUNT(*) FROM plan_features pf WHERE pf.plan_id = p.id) as feature_count FROM plans p ORDER BY p.price")).mappings().fetchall()
        features = conn.execute(text("SELECT * FROM features ORDER BY id")).mappings().fetchall()
        return render_template('manage_plans.html', plans=plans, all_features=features)
    finally:
        if conn: conn.close()

@app.route('/admin/plans/delete/<int:plan_id>', methods=['POST'])
@login_required
@admin_required
def delete_plan(plan_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.begin():
            conn.execute(text("DELETE FROM plans WHERE id = :pid"), {'pid': plan_id})
        flash("Plano excluído com sucesso.", "info")
    except Exception as e:
        flash(f"Erro ao excluir plano: {e}", "danger")
    finally:
        if conn: conn.close()
    return redirect(url_for('manage_plans_page'))

# --- ROTAS DE PLANOS E PAGAMENTOS ---
@app.route('/planos')
@login_required
def plans_page():
    conn = None
    try:
        conn = get_db_connection()
        master_features = conn.execute(text("SELECT * FROM features ORDER BY id")).mappings().fetchall()
        active_plans = conn.execute(text("SELECT * FROM plans WHERE is_active = TRUE ORDER BY price")).mappings().fetchall()
        plans_data = []
        for plan in active_plans:
            feature_ids = {row['feature_id'] for row in conn.execute(text("SELECT feature_id FROM plan_features WHERE plan_id = :pid"), {'pid': plan['id']}).mappings().fetchall()}
            plans_data.append({'plan': plan, 'enabled_feature_ids': feature_ids})
        return render_template('planos.html', plans_data=plans_data, master_features=master_features)
    finally:
        if conn: conn.close()

@app.route('/verificar-status-plano')
@login_required
def check_plan_status():
    conn = None
    try:
        conn = get_db_connection()
        user = conn.execute(text("SELECT plan_id, plan_expiration_date FROM users WHERE id = :uid"), {'uid': session['user_id']}).mappings().fetchone()
        
        if not user:
            return jsonify({'status': 'erro', 'message': 'Usuário não encontrado'}), 404

        is_approved = (
            user['plan_id'] is not None and
            user['plan_expiration_date'] is not None and
            user['plan_expiration_date'] >= datetime.now().date()
        )
        
        if is_approved:
            return jsonify({'status': 'aprovado'})
        else:
            return jsonify({'status': 'pendente'})
    except Exception as e:
        print(f"Erro em check_plan_status: {e}")
        return jsonify({'status': 'erro', 'message': str(e)}), 500
    finally:
        if conn: conn.close()
        
@app.route('/criar-pagamento', methods=['POST'])
@login_required
def create_payment():
    plan_id, conn = request.form.get('plan_id'), None
    try:
        conn = get_db_connection()
        plan = conn.execute(text("SELECT * FROM plans WHERE id = :pid"), {'pid': plan_id}).mappings().fetchone()
        
        access_token = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")
        base_url = os.environ.get('RENDER_EXTERNAL_URL')
        if not all([plan, access_token, base_url]):
            flash("Configuração inválida para pagamento. Verifique se o plano existe e se as variáveis MERCADO_PAGO_ACCESS_TOKEN e RENDER_EXTERNAL_URL estão definidas no Render.", "danger")
            return redirect(url_for('plans_page'))
            
        sdk = mercadopago.SDK(access_token)
        
        payer_info = {
            "email": session["user_email"],
            "first_name": "Usuario",
            "last_name": "Painel"
        }

        payment_data = {
            "transaction_amount": float(plan['price']),
            "description": f"Plano {plan['name']} - {session['user_email']}",
            "payment_method_id": "pix",
            "payer": payer_info,
            "notification_url": f"{base_url}{url_for('mp_webhook')}",
            "external_reference": f"user:{session['user_id']};plan:{plan_id}"
        }
        
        payment_response = sdk.payment().create(payment_data)
        
        if payment_response and payment_response.get("status") == 201:
            pi = payment_response["response"]['point_of_interaction']['transaction_data']
            return render_template("pagamento_pix.html", pix_code=pi['qr_code'], qr_code_base64=pi['qr_code_base64'])
        else:
            error_message = payment_response.get('response', {}).get('message', 'Erro desconhecido na API do Mercado Pago.')
            flash(f"Erro ao gerar cobrança: {error_message}", "danger")
            print(f"Erro Mercado Pago (status não 201): {payment_response}")
            return redirect(url_for('plans_page'))

    except Exception as e:
        print("--- ERRO CRÍTICO AO CRIAR PAGAMENTO ---")
        print(f"Exceção capturada: {e}")
        
        error_message_for_user = "Ocorreu um erro ao gerar a cobrança Pix."
        try:
            # Tenta extrair a mensagem de erro específica da API do Mercado Pago
            sdk_error_body = json.loads(e.body)
            api_message = sdk_error_body.get("message", "Erro na API do MP.")
            causes = sdk_error_body.get("cause", [])
            if causes:
                cause_descriptions = [c.get('description', '') for c in causes]
                details = ', '.join(cause_descriptions)
                error_message_for_user = f"Erro do Mercado Pago: {api_message} Detalhes: {details}"
            else:
                error_message_for_user = f"Erro do Mercado Pago: {api_message}"
        except (AttributeError, ValueError, KeyError, TypeError):
            error_message_for_user = f"Ocorreu um erro inesperado: {e}"
        
        print(f"Mensagem de erro para o usuário: {error_message_for_user}")
        print("--- FIM DO ERRO ---")
        flash(error_message_for_user, "danger")
        return redirect(url_for('plans_page'))
    finally:
        if conn: conn.close()

@app.route('/mercado-pago/webhook', methods=['POST'])
def mp_webhook():
    data, access_token = request.json, os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")
    if data and data.get("action") == "payment.updated" and access_token:
        payment_id, conn = data["data"]["id"], None
        sdk = mercadopago.SDK(access_token)
        try:
            payment_info = sdk.payment().get(payment_id)
            if payment_info["status"] == 200 and payment_info["response"]["status"] == "approved":
                payment = payment_info["response"]
                user_id, plan_id = [int(p.split(':')[1]) for p in payment['external_reference'].split(';')]
                conn = get_db_connection()
                with conn.begin():
                    plan = conn.execute(text("SELECT * FROM plans WHERE id = :pid"), {'pid': plan_id}).mappings().fetchone()
                    user = conn.execute(text("SELECT * FROM users WHERE id = :uid"), {'uid': user_id}).mappings().fetchone()
                    if user and plan:
                        expiration_date = datetime.now() + timedelta(days=plan['validity_days'])
                        if user['plan_expiration_date'] and user['plan_expiration_date'] > datetime.now().date():
                            expiration_date = datetime.combine(user['plan_expiration_date'], datetime.min.time()) + timedelta(days=plan['validity_days'])
                        conn.execute(text("UPDATE users SET plan_id = :pid, plan_expiration_date = :exp WHERE id = :uid"), {'pid': plan_id, 'exp': expiration_date.date(), 'uid': user_id})
                        print(f"Plano do usuário {user_id} atualizado para o plano {plan_id}.")
        except Exception as e:
            print(f"Erro no webhook: {e}")
        finally:
            if conn: conn.close()
    return jsonify({"status": "received"}), 200

# --- ROTAS DE FUNCIONALIDADES ---
@app.route('/envio-em-massa', methods=['GET', 'POST'])
@login_required
@feature_required('mass-send')
def mass_send_page():
    user_id = session['user_id']
    settings = load_user_settings(user_id)

    if request.method == 'POST':
        if session.get('role') != 'admin' and not all(settings.get(k) for k in ['smtp_host', 'smtp_user', 'smtp_password']):
            flash("Configurações de SMTP incompletas.", "danger")
            return redirect(url_for('settings_page'))
        
        try:
            all_contacts = process_contacts_status(get_all_contacts_from_baserow(settings))
            bulk_action = request.form.get('bulk_action')
            recipients = []
            if bulk_action and bulk_action != 'manual':
                if bulk_action == 'all': recipients = all_contacts
                else: recipients = [c for c in all_contacts if c.get('status_badge_class') == bulk_action]
            else:
                selected_ids = request.form.getlist('selected_contacts')
                recipients = [c for c in all_contacts if str(c.get('id')) in selected_ids]

            if not recipients:
                flash("Nenhum destinatário selecionado.", "warning")
                return redirect(url_for('mass_send_page'))
            
            user_info = inject_user_info()
            sends_remaining = user_info.get('sends_remaining', 0)
            if sends_remaining != -1 and len(recipients) > sends_remaining:
                flash(f"Envio bloqueado. Você tentou enviar para {len(recipients)} contatos, mas só tem {sends_remaining} envios restantes hoje.", "danger")
                return redirect(url_for('mass_send_page'))

            subject, body = request.form.get('subject'), request.form.get('body')
            
            conn = get_db_connection()
            with conn.begin():
                conn.execute(
                    text("INSERT INTO mass_send_jobs (user_id, subject, body, recipients_json) VALUES (:uid, :sub, :body, :rec)"),
                    {'uid': user_id, 'sub': subject, 'body': body, 'rec': json.dumps(recipients)}
                )
            conn.close()
            
            flash("Campanha de envio em massa agendada com sucesso! O processamento será feito em segundo plano.", "success")
            return redirect(url_for('history_page'))

        except Exception as e:
            flash(f"Erro ao agendar envio: {e}", "danger")

    # GET
    contacts, error_message, templates = [], None, []
    conn = None
    try:
        if all(settings.get(k) for k in ['baserow_host', 'baserow_api_key', 'baserow_table_id']):
            contacts = process_contacts_status(get_all_contacts_from_baserow(settings))
        conn = get_db_connection()
        templates = conn.execute(text("SELECT * FROM email_templates WHERE user_id = :uid ORDER BY name"), {'uid': user_id}).mappings().fetchall()
    except Exception as e:
        error_message = f"Não foi possível carregar dados: {e}"
    finally:
        if conn: conn.close()
    return render_template('envio_em_massa.html', contacts=contacts, error=error_message, templates=templates)


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    user_id = session['user_id']
    is_admin = session.get('role') == 'admin'
    conn = None
    try:
        conn = get_db_connection()
        if request.method == 'POST':
            if is_admin and 'mp_access_token' in request.form:
                # Lógica para admin salvar o token do MP nas variáveis de ambiente (não recomendado diretamente, mas para manter a funcionalidade)
                # O ideal é o admin configurar isso diretamente no Render.com
                pass 

            with conn.begin():
                automations = {
                    'welcome': {'enabled': 'welcome_enabled' in request.form, 'subject': request.form.get('welcome_subject'), 'body': request.form.get('welcome_body')},
                    'expiry': {
                        'enabled': 'expiry_enabled' in request.form,
                        'subject_7_days': request.form.get('expiry_7_days_subject'), 'body_7_days': request.form.get('expiry_7_days_body'),
                        'subject_3_days': request.form.get('expiry_3_days_subject'), 'body_3_days': request.form.get('expiry_3_days_body'),
                        'subject_1_day': request.form.get('expiry_1_day_subject'), 'body_1_day': request.form.get('expiry_1_day_body')
                    }
                }
                
                update_fields = {k: request.form.get(k) for k in ['baserow_host', 'baserow_api_key', 'baserow_table_id', 'smtp_host', 'smtp_user']}
                update_fields.update({
                    'smtp_port': int(request.form.get('smtp_port') or 587),
                    'batch_size': int(request.form.get('batch_size') or 15),
                    'delay_seconds': int(request.form.get('delay_seconds') or 60),
                    'automations_config': json.dumps(automations)
                })

                set_clauses = [f"{key} = :{key}" for key in update_fields]
                if request.form.get('smtp_password'):
                    set_clauses.append("smtp_password = :smtp_password")
                    update_fields['smtp_password'] = request.form.get('smtp_password')
                
                conn.execute(text(f"UPDATE users SET {', '.join(set_clauses)} WHERE id = :user_id"), {**update_fields, 'user_id': user_id})

            flash("Configurações salvas!", "success")
            return redirect(url_for('settings_page'))
        
        user_settings_row = conn.execute(text('SELECT * FROM users WHERE id = :uid'), {'uid': user_id}).mappings().fetchone()
        user_settings = dict(user_settings_row) if user_settings_row else {}
        
        if user_settings.get('automations_config'):
            user_settings['automations_config'] = json.loads(user_settings['automations_config'])
        else:
            user_settings['automations_config'] = {}
        
        global_settings = {}
        if is_admin:
            global_settings['MERCADO_PAGO_ACCESS_TOKEN'] = os.environ.get('MERCADO_PAGO_ACCESS_TOKEN', '')

        return render_template('settings.html', user_settings=user_settings, global_settings=global_settings)
    
    except Exception as e:
        flash(f"Erro ao salvar/carregar configurações: {e}", "danger")
        return redirect(url_for('dashboard'))
    finally:
        if conn: conn.close()

@app.route('/history')
@login_required
@feature_required('mass-send')
def history_page():
    conn = None
    try:
        conn = get_db_connection()
        history = conn.execute(text("SELECT * FROM envio_historico WHERE user_id = :uid ORDER BY sent_at DESC"), {'uid': session['user_id']}).mappings().fetchall()
        return render_template('history.html', history=history)
    finally:
        if conn: conn.close()

@app.route('/history/resend', methods=['POST'])
@login_required
@feature_required('mass-send')
def resend_from_history():
    session['resend_subject'] = request.form.get('subject')
    session['resend_body'] = request.form.get('body')
    flash('Conteúdo carregado para reenvio.', 'info')
    return redirect(url_for('mass_send_page'))

@app.route('/history/save-as-template', methods=['POST'])
@login_required
@feature_required('mass-send')
def save_history_as_template():
    template_name, subject, body, user_id = request.form.get('template_name'), request.form.get('subject'), request.form.get('body'), session['user_id']
    if not all([template_name, subject, body]):
        flash("Nome, assunto e corpo são necessários.", "warning")
    else:
        conn = None
        try:
            conn = get_db_connection()
            with conn.begin():
                conn.execute(text("INSERT INTO email_templates (user_id, name, subject, body) VALUES (:uid, :n, :s, :b)"), {'uid': user_id, 'n': template_name, 's': subject, 'b': body})
            flash(f'Salvo como modelo "{template_name}"!', 'success')
        except sqlalchemy_exc.IntegrityError:
            flash(f'Um modelo com o nome "{template_name}" já existe.', 'danger')
        except Exception as e:
            flash(f'Erro ao salvar modelo: {e}', 'danger')
        finally:
            if conn: conn.close()
    return redirect(url_for('history_page'))


@app.route('/templates', methods=['GET', 'POST'])
@login_required
@feature_required('mass-send')
def templates_page():
    conn, user_id = None, session['user_id']
    try:
        conn = get_db_connection()
        if request.method == 'POST':
            name, subject, body = request.form.get('template_name'), request.form.get('subject'), request.form.get('body')
            if name and subject and body:
                try:
                    with conn.begin():
                        conn.execute(text("INSERT INTO email_templates (user_id, name, subject, body) VALUES (:uid, :n, :s, :b)"), {'uid': user_id, 'n': name, 's': subject, 'b': body})
                    flash("Template salvo!", "success")
                except sqlalchemy_exc.IntegrityError:
                    flash("Um template com esse nome já existe.", "danger")
            else:
                flash("Todos os campos são obrigatórios.", "warning")
            return redirect(url_for('templates_page'))
        templates = conn.execute(text("SELECT * FROM email_templates WHERE user_id = :uid ORDER BY name"), {'uid': user_id}).mappings().fetchall()
        return render_template('templates.html', templates=templates)
    finally:
        if conn: conn.close()


@app.route('/agendamento', methods=['GET', 'POST'])
@login_required
@feature_required('schedules')
def schedule_page():
    conn, user_id = None, session['user_id']
    try:
        conn = get_db_connection()
        if request.method == 'POST':
            subject, body, send_at_str = request.form.get('subject'), request.form.get('body'), request.form.get('send_at')
            schedule_type, status_target, manual_recipients = request.form.get('schedule_type'), None, None
            
            if schedule_type == 'group':
                status_target = request.form.get('status_target')
            elif schedule_type == 'manual':
                emails = [email.strip() for email in re.split(r'[,\s]+', request.form.get('manual_emails', '')) if email.strip()]
                if not emails:
                    flash("Insira ao menos um e-mail válido para agendamento manual.", "warning")
                    return redirect(url_for('schedule_page'))
                manual_recipients = ','.join(emails)

            if not all([subject, body, send_at_str, schedule_type]):
                flash("Todos os campos são obrigatórios para agendar.", "warning")
            else:
                send_at_dt = datetime.strptime(send_at_str, '%Y-%m-%dT%H:%M')
                with conn.begin():
                    conn.execute(
                        text("""INSERT INTO scheduled_emails (user_id, schedule_type, status_target, manual_recipients, subject, body, send_at) 
                                VALUES (:uid, :st, :stat, :mr, :sub, :body, :sa)"""),
                        {'uid': user_id, 'st': schedule_type, 'stat': status_target, 'mr': manual_recipients, 'sub': subject, 'body': body, 'sa': send_at_dt}
                    )
                flash("E-mail agendado com sucesso!", "success")
            return redirect(url_for('schedule_page'))

        pending_emails = conn.execute(text("SELECT * FROM scheduled_emails WHERE user_id = :uid AND is_sent = FALSE ORDER BY send_at ASC"), {'uid': user_id}).mappings().fetchall()
        return render_template('agendamento.html', pending_emails=pending_emails, schedule_data=None)
    except Exception as e:
        print(f"Erro na página de agendamentos: {e}")
        flash("Ocorreu um erro ao processar seu pedido.", "danger")
        return redirect(url_for('dashboard'))
    finally:
        if conn: conn.close()


@app.route('/agendamento/delete/<int:email_id>', methods=['POST'])
@login_required
@feature_required('schedules')
def delete_schedule(email_id):
    conn, user_id = None, session['user_id']
    try:
        conn = get_db_connection()
        with conn.begin():
            result = conn.execute(text("DELETE FROM scheduled_emails WHERE id = :eid AND user_id = :uid"), {'eid': email_id, 'uid': user_id})
            if result.rowcount > 0: flash("Agendamento excluído.", "info")
            else: flash("Agendamento não encontrado ou sem permissão.", "danger")
    except Exception as e:
        print(f"Erro ao excluir agendamento: {e}")
        flash("Não foi possível excluir o agendamento.", "danger")
    finally:
        if conn: conn.close()
    return redirect(url_for('schedule_page'))

@app.route('/automations', methods=['GET', 'POST'])
@login_required
@feature_required('automations')
def automations_page():
    user_id = session['user_id']
    conn = None
    try:
        conn = get_db_connection()
        if request.method == 'POST':
            automations = {
                'welcome': {
                    'enabled': 'welcome_enabled' in request.form,
                    'subject': request.form.get('welcome_subject', ''),
                    'body': request.form.get('welcome_body', '')
                },
                'expiry': {
                    'enabled': 'expiry_enabled' in request.form,
                    'subject_7_days': request.form.get('expiry_7_days_subject', ''),
                    'body_7_days': request.form.get('expiry_7_days_body', ''),
                    'subject_3_days': request.form.get('expiry_3_days_subject', ''),
                    'body_3_days': request.form.get('expiry_3_days_body', ''),
                    'subject_1_day': request.form.get('expiry_1_day_subject', ''),
                    'body_1_day': request.form.get('expiry_1_day_body', '')
                }
            }
            with conn.begin():
                conn.execute(text("UPDATE users SET automations_config = :config WHERE id = :uid"), {'config': json.dumps(automations), 'uid': user_id})
            flash('Configurações de automação salvas!', 'success')
            return redirect(url_for('automations_page'))
        
        user = conn.execute(text("SELECT automations_config FROM users WHERE id = :uid"), {'uid': user_id}).mappings().fetchone()
        automations_data = json.loads(user['automations_config']) if user and user['automations_config'] else {}
        return render_template('automations.html', automations=automations_data)
    except Exception as e:
        print(f"Erro na página de automações: {e}")
        flash("Ocorreu um erro ao carregar as automações.", "danger")
        return redirect(url_for('dashboard'))
    finally:
        if conn: conn.close()


# ===============================================================
# == LÓGICA DO WORKER (ROBÔ) INTEGRADO ==
# ===============================================================

def worker_process_mass_send_jobs(user_settings, conn):
    """Processa envios em massa pendentes para um usuário."""
    user_id = user_settings['id']
    
    # Busca um job pendente para este usuário
    job = conn.execute(text("SELECT * FROM mass_send_jobs WHERE user_id = :uid AND status = 'pending' LIMIT 1"), {'uid': user_id}).mappings().fetchone()
    if not job:
        return

    print(f"-> [Usuário: {user_settings['email']}] Encontrado 1 job de envio em massa (ID: {job['id']}).")
    
    try:
        # Marca o job como 'processing' para evitar que seja pego novamente
        conn.execute(text("UPDATE mass_send_jobs SET status = 'processing', processed_at = :now WHERE id = :job_id"), {'now': datetime.now(), 'job_id': job['id']})
        conn.commit()

        recipients = json.loads(job['recipients_json'])
        subject = job['subject']
        body = job['body']
        
        # Recalcula o limite de envios no momento da ação
        user = conn.execute(text("SELECT * FROM users WHERE id = :id"), {'id': user_id}).mappings().fetchone()
        plan = conn.execute(text("SELECT daily_send_limit FROM plans WHERE id = :pid"), {'pid': user['plan_id']}).mappings().fetchone() if user['plan_id'] else None
        daily_limit = plan['daily_send_limit'] if plan else 25
        sends_today = user['sends_today'] if user['last_send_date'] == datetime.now().date() else 0
        sends_remaining = daily_limit - sends_today if daily_limit != -1 else -1

        if sends_remaining != -1 and len(recipients) > sends_remaining:
            print(f"--> Job ID {job['id']} falhou: Limite de envios excedido.")
            conn.execute(text("UPDATE mass_send_jobs SET status = 'failed' WHERE id = :job_id"), {'job_id': job['id']})
            conn.commit()
            return
            
        sent, failed = send_emails_in_batches(recipients, subject, body, user_settings, user_id)
        
        # Atualiza o contador de envios
        if daily_limit != -1 and sent > 0:
            conn.execute(text("UPDATE users SET sends_today = :st, last_send_date = :lsd WHERE id = :uid"), {'st': sends_today + sent, 'lsd': datetime.now().date(), 'uid': user_id})

        # Marca o job como 'completed'
        conn.execute(text("UPDATE mass_send_jobs SET status = 'completed' WHERE id = :job_id"), {'job_id': job['id']})
        conn.commit()
        print(f"--> Job ID {job['id']} concluído. {sent} enviados, {failed} falhas.")

    except Exception as e:
        print(f"--> ERRO CRÍTICO ao processar job de envio em massa ID {job['id']}: {e}")
        conn.execute(text("UPDATE mass_send_jobs SET status = 'failed' WHERE id = :job_id"), {'job_id': job['id']})
        conn.commit()

def worker_process_pending_schedules(user_settings, all_contacts_processed, conn):
    """Processa e-mails da fila de agendamento para um usuário específico."""
    user_id = user_settings['id']
    pending_emails = conn.execute(text("SELECT * FROM scheduled_emails WHERE user_id = :uid AND is_sent = FALSE AND send_at <= :now"), {'uid': user_id, 'now': datetime.now()}).mappings().fetchall()
    if not pending_emails: return

    print(f"-> [Usuário: {user_settings['email']}] Encontrados {len(pending_emails)} agendamento(s) para processar.")
    for email_job in pending_emails:
        recipients = []
        if email_job['schedule_type'] == 'group':
            target = email_job['status_target']
            if target == 'all': recipients = all_contacts_processed
            else: recipients = [c for c in all_contacts_processed if c['status_badge_class'] == target]
        elif email_job['schedule_type'] == 'manual' and email_job['manual_recipients']:
            recipients = [{'Email': email.strip()} for email in email_job['manual_recipients'].split(',')]
        
        if recipients:
            print(f"--> Processando agendamento ID {email_job['id']} para {len(recipients)} destinatário(s)...")
            sent, failed = send_emails_in_batches(recipients, email_job['subject'], email_job['body'], user_settings, user_id)
            if failed == 0:
                conn.execute(text("UPDATE scheduled_emails SET is_sent = TRUE WHERE id = :eid"), {'eid': email_job['id']})
                print(f"--> Agendamento ID {email_job['id']} concluído e marcado como enviado.")
        else:
            conn.execute(text("UPDATE scheduled_emails SET is_sent = TRUE WHERE id = :eid"), {'eid': email_job['id']}) # Marca como enviado mesmo sem destinatários para não rodar de novo
            print(f"--> Agendamento ID {email_job['id']} não tinha destinatários e foi marcado como enviado.")

def worker_check_and_run_automations(user_settings, all_contacts_processed, conn):
    """Verifica e executa as automações para um usuário específico."""
    user_id = user_settings['id']
    automations = json.loads(user_settings.get('automations_config') or '{}')
    
    # Automação de Boas-vindas
    welcome_config = automations.get('welcome', {})
    if welcome_config.get('enabled'):
        subject, body = welcome_config.get('subject'), welcome_config.get('body')
        if subject and body:
            new_contacts = [c for c in all_contacts_processed if c.get("Data") and (datetime.now() - parse_date_string(c["Data"])).days < 1]
            for contact in new_contacts:
                already_sent = conn.execute(text("SELECT id FROM envio_historico WHERE user_id = :uid AND recipient_email = :re AND subject = :sub"), {'uid': user_id, 're': contact.get('Email'), 'sub': subject}).fetchone()
                if not already_sent:
                    print(f"--> Enviando boas-vindas para {contact.get('Email')}")
                    send_emails_in_batches([contact], subject, body, user_settings, user_id)

    # Automação de Expiração
    expiry_config = automations.get('expiry', {})
    if expiry_config.get('enabled'):
        for days_left in [7, 3, 1]:
            subject, body = expiry_config.get(f'subject_{days_left}_days'), expiry_config.get(f'body_{days_left}_days')
            if subject and body:
                expiring_contacts = [c for c in all_contacts_processed if c.get('remaining_days_int') == days_left]
                for contact in expiring_contacts:
                    already_sent_today = conn.execute(text("SELECT id FROM envio_historico WHERE user_id = :uid AND recipient_email = :re AND subject = :sub AND DATE(sent_at) = CURRENT_DATE"), {'uid': user_id, 're': contact.get('Email'), 'sub': subject}).fetchone()
                    if not already_sent_today:
                        print(f"--> Enviando aviso de {days_left} dias para {contact.get('Email')}")
                        send_emails_in_batches([contact], subject, body, user_settings, user_id)


def worker_main_loop():
    print("--- Worker de Fundo Iniciado ---")
    self_url = os.environ.get('RENDER_EXTERNAL_URL')
    while True:
        gevent.sleep(300) # Aguarda 5 minutos
        conn = None
        try:
            print(f"\n[{datetime.now()}] Worker: Iniciando ciclo...")
            if self_url:
                try:
                    requests.get(self_url, timeout=10)
                    print(f"-> Worker: Auto-ping para {self_url} bem-sucedido.")
                except requests.RequestException as e:
                    print(f"-> Worker: Falha no auto-ping: {e}")
            
            conn = get_db_connection()
            active_users = conn.execute(text("SELECT * FROM users WHERE role = 'admin' OR (plan_id IS NOT NULL AND plan_expiration_date >= CURRENT_DATE)")).mappings().fetchall()
            
            if not active_users: print("-> Worker: Nenhum usuário ativo (com plano ou admin) encontrado.")
            else: print(f"-> Worker: Encontrados {len(active_users)} usuários ativos para processar.")
            
            for user in active_users:
                user_settings = dict(user)
                print(f"--- Processando para: {user_settings['email']} ---")
                if user_settings['role'] != 'admin' and not all(user_settings.get(k) for k in ['baserow_host', 'baserow_api_key', 'smtp_user']):
                    print("-> AVISO: Configurações do usuário incompletas. Pulando.")
                    continue
                try:
                    with conn.begin():
                        all_contacts = process_contacts_status(get_all_contacts_from_baserow(user_settings))
                        # CHAMA AS FUNÇÕES DO WORKER
                        worker_process_mass_send_jobs(user_settings, conn)
                        worker_process_pending_schedules(user_settings, all_contacts, conn)
                        worker_check_and_run_automations(user_settings, all_contacts, conn)
                except Exception as e:
                    print(f"--> ERRO ao processar para o usuário {user_settings['email']}: {e}")

            print(f"[{datetime.now()}] Worker: Ciclo concluído.")
        except Exception as e:
            print(f"ERRO CRÍTICO NO WORKER: {e}")
        finally:
            if conn and not conn.closed:
                conn.close()

def start_background_worker():
    print("Iniciando o loop do worker em uma greenlet...")
    worker_main_loop()

print("Disparando greenlet para o background worker...")
gevent.spawn(start_background_worker)

# O Gunicorn assume o controle a partir daqui. Não use app.run()
