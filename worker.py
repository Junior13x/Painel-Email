import sqlite3
from datetime import datetime
import time
import json
# Importa as funções do nosso arquivo de lógica
from core_logic import (
    load_settings, get_all_contacts_from_baserow,
    process_contacts_status, send_emails_in_batches, parse_date_string
)

def get_db_connection():
    """Cria uma conexão com o banco de dados."""
    conn = sqlite3.connect('painel.db')
    conn.row_factory = sqlite3.Row
    return conn

# --- CONFIGURAÇÃO IMPORTANTE ---
COLUNA_DATA_CRIACAO = "Data"
# -----------------------------

def check_and_run_automations(user, all_contacts_processed, conn):
    """Verifica e executa as automações para um usuário específico."""
    automations_config_str = user['automations_config']
    if not automations_config_str: return

    automations = json.loads(automations_config_str)
    cursor = conn.cursor()
    user_settings = dict(user)
    
    # --- Automação 1: Boas-vindas ---
    welcome_config = automations.get('welcome', {})
    if welcome_config.get('enabled'):
        print(f"  -> [Usuário: {user['email']}] Verificando automação de Boas-vindas...")
        recipients_welcome = []
        subject = welcome_config.get('subject')
        body = welcome_config.get('body')
        if subject and body:
            for contact in all_contacts_processed:
                created_on_str = contact.get(COLUNA_DATA_CRIACAO)
                if created_on_str:
                    created_date = parse_date_string(created_on_str.split('T')[0])
                    if created_date and (datetime.now() - created_date).days < 1:
                        cursor.execute("SELECT id FROM envio_historico WHERE recipient_email = ? AND subject = ?", (contact.get('Email'), subject))
                        if cursor.fetchone() is None:
                            recipients_welcome.append(contact)
            if recipients_welcome:
                print(f"    -> Encontrados {len(recipients_welcome)} novo(s) contato(s).")
                send_emails_in_batches(recipients_welcome, subject, body, user_settings)

    # --- Automação 2: Avisos de Expiração ---
    expiry_config = automations.get('expiry', {})
    if expiry_config.get('enabled'):
        print(f"  -> [Usuário: {user['email']}] Verificando automação de Expiração...")
        for days_left in [7, 3, 1]:
            recipients_expiry = []
            subject = expiry_config.get(f'subject_{days_left}_days')
            body = expiry_config.get(f'body_{days_left}_days')
            if subject and body:
                for contact in all_contacts_processed:
                    if contact.get('remaining_days_int') == days_left:
                        cursor.execute("SELECT id FROM envio_historico WHERE recipient_email = ? AND subject = ? AND date(sent_at) = date('now', 'localtime')", (contact.get('Email'), subject))
                        if cursor.fetchone() is None:
                            recipients_expiry.append(contact)
                if recipients_expiry:
                    print(f"    -> Encontrados {len(recipients_expiry)} contato(s) expirando em {days_left} dia(s).")
                    send_emails_in_batches(recipients_expiry, subject, body, user_settings)

def process_pending_schedules(user, all_contacts_processed, conn):
    """Processa e-mails da fila de agendamento para um usuário específico."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM scheduled_emails WHERE is_sent = FALSE AND user_id = ? AND datetime(send_at) <= datetime('now', 'localtime')", (user['id'],))
    pending_emails = cursor.fetchall()
    
    if not pending_emails: return

    print(f"-> [Usuário: {user['email']}] Encontrados {len(pending_emails)} agendamento(s) para processar.")
    user_settings = dict(user)

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
                recipients = [{'Email': email.strip()} for email in email_list if email.strip()]

        print(f"  -> Processando agendamento ID {email_job['id']} para {len(recipients)} destinatário(s)...")
        sent_count, fail_count = send_emails_in_batches(recipients, subject, body, user_settings)
        if fail_count == 0:
            cursor.execute("UPDATE scheduled_emails SET is_sent = TRUE WHERE id = ?", (email_job['id'],))
            conn.commit()
            print(f"  -> Agendamento ID {email_job['id']} concluído e marcado como enviado.")
        else:
            print(f"  -> Agendamento ID {email_job['id']} concluído com {fail_count} falhas.")

def run_once():
    """Função principal que roda o ciclo de verificação uma única vez."""
    print(f"\n[{datetime.now()}] Iniciando ciclo de verificação do Cron Job...")
    conn = get_db_connection()
    vip_users = conn.execute("SELECT * FROM users WHERE plan = 'vip'").fetchall()
    conn.close()

    if not vip_users:
        print("-> Nenhum usuário VIP ativo encontrado. Encerrando ciclo.")
        return

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
            process_pending_schedules(user_settings, all_contacts_processed, conn_job)
            check_and_run_automations(user_settings, all_contacts_processed, conn_job)
            conn_job.close()
            print(f"--- Ciclo para {user_settings['email']} concluído. ---")
        except Exception as e:
            print(f"  -> ERRO ao processar para o usuário {user_settings['email']}: {e}")

    print(f"[{datetime.now()}] Ciclo geral de verificação concluído.")

if __name__ == '__main__':
    run_once()

