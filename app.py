import sqlite3
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from functools import wraps
import requests
import re
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import json
import mercadopago
import smtplib
from email.message import EmailMessage
import time
import threading
app = Flask(__name__)
app.secret_key = 'uma-chave-secreta-muito-dificil-de-adivinhar'

# ===============================================================
# == BANCO DE DADOS E INICIALIZAÇÃO ==
# ===============================================================

def get_db_connection():
    """Cria e retorna uma conexão com o banco de dados."""
    conn = sqlite3.connect('painel.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Adiciona as colunas de configuração e plano à tabela de usuários, se não existirem.
    user_columns = [
        ('plan', "TEXT NOT NULL DEFAULT 'free'"), ('plan_start_date', "DATE"), ('plan_validity_days', "INTEGER"),
        ('baserow_host', "TEXT"), ('baserow_api_key', "TEXT"), ('baserow_table_id', "TEXT"),
        ('smtp_host', "TEXT"), ('smtp_port', "INTEGER"), ('smtp_user', "TEXT"), ('smtp_password', "TEXT"),
        ('batch_size', "INTEGER"), ('delay_seconds', "INTEGER"), ('automations_config', "TEXT")
    ]
    
    # Verifica a existência da tabela users antes de tentar alterá-la
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users';")
    table_exists = cursor.fetchone()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
            plan TEXT NOT NULL DEFAULT 'free', plan_start_date DATE, plan_validity_days INTEGER,
            baserow_host TEXT, baserow_api_key TEXT, baserow_table_id TEXT,
            smtp_host TEXT, smtp_port INTEGER, smtp_user TEXT, smtp_password TEXT,
            batch_size INTEGER, delay_seconds INTEGER, automations_config TEXT
        )""")
    
    # Adiciona colunas apenas se a tabela já existia (para migrações)
    if table_exists:
        for col_name, col_type in user_columns:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type};")
            except sqlite3.OperationalError: pass

    # --- LÓGICA PARA CRIAR ADMIN PADRÃO ---
    cursor.execute("SELECT COUNT(id) FROM users WHERE role = 'admin'")
    admin_count = cursor.fetchone()[0]
    if admin_count == 0:
        print("Nenhum administrador encontrado. Criando usuário admin padrão...")
        default_email = 'junior@admin.com'
        default_pass = '130896'
        password_hash = generate_password_hash(default_pass)
        cursor.execute(
            "INSERT INTO users (email, password_hash, role, plan) VALUES (?, ?, 'admin', 'vip')",
            (default_email, password_hash)
        )
        print(f"Usuário admin criado: {default_email} / Senha: {default_pass}")
    
    try: cursor.execute("ALTER TABLE envio_historico ADD COLUMN body TEXT;")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE scheduled_emails ADD COLUMN user_id INTEGER;")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE email_templates ADD COLUMN user_id INTEGER;")
    except sqlite3.OperationalError: pass
        
    cursor.execute("CREATE TABLE IF NOT EXISTS envio_historico (id INTEGER PRIMARY KEY, user_id INTEGER, recipient_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT, sent_at TEXT NOT NULL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS scheduled_emails (id INTEGER PRIMARY KEY, user_id INTEGER, schedule_type TEXT NOT NULL, status_target TEXT, manual_recipients TEXT, subject TEXT NOT NULL, body TEXT NOT NULL, send_at DATETIME NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, is_sent BOOLEAN DEFAULT FALSE)")
    cursor.execute("CREATE TABLE IF NOT EXISTS email_templates (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(user_id, name))")
    conn.commit()
    conn.close()

# ===============================================================
# == FUNÇÕES DE LÓGICA (HELPERS) ==
# ===============================================================

@app.context_processor
def inject_user_send_limit():
    """
    Injeta o limite de envio diário do usuário em todos os templates.
    Isso evita ter que calcular isso em cada rota.
    """
    if 'user_id' not in session:
        return {} # Retorna um dicionário vazio se o usuário não estiver logado

    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
    
    if not user:
        conn.close(); return {}

    # Admin tem envios ilimitados
    if user['role'] == 'admin':
        conn.close(); return dict(daily_limit=-1, sends_remaining=-1)

    plan = conn.execute("SELECT daily_send_limit FROM plans WHERE id = ?", (user['plan_id'],)).fetchone() if user['plan_id'] else None
    
    # Se o usuário não tem plano (Free) ou o plano não tem limite definido
    daily_limit = plan['daily_send_limit'] if plan else 25

    if daily_limit == -1:
        conn.close(); return dict(daily_limit=-1, sends_remaining=-1)

    sends_today = 0
    if user['last_send_date'] == datetime.now().strftime('%Y-%m-%d'):
        sends_today = user['sends_today']
    
    sends_remaining = daily_limit - sends_today
    conn.close()
    
    return dict(daily_limit=daily_limit, sends_remaining=sends_remaining)

def load_settings(user_id=None):
    """Carrega as configurações do usuário e globais, dando prioridade às variáveis de ambiente."""
    settings = {}
    
    # **NOVA LÓGICA:** Carrega o token a partir das variáveis de ambiente primeiro
    mp_token = os.environ.get('MERCADO_PAGO_ACCESS_TOKEN')
    if mp_token:
        settings['MERCADO_PAGO_ACCESS_TOKEN'] = mp_token

    conn = get_db_connection()
    if user_id:
        user_data = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user_data:
            user_keys = user_data.keys()
            db_keys = ['baserow_host', 'baserow_api_key', 'baserow_table_id', 'smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'batch_size', 'delay_seconds']
            for key in db_keys:
                if key in user_keys: settings[key.upper()] = user_data[key]
    
    # Tenta carregar do ficheiro JSON apenas se a variável de ambiente não existir
    # Isto mantém o funcionamento local
    if 'MERCADO_PAGO_ACCESS_TOKEN' not in settings:
        try:
            with open('settings.json', 'r') as f:
                settings.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError): pass
    
    conn.close()
    return settings

# --- Funções de processamento e envio ---
def get_all_contacts_from_baserow(settings):
    """Busca TODOS os contatos, navegando por todas as páginas."""
    all_rows = []
    page = 1
    base_url = f"{settings.get('BASEROW_HOST', '')}/api/database/rows/table/{settings.get('BASEROW_TABLE_ID', '')}/?user_field_names=true&size=200"
    headers = {"Authorization": f"Token {settings.get('BASEROW_API_KEY', '')}"}
    if not all([settings.get(k) for k in ['BASEROW_HOST', 'BASEROW_API_KEY', 'BASEROW_TABLE_ID']]):
        raise Exception("Configurações do Baserow incompletas.")
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
        except requests.exceptions.RequestException as e:
            print(f"Erro ao buscar página {page} do Baserow: {e}")
            raise e
    return all_rows

def process_contacts_status(contacts):
    """Simulação da lógica que calcula status (Vip, Expirando, etc)."""
    # Adapte esta função para suas regras de negócio do Baserow
    return contacts

def send_emails_in_batches(recipients, subject, body, settings, user_id):
    """Envia e-mails em lotes controlados para evitar spam."""
    batch_size = int(settings.get('BATCH_SIZE', 15))
    delay_seconds = int(settings.get('DELAY_SECONDS', 60))
    sent_count, fail_count = 0, 0
    for i in range(0, len(recipients), batch_size):
        batch = recipients[i:i + batch_size]
        for recipient in batch:
            recipient_email = recipient.get("Email")
            if recipient_email:
                try:
                    msg = EmailMessage()
                    msg['Subject'] = subject
                    msg['From'] = settings.get('SMTP_USER')
                    msg['To'] = recipient_email
                    msg.add_alternative(body, subtype='html')
                    with smtplib.SMTP(str(settings.get('SMTP_HOST')), int(settings.get('SMTP_PORT', 587))) as server:
                        server.starttls()
                        server.login(settings.get('SMTP_USER'), settings.get('SMTP_PASSWORD'))
                        server.send_message(msg)
                    sent_count += 1
                except Exception as e:
                    fail_count += 1
                    print(f"FALHA ao enviar para {recipient_email}: {e}")
        if i + batch_size < len(recipients):
            time.sleep(delay_seconds)
    return sent_count, fail_count


# ===============================================================
# == DECORATORS (SISTEMA DE PERMISSÃO) ==
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
    """Decorator que verifica se o plano do usuário tem acesso a um recurso específico."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session: return redirect(url_for('login_page'))
            conn = get_db_connection()
            user = conn.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
            if user and user['role'] == 'admin':
                conn.close()
                return f(*args, **kwargs)
            if not user or not user['plan_id']:
                flash("Você precisa de um plano para acessar este recurso.", "warning")
                conn.close()
                return redirect(url_for('plans_page'))
            if user['plan_expiration_date'] and datetime.strptime(user['plan_expiration_date'], '%Y-%m-%d') < datetime.now():
                flash("Seu plano expirou. Renove para continuar.", "warning")
                conn.close()
                return redirect(url_for('plans_page'))
            feature_access = conn.execute("SELECT 1 FROM plan_features pf JOIN features f ON pf.feature_id = f.id WHERE pf.plan_id = ? AND f.slug = ?", (user['plan_id'], feature_slug)).fetchone()
            conn.close()
            if feature_access: return f(*args, **kwargs)
            else:
                flash("Seu plano atual não dá acesso a este recurso. Considere fazer um upgrade!", "danger")
                return redirect(url_for('plans_page'))
        return decorated_function
    return decorator

