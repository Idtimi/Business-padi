from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

db_url = settings.database_url
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
engine = create_engine(db_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """All database models inherit from this."""


def ensure_columns():
    from sqlalchemy import text
    with engine.connect() as conn:
        is_sqlite = engine.dialect.name == "sqlite"
        if is_sqlite:
            for col_stmt in ("ALTER TABLE documents ADD COLUMN items_json JSON", "ALTER TABLE documents ADD COLUMN due_date VARCHAR"):
                try:
                    conn.execute(text(col_stmt))
                    conn.commit()
                except Exception:
                    pass
        else:
            try:
                conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS items_json JSON"))
                conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS due_date VARCHAR"))
                conn.commit()
            except Exception:
                pass

try:
    ensure_columns()
except Exception:
    pass


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
