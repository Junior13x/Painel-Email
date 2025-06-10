# ===============================================================
# == IMPORTAÇÕES E CONFIGURAÇÕES INICIAIS ==
# ===============================================================
from gevent import monkey
monkey.patch_all() # Deve ser a primeira linha executável

import os
import requests
import re
import json
import smtplib
import time
from email.message import EmailMessage
from functools import wraps
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, g
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import create_engine, text, exc as sqlalchemy_exc
from dotenv import load_dotenv

import gevent
import threading # Usaremos um lock para iniciar o worker apenas uma vez
import mercadopago

# Carrega variáveis de ambiente de um arquivo .env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'uma-chave-secreta-padrao-para-desenvolvimento')

# ===============================================================
# == 1. GESTÃO CENTRALIZADA DO BANCO DE DADOS ==
# ===============================================================

def get_db_engine():
    """Cria e retorna o motor SQLAlchemy, que gerencia o pool de conexões."""
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise ValueError("Variável de ambiente DATABASE_URL não foi configurada.")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return create_engine(db_url, pool_pre_ping=True)

db_engine = get_db_engine()

@app.before_request
def before_request_db_connection():
    """Antes de cada request, pega uma conexão do pool e a armazena em 'g'."""
    if 'db_conn' not in g:
        g.db_conn = db_engine.connect()

@app.teardown_request
def teardown_request_db_connection(exception=None):
    """Ao final de cada request, devolve a conexão para o pool."""
    conn = g.pop('db_conn', None)
    if conn is not None and not conn.closed:
        conn.close()

# ===============================================================
# == 2. LÓGICA DO ROBÔ DE FUNDO (BACKGROUND WORKER) ==
# ===============================================================

def log_to_db_worker(conn, level, message):
    """Função de log que usa a conexão DEDICADA do worker para evitar recursão."""
    try:
        # Usa a conexão já existente, mas em modo autocommit para não interferir nas transações principais.
        conn.execution_options(autocommit=True).execute(
            text("INSERT INTO app_logs (level, message) VALUES (:level, :message)"),
            {'level': level, 'message': str(message)}
        )
    except Exception as e:
        print(f"[WORKER LOG FALLBACK] Level: {level}, Msg: {message}, Err: {e}")

def send_emails_in_batches_worker(conn, user_settings, user_id, recipients, subject, body):
    batch_size, delay_seconds, smtp_port = int(user_settings.get('batch_size') or 15), int(user_settings.get('delay_seconds') or 60), int(user_settings.get('smtp_port') or 587)
    sent_count, fail_count = 0, 0
    log_to_db_worker(conn, 'WORKER_SEND', f"Iniciando envio para {len(recipients)} destinatários do user {user_id}.")
    for i in range(0, len(recipients), batch_size):
        batch = recipients[i:i + batch_size]
        log_to_db_worker(conn, 'WORKER_SEND', f"Processando lote {i//batch_size + 1} com {len(batch)} e-mails.")
        for recipient in batch:
            recipient_email = recipient.get("Email")
            if not recipient_email: continue
            try:
                msg = EmailMessage()
                msg['Subject'], msg['From'], msg['To'] = subject, user_settings.get('smtp_user'), recipient_email
                msg.add_alternative(body, subtype='html')
                with smtplib.SMTP(str(user_settings.get('smtp_host')), smtp_port, timeout=20) as server:
                    server.starttls()
                    server.login(user_settings.get('smtp_user'), user_settings.get('smtp_password'))
                    server.send_message(msg)
                sent_count += 1
                conn.execute(text("INSERT INTO envio_historico (user_id, recipient_email, subject, body) VALUES (:uid, :re, :s, :b)"), {'uid': user_id, 're': recipient_email, 's': subject, 'b': body})
            except Exception as e:
                fail_count += 1
                log_to_db_worker(conn, 'ERROR', f"FALHA SMTP ao enviar para {recipient_email}: {e}")
        if i + batch_size < len(recipients): gevent.sleep(delay_seconds)
    return sent_count, fail_count

