# Arquivo: init_database.py
import os
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

def init_db():
    """Cria as tabelas do banco de dados a partir do zero."""
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise ValueError("ERRO: Variável de ambiente DATABASE_URL não foi encontrada. Configure-a no Job do Render.")

    # SQLAlchemy espera 'postgresql' em vez de 'postgres' no início da URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(db_url)
    conn = None  # Inicializa a variável conn
    try:
        conn = engine.connect()
        print("Conectado ao banco de dados PostgreSQL.")
        
        # O comando 'CASCADE' apaga tabelas que dependem umas das outras.
        conn.execute(text("DROP TABLE IF EXISTS users, features, plans, plan_features, envio_historico, scheduled_emails, email_templates CASCADE;"))
        print("Tabelas antigas removidas (se existiam).")

        # Recria as tabelas (coloquei os CREATE TABLE que você já tem aqui)
        conn.execute(text("""
            CREATE TABLE users (
                id SERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
                plan_id INTEGER, plan_expiration_date DATE, baserow_host TEXT, baserow_api_key TEXT, baserow_table_id TEXT,
                smtp_host TEXT, smtp_port INTEGER, smtp_user TEXT, smtp_password TEXT, batch_size INTEGER, delay_seconds INTEGER,
                automations_config TEXT, sends_today INTEGER DEFAULT 0, last_send_date DATE
            );
        """))
        conn.execute(text("CREATE TABLE features (id SERIAL PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, description TEXT);"))
        conn.execute(text("CREATE TABLE plans (id SERIAL PRIMARY KEY, name TEXT NOT NULL, price NUMERIC(10, 2) NOT NULL, validity_days INTEGER NOT NULL, daily_send_limit INTEGER DEFAULT 25, is_active BOOLEAN DEFAULT TRUE);"))
        conn.execute(text("CREATE TABLE plan_features (plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE, feature_id INTEGER REFERENCES features(id) ON DELETE CASCADE, PRIMARY KEY (plan_id, feature_id));"))
        conn.execute(text("CREATE TABLE envio_historico (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), recipient_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT, sent_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);"))
        conn.execute(text("CREATE TABLE scheduled_emails (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), schedule_type TEXT NOT NULL, status_target TEXT, manual_recipients TEXT, subject TEXT NOT NULL, body TEXT NOT NULL, send_at TIMESTAMP NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_sent BOOLEAN DEFAULT FALSE);"))
        conn.execute(text("CREATE TABLE email_templates (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id), name TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(user_id, name));"))
        print("Todas as tabelas foram criadas com sucesso.")

        # Insere dados iniciais
        conn.execute(text("INSERT INTO features (name, slug, description) VALUES ('Agendamentos', 'schedules', 'Permite agendar envios de e-mail para o futuro.');"))
        
        # Cria o Admin Padrão
        password_hash = generate_password_hash('130896')
        conn.execute(
            text("INSERT INTO users (email, password_hash, role) VALUES (:email, :ph, 'admin')"),
            {'email': 'junior@admin.com', 'ph': password_hash}
        )
        print("Usuário Admin padrão criado.")

        conn.commit()
        print("\n===> BANCO DE DADOS INICIALIZADO COM SUCESSO! <===")
        
    except Exception as e:
        print(f"\nOcorreu um erro: {e}")
        # Em caso de erro, desfaz a transação para não deixar o banco em estado inconsistente
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexão com o banco de dados fechada.")

if __name__ == '__main__':
    init_db()