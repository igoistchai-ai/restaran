import os
import re
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import uvicorn


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

WEB_APP_URL = os.getenv(
    "WEB_APP_URL",
    "https://restaran-xtny.onrender.com"
).strip()

DATABASE = os.getenv(
    "DATABASE_PATH",
    "restaran.db"
)

DEFAULT_ADMIN_IDS = {
    8357023784,
    7003441441,
}


def parse_admin_ids():
    raw = os.getenv("ADMIN_IDS", "").strip()

    if not raw:
        return DEFAULT_ADMIN_IDS.copy()

    result = set()

    for value in raw.split(","):
        value = value.strip()

        if not value:
            continue

        try:
            result.add(int(value))
        except ValueError:
            continue

    return result or DEFAULT_ADMIN_IDS.copy()


ADMIN_IDS = parse_admin_ids()


# =========================================================
# APP
# =========================================================

telegram_app = None
cleanup_task = None


# =========================================================
# DATABASE
# =========================================================

def get_connection():
    connection = sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_db():
    connection = get_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                pin_hash TEXT,
                pin_plain TEXT,
                role TEXT NOT NULL DEFAULT 'customer',
                telegram_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0,
                online INTEGER NOT NULL DEFAULT 0,
                lat REAL,
                lon REAL,
                updated_at INTEGER,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                courier_id INTEGER,
                title TEXT,
                address TEXT,
                price REAL DEFAULT 0,
                status TEXT DEFAULT 'new',
                created_at INTEGER NOT NULL,
                customer_confirmed INTEGER DEFAULT 0,
                closed_at INTEGER
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        connection.commit()

        migrate_database(connection)

    finally:
        connection.close()


def get_columns(connection, table):
    rows = connection.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return {
        row["name"]
        for row in rows
    }