def process_user_tasks(conn, user):
    """Processa TODAS as tarefas para UM usuário."""
    user_settings = dict(user)
    user_id = user_settings['id']
    log_to_db_worker(conn, 'INFO', f"Processando tarefas para o usuário: {user_settings['email']}")

    # --- TAREFA 1: Processar Envios em Massa (mass_send_jobs) ---
    with conn.begin(): # Transação para esta tarefa
        job = conn.execute(text("SELECT * FROM mass_send_jobs WHERE user_id = :uid AND status = 'pending' ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"), {'uid': user_id}).mappings().fetchone()
        if job:
            job_id = job['id']
            log_to_db_worker(conn, 'WORKER', f"Iniciando job de envio em massa ID: {job_id}.")
            try:
                conn.execute(text("UPDATE mass_send_jobs SET status = 'processing', processed_at = NOW() WHERE id = :job_id"), {'job_id': job_id})
                recipients = json.loads(job['recipients_json'])
                sent, failed = send_emails_in_batches_worker(conn, user_settings, user_id, recipients, job['subject'], job['body'])
                if user_settings['role'] != 'admin':
                    current_user_state = conn.execute(text("SELECT sends_today, last_send_date FROM users WHERE id = :id FOR UPDATE"), {'id': user_id}).mappings().fetchone()
                    sends_today = current_user_state['sends_today'] if current_user_state and current_user_state['last_send_date'] == datetime.now().date() else 0
                    conn.execute(text("UPDATE users SET sends_today = :st, last_send_date = :lsd WHERE id = :uid"), {'st': sends_today + sent, 'lsd': datetime.now().date(), 'uid': user_id})
                conn.execute(text("UPDATE mass_send_jobs SET status = 'completed', sent_count = :sc, error_message = NULL WHERE id = :job_id"), {'sc': sent, 'job_id': job_id})
                log_to_db_worker(conn, 'WORKER', f"Job ID {job_id} concluído. {sent} enviados, {failed} falhas.")
            except Exception as e:
                error_msg = str(e)
                log_to_db_worker(conn, 'ERROR', f"ERRO CRÍTICO no Job ID {job_id}: {error_msg}")
                conn.execute(text("UPDATE mass_send_jobs SET status = 'failed', error_message = :msg WHERE id = :job_id"), {'msg': error_msg, 'job_id': job_id})
    
    # --- TAREFA 2 & 3: Agendamentos e Automações ---
    if not all(user_settings.get(k) for k in ['baserow_host', 'baserow_api_key', 'smtp_user']):
        log_to_db_worker(conn, 'WARNING', f"Configurações de Baserow/SMTP incompletas para {user_settings['email']}. Pulando agendamentos/automações.")
        return
        
    all_contacts = process_contacts_status(get_all_contacts_from_baserow(user_settings))
    
    with conn.begin(): # Transação para esta tarefa
        pending_emails = conn.execute(text("SELECT * FROM scheduled_emails WHERE user_id = :uid AND is_sent = FALSE AND send_at <= NOW() FOR UPDATE SKIP LOCKED"), {'uid': user_id}).mappings().fetchall()
        for email_job in pending_emails:
            recipients = []
            if email_job['schedule_type'] == 'group':
                target = email_job['status_target']
                if target == 'all': recipients = all_contacts
                else: recipients = [c for c in all_contacts if c.get('status_badge_class') == target]
            elif email_job['schedule_type'] == 'manual' and email_job['manual_recipients']:
                recipients = [{'Email': email.strip()} for email in email_job['manual_recipients'].split(',')]
            if recipients:
                log_to_db_worker(conn, 'WORKER', f"Processando agendamento ID {email_job['id']} para {len(recipients)} destinatário(s)...")
                send_emails_in_batches_worker(conn, user_settings, user_id, recipients, email_job['subject'], email_job['body'])
            conn.execute(text("UPDATE scheduled_emails SET is_sent = TRUE WHERE id = :eid"), {'eid': email_job['id']})

def background_worker_loop():
    """O loop principal do robô. Robusto e com gestão de conexão própria."""
    print("--- 🤖 Robô de Fundo (Greenlet) Iniciado ---")
    while True:
        gevent.sleep(60)
        conn = None
        try:
            conn = db_engine.connect()
            log_to_db_worker(conn, 'WORKER', "Ciclo de verificação do robô iniciado.")
            active_users = conn.execute(text("SELECT * FROM users WHERE role = 'admin' OR (plan_id IS NOT NULL AND plan_expiration_date >= CURRENT_DATE)")).mappings().fetchall()
            for user in active_users:
                try:
                    process_user_tasks(conn, user)
                except Exception as user_error:
                    print(f"Erro ao processar usuário {user['email']}: {user_error}")
        except Exception as loop_error:
            print(f"--- ERRO CRÍTICO NO LOOP DO ROBÔ: {loop_error} ---")
        finally:
            if conn and not conn.closed: conn.close()

