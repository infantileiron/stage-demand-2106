"""Small parameterized repository. MySQL in Compose; SQLite is explicit local QA mode."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote
from src.config.settings import settings

TABLES = ("datasets", "scenarios", "runs", "plans", "reports", "users", "audit", "schedules", "delivery_snapshots")
_lock = threading.Lock()

@contextmanager
def connection():
    if settings.database_url.startswith("sqlite:///"):
        db = sqlite3.connect(settings.database_url[10:], timeout=30)
        db.row_factory = sqlite3.Row
    else:
        import pymysql
        u = urlparse(settings.database_url)
        db = pymysql.connect(host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
             password=unquote(u.password or ""), database=u.path.lstrip("/"), charset="utf8mb4",
             cursorclass=pymysql.cursors.DictCursor, connect_timeout=10)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def execute(db, sql, params=()):
    cur = db.cursor()
    cur.execute(sql.replace("?", "%s") if not isinstance(db, sqlite3.Connection) else sql, params)
    return cur

def init_db():
    with _lock, connection() as db:
        auto = "INTEGER PRIMARY KEY AUTOINCREMENT" if isinstance(db, sqlite3.Connection) else "BIGINT PRIMARY KEY AUTO_INCREMENT"
        for table in TABLES:
            execute(db, f"CREATE TABLE IF NOT EXISTS {table} (id {auto}, natural_key VARCHAR(180) UNIQUE NOT NULL, payload LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL)")

def unpack(row):
    return dict(json.loads(row["payload"]), id=row["id"], key=row["natural_key"], created_at=row["created_at"])

def list_rows(table):
    assert table in TABLES
    with connection() as db:
        return [unpack(r) for r in execute(db, f"SELECT * FROM {table} ORDER BY id DESC").fetchall()]

def get_row(table, ident):
    assert table in TABLES
    with connection() as db:
        row = execute(db, f"SELECT * FROM {table} WHERE id = ?", (ident,)).fetchone()
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(404, "记录不存在")
    return unpack(row)

def find_by_key(table, key):
    """Return the document with this natural key, or None."""
    assert table in TABLES
    with connection() as db:
        row = execute(db, f"SELECT * FROM {table} WHERE natural_key = ?", (key,)).fetchone()
    return unpack(row) if row else None

def insert_if_absent(table, key, payload):
    """Atomically insert a natural-keyed document. Returns (row, created).

    A concurrent writer that wins the unique key is returned unchanged;
    the existing payload is never overwritten.
    """
    assert table in TABLES
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    with connection() as db:
        try:
            execute(db, f"INSERT INTO {table}(natural_key,payload,created_at) VALUES (?,?,?)",
                (key, serialized, datetime.now(timezone.utc).isoformat()))
            created = True
        except Exception as exc:
            if type(exc).__name__ != "IntegrityError":
                raise
            created = False
        row = execute(db, f"SELECT * FROM {table} WHERE natural_key = ?", (key,)).fetchone()
    if row is None:
        raise RuntimeError("记录写入后无法读取")
    return unpack(row), created

def insert_row(table, key, payload, preserve=False):
    assert table in TABLES
    if preserve:
        row, _ = insert_if_absent(table, key, payload)
        return row
    if find_by_key(table, key) is not None:
        from fastapi import HTTPException
        raise HTTPException(409, "名称已存在，请使用新名称")
    with connection() as db:
        try:
            cur = execute(db, f"INSERT INTO {table}(natural_key,payload,created_at) VALUES (?,?,?)",
                (key, json.dumps(payload, ensure_ascii=False, allow_nan=False), datetime.now(timezone.utc).isoformat()))
        except Exception as exc:
            # A concurrent insert may win the unique key between the check and this write.
            if type(exc).__name__ != "IntegrityError": raise
            from fastapi import HTTPException
            raise HTTPException(409, "名称已存在，请使用新名称") from exc
        ident = cur.lastrowid
    return get_row(table, ident)

def update_row(table, ident, changes):
    assert table in TABLES
    # Read and write under a single connection; queue has a single consumer per stack.
    with connection() as db:
        old = execute(db, f"SELECT * FROM {table} WHERE id = ?", (ident,)).fetchone()
        if old is None:
            from fastapi import HTTPException
            raise HTTPException(404, "记录不存在")
        value = json.loads(old["payload"])
        value.update(changes)
        execute(db, f"UPDATE {table} SET payload=? WHERE id=?", (json.dumps(value, ensure_ascii=False, allow_nan=False), ident))
    return get_row(table, ident)

def update_row_locked(table, ident, mutate):
    """Read-modify-write one row while holding a row lock.

    ``mutate`` receives the decoded payload and returns the changes to apply; an
    empty dict means another writer already made the same transition. SQLite
    takes a write lock up front (BEGIN IMMEDIATE) and MySQL locks the row with
    SELECT ... FOR UPDATE, so parallel catch-up runs cannot clobber progress.
    """
    assert table in TABLES
    with connection() as db:
        if isinstance(db, sqlite3.Connection):
            execute(db, "BEGIN IMMEDIATE")
            row = execute(db, f"SELECT * FROM {table} WHERE id = ?", (ident,)).fetchone()
        else:
            execute(db, "START TRANSACTION")
            row = execute(db, f"SELECT * FROM {table} WHERE id = ? FOR UPDATE", (ident,)).fetchone()
        if row is None:
            from fastapi import HTTPException
            raise HTTPException(404, "记录不存在")
        value = json.loads(row["payload"])
        changes = mutate(dict(value)) or {}
        if changes:
            value.update(changes)
            execute(db, f"UPDATE {table} SET payload=? WHERE id=?",
                (json.dumps(value, ensure_ascii=False, allow_nan=False), ident))
            row = execute(db, f"SELECT * FROM {table} WHERE id = ?", (ident,)).fetchone()
    return unpack(row)