def add_column(connection, table, column, definition):
    columns = get_columns(
        connection,
        table
    )

    if column not in columns:
        connection.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN {column} {definition}
            """
        )


def migrate_database(connection):
    add_column(
        connection,
        "users",
        "pin_plain",
        "TEXT"
    )

    add_column(
        connection,
        "users",
        "telegram_id",
        "INTEGER"
    )

    add_column(
        connection,
        "users",
        "active",
        "INTEGER NOT NULL DEFAULT 1"
    )

    add_column(
        connection,
        "orders",
        "customer_confirmed",
        "INTEGER DEFAULT 0"
    )

    add_column(
        connection,
        "orders",
        "closed_at",
        "INTEGER"
    )

    connection.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_users_phone
        ON users(phone)
    """)

    connection.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_users_telegram
        ON users(telegram_id)
    """)

    connection.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_orders_customer
        ON orders(customer_id)
    """)

    connection.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_orders_status
        ON orders(status)
    """)

    connection.commit()


init_db()


# =========================================================
# UTILS
# =========================================================

def current_time():
    return int(time.time())


def normalize_phone(phone):
    return re.sub(
        r"\D",
        "",
        phone or ""
    )


def hash_pin(pin):
    salt = secrets.token_hex(16)

    digest = hashlib.sha256(
        (salt + pin).encode("utf-8")
    ).hexdigest()

    return salt + ":" + digest


def verify_pin(pin, stored):
    if not stored:
        return False

    try:
        salt, digest = stored.split(
            ":",
            1
        )

        calculated = hashlib.sha256(
            (salt + pin).encode("utf-8")
        ).hexdigest()

        return hmac.compare_digest(
            calculated,
            digest
        )

    except Exception:
        return False


def create_session(user_id, role):
    token = secrets.token_urlsafe(48)

    connection = get_connection()

    try:
        connection.execute(
            """
            INSERT INTO sessions(
                token,
                user_id,
                role,
                created_at
            )
            VALUES(?,?,?,?)
            """,
            (
                token,
                user_id,
                role,
                current_time()
            )
        )

        connection.commit()

    finally:
        connection.close()

    return token


def get_auth(authorization):
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Требуется авторизация"
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Неверный формат токена"
        )

    token = authorization[7:].strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Пустой токен"
        )

    connection = get_connection()

    try:
        row = connection.execute(
            """
            SELECT
                s.token,
                s.user_id,
                s.role,
                s.created_at,
                u.name,
                u.phone,
                u.active
            FROM sessions s
            JOIN users u
                ON u.id = s.user_id
            WHERE s.token = ?
            LIMIT 1
            """,
            (token,)
        ).fetchone()

    finally:
        connection.close()

    if not row:
        raise HTTPException(
            status_code=401,
            detail="Сессия не найдена"
        )

    if not row["active"]:
        raise HTTPException(
            status_code=403,
            detail="Аккаунт отключён"
        )

    return row


def require_admin(auth):
    if auth["role"] != "admin":
        raise HTTPException(
            status_code=403,
            detail="Доступ только администраторам"
        )


# =========================================================
# TELEGRAM VALIDATION
# =========================================================

def validate_telegram_init_data(init_data):
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN не задан на Render"
        )

    if not init_data:
        raise ValueError(
            "Telegram initData отсутствует. "
            "Откройте Web App через Telegram."
        )

    data = dict(
        parse_qsl(
            init_data,
            keep_blank_values=True
        )
    )

    received_hash = data.pop(
        "hash",
        None
    )

    if not received_hash:
        raise ValueError(
            "Hash отсутствует"
        )

    check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(
            data.items()
        )
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode("utf-8"),
        hashlib.sha256
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        check_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(
        calculated_hash,
        received_hash
    ):
        raise ValueError(
            "Неверная подпись Telegram. "
            "Проверьте BOT_TOKEN на Render."
        )

    try:
        auth_date = int(
            data.get(
                "auth_date",
                "0"
            )
        )
    except Exception:
        auth_date = 0

    if not auth_date:
        raise ValueError(
            "auth_date отсутствует"
        )

    if current_time() - auth_date > 86400:
        raise ValueError(
            "Telegram initData устарел"
        )

    try:
        user = json.loads(
            data.get(
                "user",
                "{}"
            )
        )
    except Exception:
        raise ValueError(
            "Не удалось прочитать Telegram user"
        )

    if not user.get("id"):
        raise ValueError(
            "Telegram user отсутствует"
        )

    return user


# =========================================================
# PYDANTIC MODELS
# =========================================================

class LoginRequest(BaseModel):
    phone: str
    pin: str


class CustomerCreate(BaseModel):
    name: str
    phone: str
    pin: str


class AdminWebLogin(BaseModel):
    init_data: str


# =========================================================
# FASTAPI LIFESPAN
# =========================================================

@asynccontextmanager
async def lifespan(application):
    global telegram_app
    global cleanup_task

    init_db()

    if BOT_TOKEN:
        try:
            telegram_app = (
                Application
                .builder()
                .token(BOT_TOKEN)
                .build()
            )

            telegram_app.add_handler(
                CommandHandler(
                    "start",
                    start_command
                )
            )

            telegram_app.add_handler(
                CommandHandler(
                    "random",
                    random_command
                )
            )

            telegram_app.add_handler(
                MessageHandler(
                    filters.CONTACT,
                    contact_handler
                )
            )

            await telegram_app.initialize()
            await telegram_app.start()

            if telegram_app.updater:
                await telegram_app.updater.start_polling(
                    drop_pending_updates=True
                )

            print("Telegram bot started")

        except Exception as error:
            print(
                "Telegram bot error:",
                repr(error)
            )

            telegram_app = None

    else:
        print(
            "BOT_TOKEN is not configured"
        )

    cleanup_task = asyncio.create_task(
        cleanup_loop()
    )

    yield

    if cleanup_task:
        cleanup_task.cancel()

        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

    if telegram_app:
        try:
            if telegram_app.updater:
                await telegram_app.updater.stop()
        except Exception:
            pass

        try:
            await telegram_app.stop()
        except Exception:
            pass

        try:
            await telegram_app.shutdown()
        except Exception:
            pass

        telegram_app = None


app = FastAPI(
    title="RESTARAN",
    version="2.0",
    lifespan=lifespan
)


# =========================================================
# MAIN PAGE
# =========================================================

@app.get("/")
async def home():
    return FileResponse(
        "index.html"
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "RESTARAN",
        "admins": sorted(
            ADMIN_IDS
        )
    }


# =========================================================
# NORMAL LOGIN
# =========================================================

@app.post("/api/login")
async def login(payload: LoginRequest):
    phone = normalize_phone(
        payload.phone
    )

    pin = payload.pin.strip()

    if not phone:
        raise HTTPException(
            status_code=400,
            detail="Введите номер телефона"
        )

    if not pin:
        raise HTTPException(
            status_code=400,
            detail="Введите PIN"
        )

    connection = get_connection()

    try:
        user = connection.execute(
            """
            SELECT *
            FROM users
            WHERE phone = ?
              AND active = 1
            LIMIT 1
            """,
            (phone,)
        ).fetchone()

    finally:
        connection.close()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Пользователь не найден"
        )

    if not verify_pin(
        pin,
        user["pin_hash"]
    ):
        raise HTTPException(
            status_code=401,
            detail="Неверный PIN"
        )

    token = create_session(
        user["id"],
        user["role"]
    )

    return {
        "ok": True,
        "token": token,
        "role": user["role"],
        "user": {
            "id": user["id"],
            "name": user["name"],
            "phone": user["phone"]
        }
    }


# =========================================================
# ME
# =========================================================

@app.get("/api/me")
async def me(
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    return {
        "ok": True,
        "role": auth["role"],
        "user": {
            "id": auth["user_id"],
            "name": auth["name"],
            "phone": auth["phone"]
        }
    }


# =========================================================
# ADMIN WEB LOGIN
# =========================================================

@app.post("/api/admin/web-login")
async def admin_web_login(
    payload: AdminWebLogin
):
    try:
        telegram_user = validate_telegram_init_data(
            payload.init_data
        )

    except Exception as error:
        raise HTTPException(
            status_code=403,
            detail=(
                "Не удалось проверить администратора: "
                + str(error)
            )
        )

    try:
        telegram_id = int(
            telegram_user["id"]
        )
    except Exception:
        raise HTTPException(
            status_code=403,
            detail="Неверный Telegram ID"
        )

    if telegram_id not in ADMIN_IDS:
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещён"
        )

    name = (
        telegram_user.get("first_name")
        or telegram_user.get("username")
        or "Administrator"
    )

    connection = get_connection()

    try:
        admin = connection.execute(
            """
            SELECT *
            FROM users
            WHERE telegram_id = ?
              AND role = 'admin'
            LIMIT 1
            """,
            (telegram_id,)
        ).fetchone()

        if admin:
            admin_id = admin["id"]

            connection.execute(
                """
                UPDATE users
                SET
                    name = ?,
                    active = 1
                WHERE id = ?
                """,
                (
                    name,
                    admin_id
                )
            )

        else:
            connection.execute(
                """
                INSERT INTO users(
                    name,
                    phone,
                    pin_hash,
                    pin_plain,
                    role,
                    telegram_id,
                    active,
                    created_at
                )
                VALUES(
                    ?,
                    ?,
                    NULL,
                    NULL,
                    'admin',
                    ?,
                    1,
                    ?
                )
                """,
                (
                    name,
                    "telegram:" + str(telegram_id),
                    telegram_id,
                    current_time()
                )
            )

            admin_id = connection.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

        connection.commit()

    finally:
        connection.close()

    token = create_session(
        admin_id,
        "admin"
    )

    return {
        "ok": True,
        "token": token,
        "role": "admin",
        "user": {
            "id": admin_id,
            "name": name,
            "telegram_id": telegram_id
        }
    }


# =========================================================
# ADMIN STATS
# =========================================================

@app.get("/api/admin/stats")
async def admin_stats(
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    require_admin(auth)

    connection = get_connection()

    try:
        customers = connection.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE role = 'customer'
              AND active = 1
            """
        ).fetchone()[0]

        couriers = connection.execute(
            """
            SELECT COUNT(*)
            FROM couriers
            WHERE active = 1
            """
        ).fetchone()[0]

        orders = connection.execute(
            """
            SELECT COUNT(*)
            FROM orders
            """
        ).fetchone()[0]

        active_orders = connection.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE status NOT IN(
                'closed',
                'cancelled'
            )
            """
        ).fetchone()[0]

    finally:
        connection.close()

    return {
        "customers": customers,
        "couriers": couriers,
        "orders": orders,
        "active_orders": active_orders
    }


# =========================================================
# ADMIN CUSTOMERS
# =========================================================

@app.get("/api/admin/customers")
async def admin_customers(
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    require_admin(auth)

    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                id,
                name,
                phone,
                pin_plain,
                active,
                created_at
            FROM users
            WHERE role = 'customer'
            ORDER BY id DESC
            """
        ).fetchall()

    finally:
        connection.close()

    return [
        dict(row)
        for row in rows
    ]