# ===============================================================
# == ROTAS DA APLICAÇÃO ==
# ===============================================================

# --- ROTAS DE AUTENTICAÇÃO E DASHBOARD ---
@app.route('/')
def home():
    return redirect(url_for('login_page'))

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['logged_in'] = True
            session['user_id'] = user['id']
            session['user_email'] = user['email']
            session['role'] = user['role']
            return redirect(url_for('dashboard'))
        flash("E-mail ou senha inválidos.", "danger")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        conn = get_db_connection()
        try:
            password_hash = generate_password_hash(password)
            conn.execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, 'user')", (email, password_hash))
            conn.commit()
            flash("Conta criada com sucesso! Faça seu login.", "success")
            return redirect(url_for('login_page'))
        except sqlite3.IntegrityError:
            flash("Este e-mail já está cadastrado.", "danger")
        finally:
            conn.close()
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    session.clear()
    flash("Você saiu com sucesso.", "info")
    return redirect(url_for('login_page'))

@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
    plan_status = {'plan_name': 'Free', 'badge_class': 'warning', 'days_left': None}
    enabled_features = set()
    if user and user['role'] == 'admin':
        all_features = conn.execute("SELECT slug FROM features").fetchall()
        enabled_features = {row['slug'] for row in all_features}
        plan_status = {'plan_name': 'Admin', 'badge_class': 'danger', 'days_left': 9999}
    elif user and user['plan_id'] and user['plan_expiration_date']:
        expiration_date = datetime.strptime(user['plan_expiration_date'], '%Y-%m-%d')
        days_left = (expiration_date.date() - datetime.now().date()).days
        plan = conn.execute("SELECT name FROM plans WHERE id = ?", (user['plan_id'],)).fetchone()
        plan_name = plan['name'] if plan else 'Expirado'
        if days_left >= 0:
            plan_status = {'plan_name': plan_name, 'badge_class': 'success', 'days_left': days_left}
            feature_rows = conn.execute("SELECT f.slug FROM features f JOIN plan_features pf ON f.id = pf.feature_id WHERE pf.plan_id = ?", (user['plan_id'],)).fetchall()
            enabled_features = {row['slug'] for row in feature_rows}
        else:
            plan_status = {'plan_name': f"{plan_name} (Expirado)", 'badge_class': 'danger', 'days_left': days_left}
    conn.close()
    session['user_plan_status'] = plan_status
    session['user_features'] = list(enabled_features)
    return render_template('dashboard.html')