# ===============================================================
# == 3. INICIALIZAÇÃO SEGURA DO ROBÔ (UMA ÚNICA VEZ) ==
# ===============================================================
worker_start_lock = threading.Lock()
worker_started = False

def start_worker_once():
    global worker_started
    if not worker_started:
        with worker_start_lock:
            if not worker_started:
                gevent.spawn(background_worker_loop)
                worker_started = True
                print(">>> Robô de fundo disparado com sucesso. <<<")

# Este hook será removido após a primeira execução bem-sucedida.
_initial_request_done = False
@app.before_request
def initial_worker_start_hook():
    global _initial_request_done
    if not _initial_request_done:
        start_worker_once()
        _initial_request_done = True


# ===============================================================
# == 4. LÓGICA DE INICIALIZAÇÃO E HELPERS ==
# ===============================================================

def init_db_logic():
    """Lógica de inicialização que pega uma conexão própria pois roda fora do ciclo de request."""
    conn = None
    try:
        conn = db_engine.connect()
        with conn.begin():
            print("Conectado ao banco de dados. Iniciando a criação das tabelas...")
            conn.execute(text("DROP TABLE IF EXISTS users, features, plans, plan_features, envio_historico, scheduled_emails, email_templates, mass_send_jobs, app_logs CASCADE;"))
            conn.execute(text("CREATE TABLE features (id SERIAL PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, description TEXT);"))
            conn.execute(text("CREATE TABLE plans (id SERIAL PRIMARY KEY, name TEXT NOT NULL, price NUMERIC(10, 2) NOT NULL, validity_days INTEGER NOT NULL, daily_send_limit INTEGER DEFAULT 25, is_active BOOLEAN DEFAULT TRUE);"))
            conn.execute(text("""
                CREATE TABLE users (
                    id SERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', 
                    plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL, plan_expiration_date DATE,
                    baserow_host TEXT, baserow_api_key TEXT, baserow_table_id TEXT, smtp_host TEXT, smtp_port INTEGER, 
                    smtp_user TEXT, smtp_password TEXT, batch_size INTEGER, delay_seconds INTEGER, automations_config TEXT,
                    sends_today INTEGER DEFAULT 0, last_send_date DATE
                );"""))
            conn.execute(text("CREATE TABLE plan_features (plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE, feature_id INTEGER REFERENCES features(id) ON DELETE CASCADE, PRIMARY KEY (plan_id, feature_id));"))
            conn.execute(text("CREATE TABLE envio_historico (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), recipient_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT, sent_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);"))
            conn.execute(text("CREATE TABLE scheduled_emails (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), schedule_type TEXT NOT NULL, status_target TEXT, manual_recipients TEXT, subject TEXT NOT NULL, body TEXT NOT NULL, send_at TIMESTAMP NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_sent BOOLEAN DEFAULT FALSE);"))
            conn.execute(text("CREATE TABLE email_templates (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), name TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(user_id, name));"))
            conn.execute(text("""CREATE TABLE mass_send_jobs (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), subject TEXT NOT NULL, body TEXT NOT NULL, recipients_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error_message TEXT, sent_count INTEGER DEFAULT 0, recipients_count INTEGER DEFAULT 0, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, processed_at TIMESTAMP WITH TIME ZONE);"""))
            conn.execute(text("CREATE TABLE app_logs (id SERIAL PRIMARY KEY, timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, level TEXT, message TEXT);"))
            print("Tabelas criadas com sucesso.")
            
            # --- Dados Iniciais ---
            conn.execute(text("INSERT INTO features (name, slug, description) VALUES ('Envio em Massa e por Status', 'mass-send', 'Permite o envio de e-mails em massa e por status de cliente.');"))
            conn.execute(text("INSERT INTO features (name, slug, description) VALUES ('Agendamentos de Campanhas', 'schedules', 'Permite agendar envios de e-mail para o futuro.');"))
            conn.execute(text("INSERT INTO features (name, slug, description) VALUES ('Automações Inteligentes', 'automations', 'Configura e-mails automáticos de boas-vindas e lembretes de expiração.');"))
            conn.execute(text("INSERT INTO plans (name, price, validity_days, daily_send_limit, is_active) VALUES ('VIP', 99.99, 30, -1, TRUE);"))
            vip_plan_id = conn.execute(text("SELECT id FROM plans WHERE name = 'VIP';")).scalar()
            all_feature_ids = conn.execute(text("SELECT id FROM features;")).mappings().fetchall()
            for feature_id_row in all_feature_ids:
                conn.execute(text("INSERT INTO plan_features (plan_id, feature_id) VALUES (:pid, :fid);"), {'pid': vip_plan_id, 'fid': feature_id_row['id']})
            
            default_email = 'junior@admin.com'
            default_pass = '130896'
            password_hash = generate_password_hash(default_pass)
            conn.execute(text("INSERT INTO users (email, password_hash, role) VALUES (:email, :password_hash, 'admin')"), {'email': default_email, 'password_hash': password_hash})
            print(f"ADMIN PADRÃO CRIADO! E-mail: {default_email}")

    finally:
        if conn and not conn.closed: conn.close()