@app.post("/api/admin/customers")
async def create_customer(
    payload: CustomerCreate,
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    require_admin(auth)

    name = payload.name.strip()
    phone = normalize_phone(
        payload.phone
    )
    pin = payload.pin.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Введите имя"
        )

    if not phone:
        raise HTTPException(
            status_code=400,
            detail="Введите номер телефона"
        )

    if not re.fullmatch(
        r"\d{4,8}",
        pin
    ):
        raise HTTPException(
            status_code=400,
            detail="PIN должен содержать 4-8 цифр"
        )

    connection = get_connection()

    try:
        existing = connection.execute(
            """
            SELECT id
            FROM users
            WHERE phone = ?
              AND role = 'customer'
              AND active = 1
            LIMIT 1
            """,
            (phone,)
        ).fetchone()

        if existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Клиент с таким номером "
                    "уже существует"
                )
            )

        connection.execute(
            """
            INSERT INTO users(
                name,
                phone,
                pin_hash,
                pin_plain,
                role,
                active,
                created_at
            )
            VALUES(
                ?,
                ?,
                ?,
                ?,
                'customer',
                1,
                ?
            )
            """,
            (
                name,
                phone,
                hash_pin(pin),
                pin,
                current_time()
            )
        )

        customer_id = connection.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        connection.commit()

    finally:
        connection.close()

    return {
        "ok": True,
        "id": customer_id,
        "name": name,
        "phone": phone,
        "pin": pin
    }