# --- ROTAS DE ADMINISTRAÇÃO ---
@app.route('/users', methods=['GET', 'POST'])
def users_page():
    conn = get_db_connection()
    user_count = conn.execute("SELECT count(id) FROM users").fetchone()[0]
    if user_count > 0:
        if 'logged_in' not in session:
            flash("Por favor, faça login para acessar esta página.", "warning"); conn.close(); return redirect(url_for('login_page'))
        if session.get('role') != 'admin':
            flash("Você não tem permissão para acessar esta página.", "danger"); conn.close(); return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email, password, role = request.form.get('email'), request.form.get('password'), request.form.get('role', 'user')
        if not email or not password: flash("E-mail e senha são obrigatórios.", "warning")
        else:
            try:
                conn.execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",(email, generate_password_hash(password), role))
                conn.commit()
                flash(f"Usuário '{email}' criado com sucesso!", "success")
            except sqlite3.IntegrityError: flash(f"O e-mail '{email}' já está cadastrado.", "danger")
        return redirect(url_for('users_page'))
    
    # LÓGICA ATUALIZADA PARA INCLUIR A CONTAGEM DE ENVIOS
    users_raw = conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
    users_list = []
    today_str = datetime.now().strftime('%Y-%m-%d')
    for user_row in users_raw:
        user_data = dict(user_row)
        if user_data.get('last_send_date') == today_str:
            user_data['sends_today_count'] = user_data['sends_today']
        else:
            user_data['sends_today_count'] = 0
        users_list.append(user_data)
            
    conn.close()
    return render_template('users.html', users=users_list)

