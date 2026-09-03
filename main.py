import os
import re
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
import asyncio
from contextlib import contextmanager
from urllib.parse import parse_qsl

from fastapi import (
    FastAPI,
    HTTPException,
    Header,
)
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

    raw = os.getenv(
        "ADMIN_IDS",
        ""
    ).strip()

    if not raw:
        return DEFAULT_ADMIN_IDS.copy()

    result = set()

    for item in raw.split(","):

        item = item.strip()

        if not item:
            continue

        try:
            result.add(int(item))
        except ValueError:
            pass

    return result or DEFAULT_ADMIN_IDS.copy()


ADMIN_IDS = parse_admin_ids()


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    title="RESTARAN",
    version="2.0"
)


# =========================================================
# DATABASE
# =========================================================

@contextmanager
def db():

    connection = sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False
    )

    connection.row_factory = sqlite3.Row

    try:
        yield connection
        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def column_exists(
    connection,
    table,
    column
):

    rows = connection.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return any(
        row["name"] == column
        for row in rows
    )


def add_column_if_missing(
    connection,
    table,
    column,
    definition
):

    if not column_exists(
        connection,
        table,
        column
    ):

        connection.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN {column} {definition}
            """
        )


def init_db():

    with db() as c:

        c.execute("""
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

        c.execute("""
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

        c.execute("""
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

        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        add_column_if_missing(
            c,
            "users",
            "pin_plain",
            "TEXT"
        )

        add_column_if_missing(
            c,
            "users",
            "telegram_id",
            "INTEGER"
        )

        add_column_if_missing(
            c,
            "users",
            "active",
            "INTEGER NOT NULL DEFAULT 1"
        )

        add_column_if_missing(
            c,
            "orders",
            "customer_confirmed",
            "INTEGER DEFAULT 0"
        )

        add_column_if_missing(
            c,
            "orders",
            "closed_at",
            "INTEGER"
        )

        c.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_users_phone
            ON users(phone)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_users_telegram
            ON users(telegram_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_orders_customer
            ON orders(customer_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_orders_status
            ON orders(status)
        """)


init_db()


# =========================================================
# HELPERS
# =========================================================

def now():
    return int(time.time())


def norm_phone(phone):

    return re.sub(
        r"\D",
        "",
        phone or ""
    )


def hash_pin(pin):

    salt = secrets.token_hex(16)

    digest = hashlib.sha256(
        (salt + pin).encode()
    ).hexdigest()

    return salt + ":" + digest


def check_pin(
    pin,
    stored
):

    if not stored:
        return False

    try:

        salt, digest = stored.split(
            ":",
            1
        )

        calculated = hashlib.sha256(
            (salt + pin).encode()
        ).hexdigest()

        return hmac.compare_digest(
            calculated,
            digest
        )

    except Exception:

        return False


def create_session(
    user_id,
    role
):

    token = secrets.token_urlsafe(48)

    with db() as c:

        c.execute(
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
                now()
            )
        )

    return token


def get_auth(
    authorization: str | None
):

    if not authorization:
        raise HTTPException(
            401,
            "Требуется авторизация"
        )

    if not authorization.startswith(
        "Bearer "
    ):
        raise HTTPException(
            401,
            "Неверный токен"
        )

    token = authorization[
        7:
    ].strip()

    if not token:
        raise HTTPException(
            401,
            "Пустой токен"
        )

    with db() as c:

        row = c.execute(
            """
            SELECT
                s.*,
                u.name,
                u.phone,
                u.active
            FROM sessions s
            JOIN users u
                ON u.id=s.user_id
            WHERE s.token=?
            """,
            (token,)
        ).fetchone()

    if not row:
        raise HTTPException(
            401,
            "Сессия не найдена"
        )

    if not row["active"]:
        raise HTTPException(
            403,
            "Аккаунт отключён"
        )

    return row


def admin_only(
    auth
):

    if auth["role"] != "admin":

        raise HTTPException(
            403,
            "Доступ только для администратора"
        )


# =========================================================
# TELEGRAM INIT DATA
# =========================================================

def validate_init_data(
    init_data
):

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
        for key, value
        in sorted(data.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        check_string.encode(),
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

    if time.time() - auth_date > 86400:

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
# MODELS
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
# STATIC
# =========================================================

@app.get("/")
async def index():

    return FileResponse(
        "index.html"
    )


# =========================================================
# NORMAL LOGIN
# =========================================================

@app.post("/api/login")
async def login(
    payload: LoginRequest
):

    phone = norm_phone(
        payload.phone
    )

    pin = payload.pin.strip()

    if not phone:

        raise HTTPException(
            400,
            "Введите номер телефона"
        )

    if not pin:

        raise HTTPException(
            400,
            "Введите PIN"
        )

    with db() as c:

        user = c.execute(
            """
            SELECT *
            FROM users
            WHERE phone=?
              AND active=1
            LIMIT 1
            """,
            (phone,)
        ).fetchone()

    if not user:

        raise HTTPException(
            401,
            "Пользователь не найден"
        )

    if not check_pin(
        pin,
        user["pin_hash"]
    ):

        raise HTTPException(
            401,
            "Неверный PIN"
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
    authorization: str = Header(None)
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
# ADMIN TELEGRAM LOGIN
# =========================================================

@app.post("/api/admin/web-login")
async def admin_web_login(
    payload: AdminWebLogin
):

    try:

        telegram_user = validate_init_data(
            payload.init_data
        )

    except Exception as error:

        raise HTTPException(
            403,
            f"Не удалось проверить администратора: {error}"
        )

    telegram_id = int(
        telegram_user["id"]
    )

    /*
    IMPORTANT:
    ONLY these IDs are admins.
    */

    if telegram_id not in ADMIN_IDS:

        raise HTTPException(
            403,
            "Доступ запрещён"
        )

    name = (
        telegram_user.get("first_name")
        or telegram_user.get("username")
        or "Administrator"
    )

    with db() as c:

        admin = c.execute(
            """
            SELECT *
            FROM users
            WHERE telegram_id=?
              AND role='admin'
            LIMIT 1
            """,
            (telegram_id,)
        ).fetchone()

        if admin:

            c.execute(
                """
                UPDATE users
                SET
                    name=?,
                    active=1
                WHERE id=?
                """,
                (
                    name,
                    admin["id"]
                )
            )

            admin_id = admin["id"]

        else:

            c.execute(
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
                    f"telegram:{telegram_id}",
                    telegram_id,
                    now()
                )
            )

            admin_id = c.lastrowid

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
    authorization: str = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    with db() as c:

        customers = c.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE role='customer'
              AND active=1
            """
        ).fetchone()[0]

        couriers = c.execute(
            """
            SELECT COUNT(*)
            FROM couriers
            WHERE active=1
            """
        ).fetchone()[0]

        orders = c.execute(
            """
            SELECT COUNT(*)
            FROM orders
            """
        ).fetchone()[0]

        active_orders = c.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE status NOT IN(
                'closed',
                'cancelled'
            )
            """
        ).fetchone()[0]

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
    authorization: str = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    with db() as c:

        rows = c.execute(
            """
            SELECT
                id,
                name,
                phone,
                pin_plain,
                active,
                created_at
            FROM users
            WHERE role='customer'
            ORDER BY id DESC
            """
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


@app.post("/api/admin/customers")
async def create_customer(
    payload: CustomerCreate,
    authorization: str = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    name = payload.name.strip()
    phone = norm_phone(
        payload.phone
    )
    pin = payload.pin.strip()

    if not name:

        raise HTTPException(
            400,
            "Введите имя"
        )

    if not phone:

        raise HTTPException(
            400,
            "Введите номер телефона"
        )

    if not re.fullmatch(
        r"\d{4,8}",
        pin
    ):

        raise HTTPException(
            400,
            "PIN должен содержать 4-8 цифр"
        )

    with db() as c:

        existing = c.execute(
            """
            SELECT id
            FROM users
            WHERE phone=?
              AND role='customer'
              AND active=1
            LIMIT 1
            """,
            (phone,)
        ).fetchone()

        if existing:

            raise HTTPException(
                409,
                "Клиент с таким номером уже существует"
            )

        c.execute(
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
                now()
            )
        )

        customer_id = c.lastrowid

    return {
        "ok": True,
        "id": customer_id,
        "name": name,
        "phone": phone
    }


# =========================================================
# DELETE CUSTOMER
# =========================================================

@app.delete("/api/admin/customers/{customer_id}")
async def delete_customer(
    customer_id: int,
    authorization: str = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    with db() as c:

        customer = c.execute(
            """
            SELECT id
            FROM users
            WHERE id=?
              AND role='customer'
            LIMIT 1
            """,
            (customer_id,)
        ).fetchone()

        if not customer:

            raise HTTPException(
                404,
                "Клиент не найден"
            )

        /*
        Soft delete.
        История заказов сохраняется.
        */

        c.execute(
            """
            UPDATE users
            SET active=0
            WHERE id=?
            """,
            (customer_id,)
        )

        c.execute(
            """
            DELETE FROM sessions
            WHERE user_id=?
            """,
            (customer_id,)
        )

    return {
        "ok": True
    }


# =========================================================
# ADMIN ORDERS
# =========================================================

@app.get("/api/admin/orders")
async def admin_orders(
    authorization: str = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    with db() as c:

        rows = c.execute(
            """
            SELECT
                o.*,
                cu.name AS customer_name,
                cu.phone AS customer_phone
            FROM orders o
            LEFT JOIN users cu
                ON cu.id=o.customer_id
            WHERE
                o.status != 'closed'
                OR o.closed_at IS NULL
                OR o.closed_at > ?
            ORDER BY o.id DESC
            """,
            (
                now() - 300,
            )
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


# =========================================================
# ADMIN COURIERS
# =========================================================

@app.get("/api/admin/couriers")
async def admin_couriers(
    authorization: str = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    with db() as c:

        rows = c.execute(
            """
            SELECT
                co.*,
                u.name,
                u.phone
            FROM couriers co
            JOIN users u
                ON u.id=co.user_id
            WHERE co.active=1
            ORDER BY co.id DESC
            """
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


# =========================================================
# CUSTOMER ORDERS
# =========================================================

@app.get("/api/orders")
async def customer_orders(
    authorization: str = Header(None)
):

    auth = get_auth(
        authorization
    )

    if auth["role"] != "customer":

        raise HTTPException(
            403,
            "Недоступно"
        )

    with db() as c:

        rows = c.execute(
            """
            SELECT *
            FROM orders
            WHERE customer_id=?
              AND (
                    status != 'closed'
                    OR closed_at IS NULL
                    OR closed_at > ?
                  )
            ORDER BY id DESC
            """,
            (
                auth["user_id"],
                now() - 300
            )
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


# =========================================================
# COURIER ORDERS
# =========================================================

@app.get("/api/courier/orders")
async def courier_orders(
    authorization: str = Header(None)
):

    auth = get_auth(
        authorization
    )

    if auth["role"] != "courier":

        raise HTTPException(
            403,
            "Недоступно"
        )

    with db() as c:

        courier = c.execute(
            """
            SELECT id
            FROM couriers
            WHERE user_id=?
              AND active=1
            LIMIT 1
            """,
            (auth["user_id"],)
        ).fetchone()

        if not courier:

            return []

        rows = c.execute(
            """
            SELECT
                o.*,
                u.name AS customer_name,
                u.phone AS customer_phone
            FROM orders o
            LEFT JOIN users u
                ON u.id=o.customer_id
            WHERE o.courier_id=?
              AND (
                    o.status != 'closed'
                    OR o.closed_at IS NULL
                    OR o.closed_at > ?
                  )
            ORDER BY o.id DESC
            """,
            (
                courier["id"],
                now() - 300
            )
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


# =========================================================
# COURIER ONLINE
# =========================================================

@app.post("/api/courier/online")
async def courier_online(
    authorization: str = Header(None)
):

    auth = get_auth(
        authorization
    )

    if auth["role"] != "courier":

        raise HTTPException(
            403,
            "Недоступно"
        )

    with db() as c:

        courier = c.execute(
            """
            SELECT *
            FROM couriers
            WHERE user_id=?
              AND active=1
            LIMIT 1
            """,
            (auth["user_id"],)
        ).fetchone()

        if not courier:

            raise HTTPException(
                404,
                "Курьер не найден"
            )

        new_status = 0 if courier["online"] else 1

        c.execute(
            """
            UPDATE couriers
            SET
                online=?,
                updated_at=?
            WHERE id=?
            """,
            (
                new_status,
                now(),
                courier["id"]
            )
        )

    return {
        "ok": True,
        "online": bool(new_status)
    }


# =========================================================
# TELEGRAM BOT
# =========================================================

telegram_app = None


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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
        "Поделитесь номером телефона, "
        "чтобы получить данные аккаунта.",
        reply_markup=keyboard
    )


async def contact_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    contact = update.message.contact

    if not contact:

        return

    phone = norm_phone(
        contact.phone_number
    )

    with db() as c:

        user = c.execute(
            """
            SELECT
                name,
                phone,
                pin_plain,
                active
            FROM users
            WHERE phone=?
              AND role='customer'
            LIMIT 1
            """,
            (phone,)
        ).fetchone()

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
# START TELEGRAM
# =========================================================

async def start_telegram():

    global telegram_app

    if not BOT_TOKEN:

        print(
            "BOT_TOKEN не задан. "
            "Telegram bot не запущен."
        )

        return

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

    print(
        "Telegram bot started"
    )


async def stop_telegram():

    global telegram_app

    if not telegram_app:
        return

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


# =========================================================
# CLEANUP
# =========================================================

async def cleanup_loop():

    while True:

        try:

            with db() as c:

                cutoff = now() - 300

                c.execute(
                    """
                    DELETE FROM orders
                    WHERE status='closed'
                      AND closed_at IS NOT NULL
                      AND closed_at <= ?
                    """,
                    (cutoff,)
                )

                c.execute(
                    """
                    DELETE FROM sessions
                    WHERE created_at <= ?
                    """,
                    (
                        now() - 2592000,
                    )
                )

        except Exception as error:

            print(
                "Cleanup error:",
                error
            )

        await asyncio.sleep(60)


cleanup_task = None


@app.on_event("startup")
async def startup():

    global cleanup_task

    init_db()

    await start_telegram()

    cleanup_task = asyncio.create_task(
        cleanup_loop()
    )


@app.on_event("shutdown")
async def shutdown():

    global cleanup_task

    if cleanup_task:

        cleanup_task.cancel()

        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

    await stop_telegram()


# =========================================================
# HEALTH
# =========================================================

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
        app,
        host="0.0.0.0",
        port=port
    )