@app.cli.command('init-db')
def init_db_command(): init_db_logic()

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
            if session.get('role') == 'admin' or feature_slug in session.get('user_features', []):
                return f(*args, **kwargs)
            else:
                flash("Seu plano atual não dá acesso a este recurso.", "danger")
                return redirect(url_for('dashboard'))
        return decorated_function
    return decorator

@app.context_processor
def inject_user_info():
    if 'user_id' not in session: return {}
    conn = g.db_conn
    user = conn.execute(text("SELECT * FROM users WHERE id = :id"), {'id': session['user_id']}).mappings().fetchone()
    if not user:
        session.clear()
        return {}
    
    enabled_features = set()
    if user['role'] == 'admin':
        enabled_features = {row['slug'] for row in conn.execute(text("SELECT slug FROM features")).mappings().fetchall()}
    elif user.get('plan_id') and (not user.get('plan_expiration_date') or user['plan_expiration_date'] >= datetime.now().date()):
        enabled_features = {row['slug'] for row in conn.execute(text("SELECT f.slug FROM features f JOIN plan_features pf ON f.id = pf.feature_id WHERE pf.plan_id = :plan_id"), {'plan_id': user['plan_id']}).mappings().fetchall()}
    session['user_features'] = list(enabled_features)
    return dict(is_admin=(user['role'] == 'admin'), user_features=list(enabled_features))

def parse_date_string(date_string):
    if not date_string: return None
    for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d']:
        try: return datetime.strptime(date_string.strip().split('T')[0], fmt)
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
        except (ValueError, TypeError) as e: print(f"Aviso ao processar contato ID {contact.get('id')}: {e}")
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

# ===============================================================
# == 5. ROTAS DA APLICAÇÃO ==
# ===============================================================
@app.route('/')
def home(): return redirect(url_for('login_page'))

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if 'logged_in' in session: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email, password = request.form.get('email'), request.form.get('password')
        user = g.db_conn.execute(text("SELECT * FROM users WHERE email = :email"), {'email': email}).mappings().fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session.update({'logged_in': True, 'user_id': user['id'], 'user_email': user['email'], 'role': user['role']})
            return redirect(url_for('dashboard'))
        else:
            flash("E-mail ou senha inválidos.", "danger")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if 'logged_in' in session: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email, password = request.form.get('email'), request.form.get('password')
        try:
            with g.db_conn.begin():
                g.db_conn.execute(text("INSERT INTO users (email, password_hash) VALUES (:email, :ph)"), {'email': email, 'ph': generate_password_hash(password)})
            flash("Conta criada com sucesso! Faça seu login.", "success")
            return redirect(url_for('login_page'))
        except sqlalchemy_exc.IntegrityError:
            flash("Este e-mail já está cadastrado.", "danger")
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

@app.route('/users', methods=['GET', 'POST'])
@login_required
@admin_required
def users_page():
    conn = g.db_conn
    if request.method == 'POST':
        email, password, role = request.form.get('email'), request.form.get('password'), request.form.get('role', 'user')
        if email and password:
            try:
                with conn.begin(): conn.execute(text("INSERT INTO users (email, password_hash, role) VALUES (:e, :ph, :r)"), {'e': email, 'ph': generate_password_hash(password), 'r': role})
                flash(f"Usuário '{email}' criado!", "success")
            except sqlalchemy_exc.IntegrityError: flash(f"O e-mail '{email}' já existe.", "danger")
        else: flash("E-mail e senha são obrigatórios.", "warning")
        return redirect(url_for('users_page'))
    users = conn.execute(text("SELECT u.*, p.name as plan_name FROM users u LEFT JOIN plans p ON u.plan_id = p.id ORDER BY u.id ASC")).mappings().fetchall()
    return render_template('users.html', users=users)