@app.route('/users/edit/<int:user_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user_page(user_id):
    conn = get_db_connection()
    if request.method == 'POST':
        plan_id_str, validity_days_str = request.form.get('plan_id'), request.form.get('validity_days')
        if not plan_id_str or plan_id_str == 'free':
            conn.execute("UPDATE users SET plan_id = NULL, plan_expiration_date = NULL WHERE id = ?", (user_id,))
        else:
            validity_days = int(validity_days_str) if validity_days_str and validity_days_str.isdigit() else 30
            expiration_date = datetime.now() + timedelta(days=validity_days)
            conn.execute("UPDATE users SET plan_id = ?, plan_expiration_date = ? WHERE id = ?", (int(plan_id_str), expiration_date.strftime('%Y-%m-%d'), user_id))
        conn.commit()
        flash(f"Usuário ID {user_id} atualizado com sucesso!", "success")
        conn.close()
        return redirect(url_for('users_page'))
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    all_plans = conn.execute("SELECT * FROM plans WHERE is_active = 1").fetchall()
    conn.close()
    if not user:
        flash("Usuário não encontrado.", "warning"); return redirect(url_for('users_page'))
    return render_template('edit_user.html', user=user, all_plans=all_plans)

@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    if session.get('user_id') == user_id:
        flash("Você não pode excluir sua própria conta.", "danger")
    else:
        conn = get_db_connection()
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()
        flash("Usuário excluído com sucesso.", "info")
    return redirect(url_for('users_page'))

@app.route('/admin/plans', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_plans_page():
    conn = get_db_connection()
    if request.method == 'POST':
        name = request.form.get('name')
        price = float(request.form.get('price', 0))
        validity_days = int(request.form.get('validity_days', 30))
        daily_send_limit = int(request.form.get('daily_send_limit', 50))
        if not name:
            flash("O nome do plano é obrigatório.", "danger")
            return redirect(url_for('manage_plans_page'))
        cursor = conn.cursor()
        cursor.execute("INSERT INTO plans (name, price, validity_days, daily_send_limit) VALUES (?, ?, ?, ?)", (name, price, validity_days, daily_send_limit))
        new_plan_id = cursor.lastrowid
        selected_features_ids = request.form.getlist('features')
        conn.execute("DELETE FROM plan_features WHERE plan_id = ?", (new_plan_id,))
        for feature_id in selected_features_ids:
            conn.execute("INSERT INTO plan_features (plan_id, feature_id) VALUES (?, ?)", (new_plan_id, int(feature_id)))
        conn.commit()
        flash(f"Plano '{name}' criado com sucesso!", "success")
        return redirect(url_for('manage_plans_page'))
    plans = conn.execute("SELECT * FROM plans ORDER BY price").fetchall()
    features = conn.execute("SELECT * FROM features ORDER BY id").fetchall()
    conn.close()
    return render_template('manage_plans.html', plans=plans, all_features=features)

@app.route('/admin/plans/delete/<int:plan_id>', methods=['POST'])
@login_required
@admin_required
def delete_plan(plan_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    conn.commit()
    conn.close()
    flash("Plano excluído com sucesso.", "info")
    return redirect(url_for('manage_plans_page'))

@app.route('/planos')
@login_required
def plans_page():
    conn = get_db_connection()
    master_features = conn.execute("SELECT * FROM features ORDER BY id").fetchall()
    active_plans = conn.execute("SELECT * FROM plans WHERE is_active = 1 ORDER BY price").fetchall()
    plans_data = []
    for plan in active_plans:
        enabled_features_rows = conn.execute("SELECT feature_id FROM plan_features WHERE plan_id = ?", (plan['id'],)).fetchall()
        enabled_feature_ids = {row['feature_id'] for row in enabled_features_rows}
        plans_data.append({'plan': plan, 'enabled_feature_ids': enabled_feature_ids})
    conn.close()
    return render_template('planos.html', plans_data=plans_data, master_features=master_features)

@app.route('/criar-pagamento', methods=['POST'])
@login_required
def create_payment():
    plan_id = request.form.get('plan_id')
    if not plan_id:
        flash("Plano inválido.", "danger"); return redirect(url_for('plans_page'))
    conn = get_db_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if not plan: return redirect(url_for('plans_page'))
    settings = load_settings()
    access_token = settings.get("MERCADO_PAGO_ACCESS_TOKEN")
    if not access_token:
        flash("Credenciais de pagamento não configuradas.", "danger"); return redirect(url_for('plans_page'))
    sdk = mercadopago.SDK(access_token)
    # ATENÇÃO: Substitua pela sua URL do ngrok durante os testes
    ngrok_url = "https://8f92-2804-7f0-3d7-375-2184-5644-9ccb-c872.ngrok-free.app"
    payment_data = {"transaction_amount": float(plan['price']), "description": f"Assinatura {plan['name']}", "payment_method_id": "pix", "payer": { "email": session.get("user_email") }, "notification_url": f"{ngrok_url}{url_for('mp_webhook')}", "external_reference": f"user:{session['user_id']};plan:{plan_id}"}
    try:
        payment_response = sdk.payment().create(payment_data)
        if payment_response and payment_response.get("status") == 201:
            payment = payment_response["response"]
            return render_template("pagamento_pix.html", pix_code=payment['point_of_interaction']['transaction_data']['qr_code'], qr_code_base64=payment['point_of_interaction']['transaction_data']['qr_code_base64'])
        else:
            flash(f"Erro ao gerar pagamento: {payment_response.get('response', {}).get('message', 'Erro desconhecido do Mercado Pago.')}", "danger"); return redirect(url_for('plans_page'))
    except Exception as e:
        flash("Ocorreu um erro crítico ao gerar a cobrança Pix.", "danger"); return redirect(url_for('plans_page'))

@app.route('/mercado-pago/webhook', methods=['POST'])
def mp_webhook():
    data = request.json
    if data and data.get("action") == "payment.updated":
        payment_id = data.get("data", {}).get("id")
        settings = load_settings()
        sdk = mercadopago.SDK(settings.get("MERCADO_PAGO_ACCESS_TOKEN"))
        try:
            payment_info = sdk.payment().get(payment_id)
            if payment_info["status"] == 200 and payment_info["response"]["status"] == "approved":
                payment = payment_info["response"]
                external_ref = payment.get('external_reference')
                ref_parts = dict(part.split(':') for part in external_ref.split(';'))
                user_id, plan_id = int(ref_parts.get('user')), int(ref_parts.get('plan'))
                conn = get_db_connection()
                plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
                user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if user and plan:
                    expiration_date = datetime.now() + timedelta(days=plan['validity_days'])
                    if user['plan_expiration_date']:
                        current_expiration = datetime.strptime(user['plan_expiration_date'], '%Y-%m-%d')
                        if current_expiration > datetime.now(): expiration_date = current_expiration + timedelta(days=plan['validity_days'])
                    conn.execute("UPDATE users SET plan_id = ?, plan_expiration_date = ? WHERE id = ?", (plan_id, expiration_date.strftime('%Y-%m-%d'), user_id))
                    conn.commit()
                conn.close()
        except Exception as e: print(f"Erro no webhook: {e}")
    return jsonify({"status": "received"}), 200

# --- ROTAS DE FUNCIONALIDADES ---
@app.route('/envio-em-massa', methods=['GET', 'POST'])
@login_required
def mass_send_page():
    user_id = session['user_id']
    if request.method == 'POST':
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        
        # A lógica de limite agora usa o context_processor, mas a verificação é feita aqui
        limit_info = inject_user_send_limit()
        daily_limit = limit_info.get('daily_limit')
        sends_remaining = limit_info.get('sends_remaining')

        if user and user['role'] != 'admin' and user['plan_id'] and user['plan_expiration_date']:
            if datetime.strptime(user['plan_expiration_date'], '%Y-%m-%d') < datetime.now():
                flash("Seu plano expirou.", "warning"); conn.close(); return redirect(url_for('plans_page'))
        
        settings = load_settings(user_id)
        if not all([settings.get(k) for k in ['SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'BASEROW_HOST']]):
             flash("Configurações incompletas.", "danger"); conn.close(); return redirect(url_for('mass_send_page'))
        
        try:
            # ... (Lógica para determinar a lista de `recipients`)
            all_contacts_raw = get_all_contacts_from_baserow(settings)
            all_contacts_processed = process_contacts_status(all_contacts_raw)
            bulk_action = request.form.get('bulk_action')
            recipients = []
            if bulk_action and bulk_action != 'manual':
                if bulk_action == 'all': recipients = all_contacts_processed
                else: recipients = [c for c in all_contacts_processed if c.get('status_badge_class') == bulk_action]
            else:
                selected_ids = request.form.getlist('selected_contacts')
                recipients = [c for c in all_contacts_processed if str(c.get('id')) in selected_ids]
            
            if not recipients:
                flash("Nenhum destinatário selecionado.", "warning"); conn.close(); return redirect(url_for('mass_send_page'))

            if daily_limit != -1:
                if len(recipients) > sends_remaining:
                    flash(f"Ação bloqueada. O envio de {len(recipients)} e-mails ultrapassaria seu limite diário. Você tem {sends_remaining} envios restantes.", "danger"); conn.close(); return redirect(url_for('mass_send_page'))
            
            subject, body = request.form.get('subject'), request.form.get('body')
            sent_count, _ = send_emails_in_batches(recipients, subject, body, settings, user_id)
            
            if daily_limit != -1 and sent_count > 0:
                today_str = datetime.now().strftime('%Y-%m-%d')
                sends_today = user['sends_today'] if user['last_send_date'] == today_str else 0
                conn.execute("UPDATE users SET sends_today = ?, last_send_date = ? WHERE id = ?", (sends_today + sent_count, today_str, user_id))
                conn.commit()
            flash(f"Envio concluído para {sent_count} destinatários.", "success")
        except Exception as e: flash(f"Erro no envio: {e}", "danger")
        finally: conn.close()
        return redirect(url_for('mass_send_page'))

    # Lógica GET
    settings = load_settings(user_id)
    contacts, error, templates = [], None, []
    try: contacts = process_contacts_status(get_all_contacts_from_baserow(settings))
    except Exception as e: error = f"Erro ao carregar contatos: {e}"
    conn = get_db_connection()
    templates = conn.execute("SELECT * FROM email_templates WHERE user_id = ? ORDER BY name", (user_id,)).fetchall()
    conn.close()
    return render_template('envio_em_massa.html', contacts=contacts, error=error, templates=templates)

@app.route('/agendamento', methods=['GET', 'POST'])
@login_required
@feature_required('schedules')
def schedule_page():
    if request.method == 'POST':
        schedule_type = request.form.get('schedule_type')
        subject = request.form.get('subject')
        body = request.form.get('body')
        send_at_str = request.form.get('send_at')
        status_target, manual_recipients = None, None
        if schedule_type == 'group':
            status_target = request.form.get('status_target')
            if not status_target:
                flash("Por favor, selecione um grupo para agendar.", "danger")
                return redirect(url_for('schedule_page'))
        elif schedule_type == 'manual':
            manual_recipients_raw = request.form.get('manual_emails', '')
            emails = [email.strip() for email in re.split(r'[,\s]+', manual_recipients_raw) if email.strip()]
            if not emails:
                flash("Por favor, insira pelo menos um e-mail válido.", "danger")
                return redirect(url_for('schedule_page'))
            manual_recipients = ','.join(emails)
        
        if not all([schedule_type, subject, body, send_at_str]):
            flash("Todos os campos são obrigatórios para agendar.", "danger")
        else:
            try:
                conn = sqlite3.connect('painel.db')
                conn.execute("INSERT INTO scheduled_emails (schedule_type, status_target, manual_recipients, subject, body, send_at) VALUES (?, ?, ?, ?, ?, ?)", (schedule_type, status_target, manual_recipients, subject, body, send_at_str))
                conn.commit()
                flash("E-mail agendado com sucesso!", "success")
            except Exception as e:
                flash(f"Erro ao salvar agendamento: {e}", "danger")
            finally:
                if conn: conn.close()
        return redirect(url_for('schedule_page'))

    conn = sqlite3.connect('painel.db')
    conn.row_factory = sqlite3.Row
    pending_emails = conn.execute("SELECT * FROM scheduled_emails WHERE is_sent = FALSE ORDER BY send_at ASC").fetchall()
    conn.close()
    return render_template('agendamento.html', pending_emails=pending_emails, schedule_data=None)

@app.route('/agendamento/edit/<int:email_id>', methods=['GET', 'POST'])
@login_required
@feature_required('schedules')
def edit_schedule(email_id):
    if request.method == 'POST':
        schedule_type = request.form.get('schedule_type')
        subject = request.form.get('subject')
        body = request.form.get('body')
        send_at_str = request.form.get('send_at')
        status_target, manual_recipients = None, None
        if schedule_type == 'group':
            status_target = request.form.get('status_target')
        elif schedule_type == 'manual':
            manual_recipients_raw = request.form.get('manual_emails', '')
            emails = [email.strip() for email in re.split(r'[,\s]+', manual_recipients_raw) if email.strip()]
            manual_recipients = ','.join(emails)

        try:
            conn = sqlite3.connect('painel.db')
            conn.execute("UPDATE scheduled_emails SET schedule_type = ?, status_target = ?, manual_recipients = ?, subject = ?, body = ?, send_at = ? WHERE id = ?", (schedule_type, status_target, manual_recipients, subject, body, send_at_str, email_id))
            conn.commit()
            flash("Agendamento atualizado com sucesso!", "success")
        except Exception as e:
            flash(f"Erro ao atualizar agendamento: {e}", "danger")
        finally:
            if conn: conn.close()
        return redirect(url_for('schedule_page'))

    conn = sqlite3.connect('painel.db')
    conn.row_factory = sqlite3.Row
    schedule_data = conn.execute("SELECT * FROM scheduled_emails WHERE id = ?", (email_id,)).fetchone()
    pending_emails = conn.execute("SELECT * FROM scheduled_emails WHERE is_sent = FALSE ORDER BY send_at ASC").fetchall()
    conn.close()

    if not schedule_data:
        flash("Agendamento não encontrado.", "danger")
        return redirect(url_for('schedule_page'))
    
    return render_template('agendamento.html', schedule_data=schedule_data, pending_emails=pending_emails)

@app.route('/agendamento/delete/<int:email_id>', methods=['POST'])
@login_required
@feature_required('schedules')
def delete_schedule(email_id):
    try:
        conn = sqlite3.connect('painel.db')
        conn.execute("DELETE FROM scheduled_emails WHERE id = ?", (email_id,))
        conn.commit()
        flash("Agendamento excluído com sucesso.", "info")
    except Exception as e:
        flash(f"Erro ao excluir agendamento: {e}", "danger")
    finally:
        if conn: conn.close()
    return redirect(url_for('schedule_page'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    user_id = session['user_id']
    is_admin = session.get('role') == 'admin'

    # ---- Lógica para quando o formulário é ENVIADO (POST) ----
    if request.method == 'POST':
        conn = get_db_connection()
        try:
            # Se for admin, salva o token do Mercado Pago no settings.json
            if is_admin:
                global_settings = {}
                try:
                    with open('settings.json', 'r') as f:
                        global_settings = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass # Se o arquivo não existe ou está vazio, ignora
                
                # Usa .get() para evitar erro se o campo não for enviado
                global_settings['MERCADO_PAGO_ACCESS_TOKEN'] = request.form.get('mp_access_token', '')

                
                with open('settings.json', 'w') as f:
                    json.dump(global_settings, f, indent=4)

            # Salva as configurações individuais do usuário no banco de dados
            conn.execute("""
                UPDATE users SET
                baserow_host = ?, baserow_api_key = ?, baserow_table_id = ?,
                smtp_host = ?, smtp_port = ?, smtp_user = ?, smtp_password = ?,
                batch_size = ?, delay_seconds = ?
                WHERE id = ?
            """, (
                request.form.get('baserow_host'), request.form.get('baserow_api_key'), request.form.get('baserow_table_id'),
                request.form.get('smtp_host'), int(request.form.get('smtp_port') or 587), request.form.get('smtp_user'), request.form.get('smtp_password'),
                int(request.form.get('batch_size') or 15), int(request.form.get('delay_seconds') or 60),
                user_id
            ))
            conn.commit()
            flash("Configurações salvas com sucesso!", "success")

        except Exception as e:
            conn.rollback() # Desfaz a transação em caso de erro
            flash(f"Erro ao salvar configurações: {e}", "danger")
        finally:
            conn.close()
        
        return redirect(url_for('settings_page'))

    # ---- Lógica para quando a página é CARREGADA (GET) ----
    conn = get_db_connection()
    user_settings = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()

    global_settings = {}
    if is_admin:
        try:
            with open('settings.json', 'r') as f:
                global_settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass # Se o arquivo não existe ou está corrompido, não quebra a página

    return render_template('settings.html', user_settings=user_settings, global_settings=global_settings)

def save_settings(settings):
    """Salva um dicionário de configurações no arquivo settings.json"""
    with open('settings.json', 'w') as f:
        json.dump(settings, f, indent=2)

def parse_date_string(date_string):
    """Tenta analisar uma string de data com vários formatos comuns."""
    if not date_string: return None
    formats_to_try = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d']
    for fmt in formats_to_try:
        try:
            return datetime.strptime(date_string.strip(), fmt)
        except (ValueError, TypeError):
            continue
    return None

def process_contacts_status(contacts):
    """Calcula os dias restantes e atribui status a cada contato."""
    processed_contacts = []
    today = datetime.now()
    for contact in contacts:
        status_text, status_badge_class, dias_restantes_calculado, remaining_days_int = 'Status Indefinido', 'secondary', 'N/A', 99999
        dias_validade_str = contact.get('Dias')
        pagamento_str = contact.get('Pagamento')
        try:
            if dias_validade_str is not None and int(dias_validade_str) == 1:
                status_text, status_badge_class, dias_restantes_calculado, remaining_days_int = 'Expirado / Free', 'danger', 'FREE', -999
            elif pagamento_str and dias_validade_str:
                pagamento_date = parse_date_string(pagamento_str)
                if pagamento_date:
                    dias_validade = int(dias_validade_str)
                    expiration_date = pagamento_date + timedelta(days=dias_validade)
                    remaining_days = (expiration_date.date() - today.date()).days
                    remaining_days_int = remaining_days
                    dias_restantes_calculado = f"{remaining_days} dia(s)"
                    if remaining_days < 0:
                        status_text, status_badge_class = 'Expirado / Free', 'danger'
                    elif remaining_days <= 7:
                        status_text, status_badge_class = 'Expirando', 'warning'
                    else:
                        status_text, status_badge_class = 'Vip / Em Dia', 'success'
        except (ValueError, TypeError) as e:
            print(f"Aviso: Não foi possível processar data/dias para contato ID {contact.get('id')}. Erro: {e}")

        contact.update({
            'status_text': status_text, 'status_badge_class': status_badge_class,
            'dias_restantes_calculado': dias_restantes_calculado, 'remaining_days_int': remaining_days_int
        })
        processed_contacts.append(contact)
    return processed_contacts

def get_all_contacts_from_baserow(settings):
    """Busca TODOS os contatos, navegando por todas as páginas."""
    all_rows = []
    page = 1
    while True:
        try:
            # Usa .get() com valor padrão para evitar erros se a chave não existir
            base_url = f"{settings.get('BASEROW_HOST', '')}/api/database/rows/table/{settings.get('BASEROW_TABLE_ID', '')}/?user_field_names=true&size=200&page={page}"
            headers = {"Authorization": f"Token {settings.get('BASEROW_API_KEY', '')}"}
            response = requests.get(base_url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            rows = data.get('results', [])
            if not rows: break
            all_rows.extend(rows)
            if not data.get('next'): break
            page += 1
        except requests.exceptions.RequestException as e:
            print(f"Erro ao buscar página {page} do Baserow: {e}")
            raise e # Lança a exceção para ser tratada na rota
    return all_rows

def _send_single_email(recipient_email, subject, body, settings, user_id):
    """Função privada para enviar um único e-mail e registrar no histórico."""
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = settings.get('SMTP_USER')
    msg['To'] = recipient_email
    msg.add_alternative(body, subtype='html')
    
    with smtplib.SMTP(str(settings.get('SMTP_HOST')), int(settings.get('SMTP_PORT', 587))) as server:
        server.starttls()
        server.login(settings.get('SMTP_USER'), settings.get('SMTP_PASSWORD'))
        server.send_message(msg)

    # A conexão com o banco deve ser feita com a função do app.py para consistência
    # No entanto, para manter este arquivo independente, usamos uma conexão local
    try:
        conn = sqlite3.connect('painel.db')
        cursor = conn.cursor()
        timestamp = datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO envio_historico (user_id, recipient_email, subject, body, sent_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, recipient_email, subject, body, timestamp)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"ERRO CRÍTICO ao gravar no histórico: {e}")

def send_emails_in_batches(recipients, subject, body, settings, user_id):
    """Envia e-mails em lotes controlados para evitar spam."""
    # CORREÇÃO: Lida com valores None do banco de dados antes de converter para int.
    batch_size_val = settings.get('BATCH_SIZE')
    delay_seconds_val = settings.get('DELAY_SECONDS')
    smtp_port_val = settings.get('SMTP_PORT')

    batch_size = int(batch_size_val) if batch_size_val is not None else 15
    delay_seconds = int(delay_seconds_val) if delay_seconds_val is not None else 60
    smtp_port = int(smtp_port_val) if smtp_port_val is not None else 587
    
    sent_count, fail_count = 0, 0
    for i in range(0, len(recipients), batch_size):
        batch = recipients[i:i + batch_size]
        for recipient in batch:
            recipient_email = recipient.get("Email")
            if recipient_email:
                try:
                    msg = EmailMessage()
                    msg['Subject'] = subject
                    msg['From'] = settings.get('SMTP_USER')
                    msg['To'] = recipient_email
                    msg.add_alternative(body, subtype='html')
                    with smtplib.SMTP(str(settings.get('SMTP_HOST')), smtp_port) as server:
                        server.starttls()
                        server.login(settings.get('SMTP_USER'), settings.get('SMTP_PASSWORD'))
                        server.send_message(msg)
                    sent_count += 1
                except Exception as e:
                    fail_count += 1
                    print(f"FALHA ao enviar para {recipient_email}: {e}")
        if i + batch_size < len(recipients):
            time.sleep(delay_seconds)
    return sent_count, fail_count

@app.route('/automations', methods=['GET', 'POST'])
@login_required
@feature_required('schedules')
def automations_page():
    settings = load_settings()
    if request.method == 'POST':
        automations = settings.get('automations', {})
        automations['welcome'] = {
            'enabled': 'welcome_enabled' in request.form,
            'subject': request.form.get('welcome_subject', ''),
            'body': request.form.get('welcome_body', '')
        }
        automations['expiry'] = {
            'enabled': 'expiry_enabled' in request.form,
            'subject_7_days': request.form.get('expiry_7_days_subject', ''),
            'body_7_days': request.form.get('expiry_7_days_body', ''),
            'subject_3_days': request.form.get('expiry_3_days_subject', ''),
            'body_3_days': request.form.get('expiry_3_days_body', ''),
            'subject_1_day': request.form.get('expiry_1_day_subject', ''),
            'body_1_day': request.form.get('expiry_1_day_body', '')
        }
        settings['automations'] = automations
        save_settings(settings)
        flash('Configurações de automação salvas com sucesso!', 'success')
        return redirect(url_for('automations_page'))
    automations_data = settings.get('automations', {})
    return render_template('automations.html', automations=automations_data)

@app.route('/verificar-status-plano')
@login_required
def check_plan_status():
    conn = get_db_connection()
    user = conn.execute("SELECT plan_id, plan_expiration_date FROM users WHERE id = ?", (session['user_id'],)).fetchone()
    conn.close()
    if user and user['plan_id'] and user['plan_expiration_date'] and datetime.strptime(user['plan_expiration_date'], '%Y-%m-%d') >= datetime.now():
        return jsonify({'status': 'aprovado'})
    return jsonify({'status': 'pendente'})

@app.route('/ajuda')
@login_required
def help_page():
    return render_template('ajuda.html')

@app.route('/history')
@login_required
def history_page():
    conn = get_db_connection()
    history = conn.execute("SELECT * FROM envio_historico WHERE user_id = ? ORDER BY sent_at DESC", (session['user_id'],)).fetchall()
    conn.close()
    return render_template('history.html', history=history)

@app.route('/history/resend', methods=['POST'])
@login_required
def resend_from_history():
    """
    Pega o conteúdo de um e-mail do histórico e o carrega na sessão
    para pré-preencher a página de envio em massa.
    """
    session['resend_subject'] = request.form.get('subject')
    session['resend_body'] = request.form.get('body')
    flash('Conteúdo do e-mail carregado para reenvio.', 'info')
    return redirect(url_for('mass_send_page'))

@app.route('/history/save-as-template', methods=['POST'])
@login_required
def save_history_as_template():
    """
    Salva o conteúdo de um e-mail do histórico como um novo modelo (template).
    """
    template_name = request.form.get('template_name')
    subject = request.form.get('subject')
    body = request.form.get('body')
    user_id = session['user_id']

    if not all([template_name, subject, body]):
        flash("Todos os campos são necessários para salvar o modelo.", "warning")
        return redirect(url_for('history_page'))

    try:
        conn = get_db_connection()
        # Adiciona o user_id para garantir que o template pertença ao usuário correto
        conn.execute(
            "INSERT INTO email_templates (user_id, name, subject, body) VALUES (?, ?, ?, ?)",
            (user_id, template_name, subject, body)
        )
        conn.commit()
        flash(f'E-mail salvo como o modelo "{template_name}" com sucesso!', 'success')
    except sqlite3.IntegrityError:
        flash(f'Um modelo com o nome "{template_name}" já existe. Por favor, escolha outro nome.', 'danger')
    except Exception as e:
        flash(f'Erro ao salvar modelo: {e}', 'danger')
    finally:
        if conn: conn.close()
    
    return redirect(url_for('history_page'))

@app.route('/templates', methods=['GET', 'POST'])
@login_required
def templates_page():
    if request.method == 'POST':
        # Sua lógica de criar template...
        return redirect(url_for('templates_page'))
    conn = get_db_connection()
    templates = conn.execute("SELECT * FROM email_templates WHERE user_id = ? ORDER BY name", (session['user_id'],)).fetchall()
    conn.close()
    return render_template('templates.html', templates=templates)

# --- LÓGICA DO WORKER (ROBÔ) ---

def worker_check_and_run_automations(user_settings, all_contacts_processed, conn):
    """Verifica e executa as automações para um usuário específico."""
    automations_config_str = user_settings.get('automations_config')
    if not automations_config_str: return

    automations = json.loads(automations_config_str)
    cursor = conn.cursor()
    
    # Automação de Boas-vindas
    welcome_config = automations.get('welcome', {})
    if welcome_config.get('enabled'):
        print(f"  -> [Usuário: {user_settings['email']}] Verificando Boas-vindas...")
        recipients_welcome = []
        subject, body = welcome_config.get('subject'), welcome_config.get('body')
        if subject and body:
            for contact in all_contacts_processed:
                created_on_str = contact.get("Data") # Usando a coluna 'Data'
                if created_on_str:
                    created_date = parse_date_string(created_on_str.split('T')[0])
                    if created_date and (datetime.now() - created_date).days < 1:
                        cursor.execute("SELECT id FROM envio_historico WHERE user_id = ? AND recipient_email = ? AND subject = ?", (user_settings['id'], contact.get('Email'), subject))
                        if cursor.fetchone() is None:
                            recipients_welcome.append(contact)
            if recipients_welcome:
                print(f"    -> Encontrados {len(recipients_welcome)} novo(s) contato(s) para boas-vindas.")
                send_emails_in_batches(recipients_welcome, subject, body, user_settings)

    # Automação de Expiração
    expiry_config = automations.get('expiry', {})
    if expiry_config.get('enabled'):
        print(f"  -> [Usuário: {user_settings['email']}] Verificando Expirações...")
        for days_left in [7, 3, 1]:
            recipients_expiry = []
            subject, body = expiry_config.get(f'subject_{days_left}_days'), expiry_config.get(f'body_{days_left}_days')
            if subject and body:
                for contact in all_contacts_processed:
                    if contact.get('remaining_days_int') == days_left:
                        cursor.execute("SELECT id FROM envio_historico WHERE user_id = ? AND recipient_email = ? AND subject = ? AND date(sent_at) = date('now', 'localtime')", (user_settings['id'], contact.get('Email'), subject))
                        if cursor.fetchone() is None:
                            recipients_expiry.append(contact)
                if recipients_expiry:
                    print(f"    -> Encontrados {len(recipients_expiry)} contato(s) expirando em {days_left} dia(s).")
                    send_emails_in_batches(recipients_expiry, subject, body, user_settings)


def worker_process_pending_schedules(user_settings, all_contacts_processed, conn):
    """Processa e-mails da fila de agendamento para um usuário específico."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM scheduled_emails WHERE is_sent = FALSE AND user_id = ? AND datetime(send_at) <= datetime('now', 'localtime')", (user_settings['id'],))
    pending_emails = cursor.fetchall()
    
    if not pending_emails: return

    print(f"-> [Usuário: {user_settings['email']}] Encontrados {len(pending_emails)} agendamento(s) para processar.")
    for email_job in pending_emails:
        subject, body = email_job['subject'], email_job['body']
        recipients = []
        if email_job['schedule_type'] == 'group':
            target_status = email_job['status_target']
            if target_status == 'all': recipients = all_contacts_processed
            else: recipients = [c for c in all_contacts_processed if c['status_badge_class'] == target_status]
        elif email_job['schedule_type'] == 'manual':
            if email_job['manual_recipients']:
                email_list = email_job['manual_recipients'].split(',')
                recipients = [{'Email': email.strip(), 'id': user_settings['id']} for email in email_list if email.strip()]

        print(f"  -> Processando agendamento ID {email_job['id']} para {len(recipients)} destinatário(s)...")
        sent_count, fail_count = send_emails_in_batches(recipients, subject, body, user_settings)
        if fail_count == 0:
            cursor.execute("UPDATE scheduled_emails SET is_sent = TRUE WHERE id = ?", (email_job['id'],))
            conn.commit()
            print(f"  -> Agendamento ID {email_job['id']} concluído e marcado como enviado.")
        else:
            print(f"  -> Agendamento ID {email_job['id']} concluído com {fail_count} falhas.")


def worker_main_loop():
    """Loop principal do worker, agora processando usuário por usuário."""
    print("--- Worker de Fundo Iniciado ---")
    while True:
        try:
            print(f"\n[{datetime.now()}] Worker: Iniciando ciclo de verificação...")
            conn = get_db_connection()
            vip_users = conn.execute("SELECT * FROM users WHERE plan = 'vip'").fetchall()
            conn.close()

            if not vip_users:
                print("-> Worker: Nenhum usuário VIP ativo encontrado.")
            
            for user in vip_users:
                user_settings = dict(user)
                print(f"--- Processando para o usuário: {user_settings['email']} ---")
                if not all([user_settings.get('baserow_host'), user_settings.get('baserow_api_key'), user_settings.get('smtp_user')]):
                    print(f"-> AVISO: Configurações incompletas para {user_settings['email']}. Pulando.")
                    continue
                try:
                    all_contacts_raw = get_all_contacts_from_baserow(user_settings)
                    all_contacts_processed = process_contacts_status(all_contacts_raw)

                    conn_job = get_db_connection()
                    worker_process_pending_schedules(user_settings, all_contacts_processed, conn_job)
                    worker_check_and_run_automations(user_settings, all_contacts_processed, conn_job)
                    conn_job.close()
                    print(f"--- Ciclo para {user_settings['email']} concluído. ---")
                except Exception as e:
                    print(f"  -> ERRO ao processar para o usuário {user_settings['email']}: {e}")

            print(f"[{datetime.now()}] Worker: Ciclo geral concluído. Aguardando 5 minutos...")
            time.sleep(300) # O worker verifica a cada 5 minutos

        except KeyboardInterrupt:
            print("\n--- Worker Parado ---")
            break
        except Exception as e:
            print(f"ERRO CRÍTICO NO WORKER: {e}")
            time.sleep(60)

if __name__ == '__main__':
    init_db()
    # Inicia o worker em um processo de fundo (thread)
    worker_thread = threading.Thread(target=worker_main_loop, daemon=True)
    worker_thread.start()
    # Inicia a aplicação web
    app.run(debug=False, use_reloader=True)