# =========================================================
# DELETE CUSTOMER
# =========================================================

@app.delete("/api/admin/customers/{customer_id}")
async def delete_customer(
    customer_id: int,
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    require_admin(auth)

    connection = get_connection()

    try:
        customer = connection.execute(
            """
            SELECT id
            FROM users
            WHERE id = ?
              AND role = 'customer'
            LIMIT 1
            """,
            (customer_id,)
        ).fetchone()

        if not customer:
            raise HTTPException(
                status_code=404,
                detail="Клиент не найден"
            )

        connection.execute(
            """
            UPDATE users
            SET active = 0
            WHERE id = ?
            """,
            (customer_id,)
        )

        connection.execute(
            """
            DELETE FROM sessions
            WHERE user_id = ?
            """,
            (customer_id,)
        )

        connection.commit()

    finally:
        connection.close()

    return {
        "ok": True
    }


# =========================================================
# ADMIN ORDERS
# =========================================================

@app.get("/api/admin/orders")
async def admin_orders(
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    require_admin(auth)

    cutoff = current_time() - 300

    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                o.*,
                cu.name AS customer_name,
                cu.phone AS customer_phone
            FROM orders o
            LEFT JOIN users cu
                ON cu.id = o.customer_id
            WHERE
                o.status != 'closed'
                OR o.closed_at IS NULL
                OR o.closed_at > ?
            ORDER BY o.id DESC
            """,
            (cutoff,)
        ).fetchall()

    finally:
        connection.close()

    return [
        dict(row)
        for row in rows
    ]


# =========================================================
# ADMIN COURIERS
# =========================================================

@app.get("/api/admin/couriers")
async def admin_couriers(
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    require_admin(auth)

    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                co.id,
                co.user_id,
                co.approved,
                co.online,
                co.lat,
                co.lon,
                co.updated_at,
                co.active,
                u.name,
                u.phone
            FROM couriers co
            JOIN users u
                ON u.id = co.user_id
            WHERE co.active = 1
            ORDER BY co.id DESC
            """
        ).fetchall()

    finally:
        connection.close()

    return [
        dict(row)
        for row in rows
    ]


# =========================================================
# CUSTOMER ORDERS
# =========================================================

@app.get("/api/orders")
async def customer_orders(
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    if auth["role"] != "customer":
        raise HTTPException(
            status_code=403,
            detail="Недоступно"
        )

    cutoff = current_time() - 300

    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                o.*,
                u.name AS customer_name,
                u.phone AS customer_phone
            FROM orders o
            LEFT JOIN users u
                ON u.id = o.customer_id
            WHERE o.customer_id = ?
              AND (
                    o.status != 'closed'
                    OR o.closed_at IS NULL
                    OR o.closed_at > ?
                  )
            ORDER BY o.id DESC
            """,
            (
                auth["user_id"],
                cutoff
            )
        ).fetchall()

    finally:
        connection.close()

    return [
        dict(row)
        for row in rows
    ]


# =========================================================
# COURIER ORDERS
# =========================================================

@app.get("/api/courier/orders")
async def courier_orders(
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    if auth["role"] != "courier":
        raise HTTPException(
            status_code=403,
            detail="Недоступно"
        )

    connection = get_connection()

    try:
        courier = connection.execute(
            """
            SELECT id
            FROM couriers
            WHERE user_id = ?
              AND active = 1
            LIMIT 1
            """,
            (auth["user_id"],)
        ).fetchone()

        if not courier:
            return []

        cutoff = current_time() - 300

        rows = connection.execute(
            """
            SELECT
                o.*,
                u.name AS customer_name,
                u.phone AS customer_phone
            FROM orders o
            LEFT JOIN users u
                ON u.id = o.customer_id
            WHERE o.courier_id = ?
              AND (
                    o.status != 'closed'
                    OR o.closed_at IS NULL
                    OR o.closed_at > ?
                  )
            ORDER BY o.id DESC
            """,
            (
                courier["id"],
                cutoff
            )
        ).fetchall()

    finally:
        connection.close()

    return [
        dict(row)
        for row in rows
    ]


# =========================================================
# COURIER ONLINE
# =========================================================

@app.post("/api/courier/online")
async def courier_online(
    authorization: str = Header(default=None)
):
    auth = get_auth(
        authorization
    )

    if auth["role"] != "courier":
        raise HTTPException(
            status_code=403,
            detail="Недоступно"
        )

    connection = get_connection()

    try:
        courier = connection.execute(
            """
            SELECT *
            FROM couriers
            WHERE user_id = ?
              AND active = 1
            LIMIT 1
            """,
            (auth["user_id"],)
        ).fetchone()

        if not courier:
            raise HTTPException(
                status_code=404,
                detail="Курьер не найден"
            )

        online = 0 if courier["online"] else 1

        connection.execute(
            """
            UPDATE couriers
            SET
                online = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                online,
                current_time(),
                courier["id"]
            )
        )

        connection.commit()

    finally:
        connection.close()

    return {
        "ok": True,
        "online": bool(online)
    }