@app.route('/users/edit/<int:user_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user_page(user_id):
    conn = g.db_conn
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

@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    if session['user_id'] == user_id:
        flash("Você não pode excluir a própria conta.", "danger")
    else:
        with g.db_conn.begin(): g.db_conn.execute(text("DELETE FROM users WHERE id = :id"), {'id': user_id})
        flash("Usuário excluído.", "info")
    return redirect(url_for('users_page'))

@app.route('/admin/plans', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_plans_page():
    conn = g.db_conn
    if request.method == 'POST':
        name, price, validity, limit = request.form.get('name'), float(request.form.get('price', 0)), int(request.form.get('validity_days', 30)), int(request.form.get('daily_send_limit', 50))
        features = request.form.getlist('features')
        if not name: flash("O nome do plano é obrigatório.", "danger")
        elif not features: flash("Atenção: Você deve selecionar pelo menos um recurso para o plano.", "warning")
        else:
            with conn.begin():
                result = conn.execute(text("INSERT INTO plans (name, price, validity_days, daily_send_limit) VALUES (:n, :p, :v, :l) RETURNING id"), {'n': name, 'p': price, 'v': validity, 'l': limit})
                new_plan_id = result.scalar_one()
                for fid in features: conn.execute(text("INSERT INTO plan_features (plan_id, feature_id) VALUES (:pid, :fid)"), {'pid': new_plan_id, 'fid': int(fid)})
            flash(f"Plano '{name}' criado com sucesso!", "success")
        return redirect(url_for('manage_plans_page'))
    plans = conn.execute(text("SELECT p.*, (SELECT COUNT(*) FROM plan_features pf WHERE pf.plan_id = p.id) as feature_count FROM plans p ORDER BY p.price")).mappings().fetchall()
    features = conn.execute(text("SELECT * FROM features ORDER BY id")).mappings().fetchall()
    return render_template('manage_plans.html', plans=plans, all_features=features)

@app.route('/admin/plans/delete/<int:plan_id>', methods=['POST'])
@login_required
@admin_required
def delete_plan(plan_id):
    try:
        with g.db_conn.begin(): g.db_conn.execute(text("DELETE FROM plans WHERE id = :pid"), {'pid': plan_id})
        flash("Plano excluído com sucesso.", "info")
    except Exception as e: flash(f"Erro ao excluir plano: {e}", "danger")
    return redirect(url_for('manage_plans_page'))

@app.route('/planos')
@login_required
def plans_page():
    conn = g.db_conn
    master_features = conn.execute(text("SELECT * FROM features ORDER BY id")).mappings().fetchall()
    active_plans = conn.execute(text("SELECT * FROM plans WHERE is_active = TRUE ORDER BY price")).mappings().fetchall()
    plans_data = []
    for plan in active_plans:
        feature_ids = {row['feature_id'] for row in conn.execute(text("SELECT feature_id FROM plan_features WHERE plan_id = :pid"), {'pid': plan['id']}).mappings().fetchall()}
        plans_data.append({'plan': plan, 'enabled_feature_ids': feature_ids})
    return render_template('planos.html', plans_data=plans_data, master_features=master_features)

@app.route('/criar-pagamento', methods=['POST'])
@login_required
def create_payment():
    plan_id = request.form.get('plan_id')
    conn = g.db_conn
    plan = conn.execute(text("SELECT * FROM plans WHERE id = :pid"), {'pid': plan_id}).mappings().fetchone()
    access_token = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")
    base_url = os.environ.get('RENDER_EXTERNAL_URL')
    if not all([plan, access_token, base_url]):
        flash("Configuração de pagamento inválida.", "danger")
        return redirect(url_for('plans_page'))
    sdk = mercadopago.SDK(access_token)
    payment_data = { "transaction_amount": float(plan['price']), "description": f"Plano {plan['name']} - {session['user_email']}", "payment_method_id": "pix", "payer": {"email": session["user_email"]}, "notification_url": f"{base_url}{url_for('mp_webhook')}", "external_reference": f"user:{session['user_id']};plan:{plan_id}" }
    try:
        payment_response = sdk.payment().create(payment_data)
        if payment_response and payment_response.get("status") == 201:
            pi = payment_response["response"]['point_of_interaction']['transaction_data']
            return render_template("pagamento_pix.html", pix_code=pi['qr_code'], qr_code_base64=pi['qr_code_base64'])
        else:
            flash(f"Erro ao gerar cobrança: {payment_response.get('response', {}).get('message', 'Erro desconhecido')}", "danger")
            return redirect(url_for('plans_page'))
    except Exception as e:
        flash(f"Erro crítico ao gerar cobrança: {e}", "danger")
        return redirect(url_for('plans_page'))

@app.route('/mercado-pago/webhook', methods=['POST'])
def mp_webhook():
    data = request.json
    access_token = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")
    if data and data.get("action") == "payment.updated" and access_token:
        payment_id = data["data"]["id"]
        sdk = mercadopago.SDK(access_token)
        conn_webhook = None
        try:
            payment_info = sdk.payment().get(payment_id)
            if payment_info["status"] == 200 and payment_info["response"]["status"] == "approved":
                payment = payment_info["response"]
                user_id, plan_id = [int(p.split(':')[1]) for p in payment['external_reference'].split(';')]
                conn_webhook = db_engine.connect()
                with conn_webhook.begin():
                    plan = conn_webhook.execute(text("SELECT * FROM plans WHERE id = :pid"), {'pid': plan_id}).mappings().fetchone()
                    user = conn_webhook.execute(text("SELECT * FROM users WHERE id = :uid"), {'uid': user_id}).mappings().fetchone()
                    if user and plan:
                        expiration_date = datetime.now() + timedelta(days=plan['validity_days'])
                        if user['plan_expiration_date'] and user['plan_expiration_date'] > datetime.now().date():
                            expiration_date = datetime.combine(user['plan_expiration_date'], datetime.min.time()) + timedelta(days=plan['validity_days'])
                        conn_webhook.execute(text("UPDATE users SET plan_id = :pid, plan_expiration_date = :exp WHERE id = :uid"), {'pid': plan_id, 'exp': expiration_date.date(), 'uid': user_id})
        except Exception as e:
            print(f"Erro no webhook: {e}")
        finally:
            if conn_webhook: conn_webhook.close()
    return jsonify({"status": "received"}), 200

@app.route('/envio-em-massa', methods=['GET', 'POST'])
@login_required
@feature_required('mass-send')
def mass_send_page():
    conn, user_id = g.db_conn, session['user_id']
    user_settings = conn.execute(text("SELECT * FROM users WHERE id = :id"), {'id': user_id}).mappings().fetchone()
    if request.method == 'POST':
        if session.get('role') != 'admin' and not all(user_settings.get(k) for k in ['smtp_host', 'smtp_user', 'smtp_password']):
            flash("Configurações de SMTP incompletas.", "danger")
            return redirect(url_for('settings_page'))
        try:
            all_contacts = process_contacts_status(get_all_contacts_from_baserow(user_settings))
            bulk_action, recipients = request.form.get('bulk_action'), []
            if bulk_action and bulk_action != 'manual':
                if bulk_action == 'all': recipients = all_contacts
                else: recipients = [c for c in all_contacts if c.get('status_badge_class') == bulk_action]
            else:
                selected_ids = request.form.getlist('selected_contacts')
                recipients = [c for c in all_contacts if str(c.get('id')) in selected_ids]
            if not recipients:
                flash("Nenhum destinatário selecionado.", "warning")
                return redirect(url_for('mass_send_page'))
            
            subject, body = request.form.get('subject'), request.form.get('body')
            with conn.begin():
                conn.execute(
                    text("INSERT INTO mass_send_jobs (user_id, subject, body, recipients_json, recipients_count) VALUES (:uid, :sub, :body, :rec_json, :rec_count)"),
                    {'uid': user_id, 'sub': subject, 'body': body, 'rec_json': json.dumps(recipients), 'rec_count': len(recipients)}
                )
            flash(f"Campanha para {len(recipients)} destinatários foi agendada! O envio será processado em segundo plano.", "success")
            return redirect(url_for('history_page'))
        except Exception as e:
            flash(f"Erro ao agendar envio: {e}", "danger")
    contacts, error_message, templates = [], None, []
    try:
        if all(user_settings.get(k) for k in ['baserow_host', 'baserow_api_key', 'baserow_table_id']):
            contacts = process_contacts_status(get_all_contacts_from_baserow(user_settings))
        templates = conn.execute(text("SELECT * FROM email_templates WHERE user_id = :uid ORDER BY name"), {'uid': user_id}).mappings().fetchall()
    except Exception as e:
        error_message = f"Não foi possível carregar dados: {e}"
    return render_template('envio_em_massa.html', contacts=contacts, error=error_message, templates=templates)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    conn, user_id = g.db_conn, session['user_id']
    if request.method == 'POST':
        try:
            with conn.begin():
                automations = {'welcome': {'enabled': 'welcome_enabled' in request.form, 'subject': request.form.get('welcome_subject'), 'body': request.form.get('welcome_body')}, 'expiry': {'enabled': 'expiry_enabled' in request.form, 'subject_7_days': request.form.get('expiry_7_days_subject'), 'body_7_days': request.form.get('expiry_7_days_body'), 'subject_3_days': request.form.get('expiry_3_days_subject'), 'body_3_days': request.form.get('expiry_3_days_body'), 'subject_1_day': request.form.get('expiry_1_day_subject'), 'body_1_day': request.form.get('expiry_1_day_body')}}
                update_fields = {k: request.form.get(k) for k in ['baserow_host', 'baserow_api_key', 'baserow_table_id', 'smtp_host', 'smtp_user']}
                update_fields.update({'smtp_port': int(request.form.get('smtp_port') or 587), 'batch_size': int(request.form.get('batch_size') or 15), 'delay_seconds': int(request.form.get('delay_seconds') or 60), 'automations_config': json.dumps(automations)})
                set_clauses = [f"{key} = :{key}" for key in update_fields]
                if request.form.get('smtp_password'):
                    set_clauses.append("smtp_password = :smtp_password")
                    update_fields['smtp_password'] = request.form.get('smtp_password')
                conn.execute(text(f"UPDATE users SET {', '.join(set_clauses)} WHERE id = :user_id"), {**update_fields, 'user_id': user_id})
            flash("Configurações salvas!", "success")
        except Exception as e: flash(f"Erro ao salvar configurações: {e}", "danger")
        return redirect(url_for('settings_page'))
    user_settings_row = conn.execute(text('SELECT * FROM users WHERE id = :uid'), {'uid': user_id}).mappings().fetchone()
    user_settings = dict(user_settings_row) if user_settings_row else {}
    if user_settings.get('automations_config'): user_settings['automations_config'] = json.loads(user_settings['automations_config'])
    else: user_settings['automations_config'] = {}
    return render_template('settings.html', user_settings=user_settings)

@app.route('/history', methods=['GET'])
@login_required
@feature_required('mass-send')
def history_page():
    conn = g.db_conn
    jobs = conn.execute(text("SELECT * FROM mass_send_jobs WHERE user_id = :uid ORDER BY created_at DESC LIMIT 50"), {'uid': session['user_id']}).mappings().fetchall()
    history = conn.execute(text("SELECT * FROM envio_historico WHERE user_id = :uid ORDER BY sent_at DESC LIMIT 100"), {'uid': session['user_id']}).mappings().fetchall()
    return render_template('history.html', jobs=jobs, history=history)

@app.route('/history/details/<int:history_id>')
@login_required
@feature_required('mass-send')
def history_details(history_id):
    entry = g.db_conn.execute(text("SELECT * FROM envio_historico WHERE id = :hid AND user_id = :uid"), {'hid': history_id, 'uid': session['user_id']}).mappings().fetchone()
    if entry:
        entry_dict = dict(entry)
        if 'sent_at' in entry_dict and isinstance(entry_dict['sent_at'], datetime):
            entry_dict['sent_at'] = entry_dict['sent_at'].isoformat()
        return jsonify(entry_dict)
    else:
        return jsonify({'error': 'Registro não encontrado ou sem permissão.'}), 404

@app.route('/history/delete/<int:history_id>', methods=['POST'])
@login_required
@feature_required('mass-send')
def delete_history_entry(history_id):
    with g.db_conn.begin(): g.db_conn.execute(text("DELETE FROM envio_historico WHERE id = :hid AND user_id = :uid"), {'hid': history_id, 'uid': session['user_id']})
    flash("Registro do histórico excluído com sucesso.", "info")
    return redirect(url_for('history_page'))

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
    template_name, subject, body = request.form.get('template_name'), request.form.get('subject'), request.form.get('body')
    if not all([template_name, subject, body]):
        flash("Nome, assunto e corpo são necessários.", "warning")
    else:
        try:
            with g.db_conn.begin(): g.db_conn.execute(text("INSERT INTO email_templates (user_id, name, subject, body) VALUES (:uid, :n, :s, :b)"), {'uid': session['user_id'], 'n': template_name, 's': subject, 'b': body})
            flash(f'Salvo como modelo "{template_name}"!', 'success')
        except sqlalchemy_exc.IntegrityError: flash(f'Um modelo com o nome "{template_name}" já existe.', 'danger')
        except Exception as e: flash(f'Erro ao salvar modelo: {e}', 'danger')
    return redirect(url_for('history_page'))

@app.route('/templates', methods=['GET', 'POST'])
@login_required
@feature_required('mass-send')
def templates_page():
    conn, user_id = g.db_conn, session['user_id']
    if request.method == 'POST':
        name, subject, body = request.form.get('template_name'), request.form.get('subject'), request.form.get('body')
        if name and subject and body:
            try:
                with conn.begin(): conn.execute(text("INSERT INTO email_templates (user_id, name, subject, body) VALUES (:uid, :n, :s, :b)"), {'uid': user_id, 'n': name, 's': subject, 'b': body})
                flash("Template salvo!", "success")
            except sqlalchemy_exc.IntegrityError: flash("Um template com esse nome já existe.", "danger")
        else: flash("Todos os campos são obrigatórios.", "warning")
        return redirect(url_for('templates_page'))
    templates = conn.execute(text("SELECT * FROM email_templates WHERE user_id = :uid ORDER BY name"), {'uid': user_id}).mappings().fetchall()
    return render_template('templates.html', templates=templates)

@app.route('/agendamento', methods=['GET', 'POST'])
@login_required
@feature_required('schedules')
def schedule_page():
    conn, user_id = g.db_conn, session['user_id']
    if request.method == 'POST':
        subject, body, send_at_str = request.form.get('subject'), request.form.get('body'), request.form.get('send_at')
        schedule_type, status_target, manual_recipients = request.form.get('schedule_type'), None, None
        if schedule_type == 'group': status_target = request.form.get('status_target')
        elif schedule_type == 'manual':
            emails = [email.strip() for email in re.split(r'[,\s]+', request.form.get('manual_emails', '')) if email.strip()]
            if not emails:
                flash("Insira ao menos um e-mail válido para agendamento manual.", "warning")
                return redirect(url_for('schedule_page'))
            manual_recipients = ','.join(emails)
        if not all([subject, body, send_at_str, schedule_type]): flash("Todos os campos são obrigatórios para agendar.", "warning")
        else:
            send_at_dt = datetime.strptime(send_at_str, '%Y-%m-%dT%H:%M')
            with conn.begin():
                conn.execute(text("""INSERT INTO scheduled_emails (user_id, schedule_type, status_target, manual_recipients, subject, body, send_at) VALUES (:uid, :st, :stat, :mr, :sub, :body, :sa)"""), {'uid': user_id, 'st': schedule_type, 'stat': status_target, 'mr': manual_recipients, 'sub': subject, 'body': body, 'sa': send_at_dt})
            flash("E-mail agendado com sucesso!", "success")
        return redirect(url_for('schedule_page'))
    pending_emails = conn.execute(text("SELECT * FROM scheduled_emails WHERE user_id = :uid AND is_sent = FALSE ORDER BY send_at ASC"), {'uid': user_id}).mappings().fetchall()
    return render_template('agendamento.html', pending_emails=pending_emails)

@app.route('/agendamento/delete/<int:email_id>', methods=['POST'])
@login_required
@feature_required('schedules')
def delete_schedule(email_id):
    with g.db_conn.begin():
        result = g.db_conn.execute(text("DELETE FROM scheduled_emails WHERE id = :eid AND user_id = :uid"), {'eid': email_id, 'uid': session['user_id']})
    if result.rowcount > 0: flash("Agendamento excluído.", "info")
    else: flash("Agendamento não encontrado ou sem permissão.", "danger")
    return redirect(url_for('schedule_page'))

@app.route('/automations', methods=['GET', 'POST'])
@login_required
@feature_required('automations')
def automations_page():
    user_id = session['user_id']
    conn = g.db_conn
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

@app.route('/admin/logs')
@login_required
@admin_required
def view_logs():
    logs = g.db_conn.execute(text("SELECT * FROM app_logs ORDER BY timestamp DESC LIMIT 200")).mappings().fetchall()
    return render_template('logs.html', logs=logs)

# O Gunicorn assume o controle a partir daqui.