# =========================================================
# TELEGRAM BOT
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    if not WEB_APP_URL:
        await update.message.reply_text(
            "Web App URL не настроен."
        )
        return

    keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "🍽 Открыть приложение",
                    web_app=WebAppInfo(
                        url=WEB_APP_URL
                    )
                )
            ]
        ],
        resize_keyboard=True
    )

    await update.message.reply_text(
        "Добро пожаловать в RESTARAN!",
        reply_markup=keyboard
    )


async def random_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 Поделиться контактом",
                    request_contact=True
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await update.message.reply_text(
        "Поделитесь номером телефона.",
        reply_markup=keyboard
    )


async def contact_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    contact = update.message.contact

    if not contact:
        return

    phone = normalize_phone(
        contact.phone_number
    )

    connection = get_connection()

    try:
        user = connection.execute(
            """
            SELECT
                name,
                phone,
                pin_plain,
                active
            FROM users
            WHERE phone = ?
              AND role = 'customer'
            LIMIT 1
            """,
            (phone,)
        ).fetchone()

    finally:
        connection.close()

    if not user:
        await update.message.reply_text(
            "Аккаунт с таким номером не найден."
        )
        return

    if not user["active"]:
        await update.message.reply_text(
            "Этот аккаунт отключён."
        )
        return

    await update.message.reply_text(
        "Ваш аккаунт:\n\n"
        f"Имя: {user['name']}\n"
        f"Телефон: {user['phone']}\n"
        f"PIN: {user['pin_plain']}"
    )


# =========================================================
# CLEANUP
# =========================================================

async def cleanup_loop():
    while True:
        try:
            cutoff = current_time() - 300

            connection = get_connection()

            try:
                connection.execute(
                    """
                    DELETE FROM orders
                    WHERE status = 'closed'
                      AND closed_at IS NOT NULL
                      AND closed_at <= ?
                    """,
                    (cutoff,)
                )

                connection.execute(
                    """
                    DELETE FROM sessions
                    WHERE created_at <= ?
                    """,
                    (
                        current_time() - 2592000,
                    )
                )

                connection.commit()

            finally:
                connection.close()

        except Exception as error:
            print(
                "Cleanup error:",
                repr(error)
            )

        await asyncio.sleep(60)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False
    )
