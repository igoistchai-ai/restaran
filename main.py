import os
import re
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
import asyncio
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

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


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DB_PATH = os.path.join(
    BASE_DIR,
    "restaran.db"
)

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    ""
).strip()

WEB_APP_URL = os.getenv(
    "WEB_APP_URL",
    "https://restaran-xtny.onrender.com"
).strip()

DEFAULT_ADMIN_IDS = {
    "8357023784",
    "7003441441",
}

ADMIN_IDS = set(
    x.strip()
    for x in os.getenv(
        "ADMIN_IDS",
        ",".join(DEFAULT_ADMIN_IDS)
    ).split(",")
    if x.strip()
)


# ============================================================
# DATABASE
# ============================================================

def connection():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA foreign_keys=ON"
    )

    return conn


def db():

    return connection()


def ensure_column(
    conn,
    table,
    column,
    definition
):

    columns = [
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    ]

    if column not in columns:

        conn.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN {column} {definition}
            """
        )


def init_db():

    conn = db()

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            pin_hash TEXT NOT NULL DEFAULT '',
            pin_plain TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'customer',
            telegram_id TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS couriers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,
            online INTEGER NOT NULL DEFAULT 0,
            lat REAL,
            lon REAL,
            updated_at INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            courier_id INTEGER,
            title TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            price REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'new',
            created_at INTEGER NOT NULL,
            customer_confirmed INTEGER NOT NULL DEFAULT 0,
            closed_at INTEGER,
            FOREIGN KEY(customer_id)
                REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
        );
        """
    )

    ensure_column(
        conn,
        "users",
        "pin_plain",
        "TEXT NOT NULL DEFAULT ''"
    )

    ensure_column(
        conn,
        "users",
        "telegram_id",
        "TEXT"
    )

    ensure_column(
        conn,
        "users",
        "active",
        "INTEGER NOT NULL DEFAULT 1"
    )

    ensure_column(
        conn,
        "orders",
        "customer_confirmed",
        "INTEGER NOT NULL DEFAULT 0"
    )

    ensure_column(
        conn,
        "orders",
        "closed_at",
        "INTEGER"
    )

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

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


def check_pin(pin, stored):

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


def new_token():

    return secrets.token_urlsafe(48)


def user_dict(row):

    if not row:
        return None

    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "role": row["role"],
        "telegram_id": row["telegram_id"],
        "active": bool(row["active"]),
    }


def get_auth(
    authorization: str | None
):

    if not authorization:

        raise HTTPException(
            401,
            "Авторизация отсутствует"
        )

    if authorization.startswith(
        "Bearer "
    ):

        token = authorization[7:].strip()

    else:

        token = authorization.strip()

    if not token:

        raise HTTPException(
            401,
            "Токен отсутствует"
        )

    conn = db()

    row = conn.execute(
        """
        SELECT
            s.token,
            s.user_id,
            s.role,
            u.name,
            u.phone,
            u.telegram_id,
            u.active
        FROM sessions s
        JOIN users u
            ON u.id=s.user_id
        WHERE s.token=?
          AND u.active=1
        """,
        (token,)
    ).fetchone()

    conn.close()

    if not row:

        raise HTTPException(
            401,
            "Сессия недействительна"
        )

    return row


def admin_only(auth):

    if auth["role"] != "admin":

        raise HTTPException(
            403,
            "Доступ только для администратора"
        )


def create_session(
    user_id,
    role
):

    token = new_token()

    conn = db()

    conn.execute(
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

    conn.commit()
    conn.close()

    return token


# ============================================================
# TELEGRAM INIT DATA
# ============================================================

def validate_init_data(
    init_data
):

    if not BOT_TOKEN:

        raise ValueError(
            "BOT_TOKEN не задан на Render"
        )

    if not init_data:

        raise ValueError(
            "Telegram initData отсутствует"
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
        f"{key}={data[key]}"
        for key in sorted(data)
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
            "Неверная подпись Telegram"
        )

    auth_date = int(
        data.get(
            "auth_date",
            "0"
        )
    )

    if not auth_date:

        raise ValueError(
            "auth_date отсутствует"
        )

    if now() - auth_date > 86400:

        raise ValueError(
            "Telegram initData устарел"
        )

    raw_user = data.get(
        "user",
        "{}"
    )

    try:

        telegram_user = json.loads(
            raw_user
        )

    except Exception:

        raise ValueError(
            "Неверный Telegram user"
        )

    if not telegram_user.get("id"):

        raise ValueError(
            "Telegram ID отсутствует"
        )

    return telegram_user


# ============================================================
# MODELS
# ============================================================

class LoginRequest(BaseModel):

    phone: str
    pin: str


class AdminWebLogin(BaseModel):

    init_data: str


class CustomerCreate(BaseModel):

    name: str
    phone: str
    pin: str


class OrderCreate(BaseModel):

    customer_id: int
    title: str = ""
    address: str = ""
    price: float = 0


# ============================================================
# CLEANUP
# ============================================================

async def cleanup_loop():

    while True:

        try:

            cutoff = now() - 300

            conn = db()

            conn.execute(
                """
                DELETE FROM orders
                WHERE status='closed'
                  AND closed_at IS NOT NULL
                  AND closed_at <= ?
                """,
                (cutoff,)
            )

            conn.commit()
            conn.close()

        except Exception:

            pass

        await asyncio.sleep(30)


# ============================================================
# TELEGRAM BOT
# ============================================================

telegram_app = None


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    keyboard = [
        [
            KeyboardButton(
                "🍽 Открыть RESTARAN",
                web_app=WebAppInfo(
                    url=WEB_APP_URL
                )
            )
        ],
        [
            KeyboardButton(
                "🔐 Получить PIN",
                request_contact=True
            )
        ]
    ]

    await update.message.reply_text(
        "RESTARAN",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
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

    conn = db()

    row = conn.execute(
        """
        SELECT name, pin_plain
        FROM users
        WHERE role='customer'
          AND active=1
          AND phone=?
        """,
        (phone,)
    ).fetchone()

    conn.close()

    if not row:

        await update.message.reply_text(
            "Аккаунт по этому номеру не найден."
        )

        return

    await update.message.reply_text(
        f"Ваш PIN: {row['pin_plain']}"
    )


# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app):

    global telegram_app

    cleanup_task = asyncio.create_task(
        cleanup_loop()
    )

    if BOT_TOKEN:

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

    yield

    cleanup_task.cancel()

    if telegram_app:

        try:

            if telegram_app.updater:

                await telegram_app.updater.stop()

            await telegram_app.stop()
            await telegram_app.shutdown()

        except Exception:

            pass


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="RESTARAN",
    lifespan=lifespan
)


# ============================================================
# FRONTEND
# ============================================================

@app.get("/")
async def index():

    return FileResponse(
        os.path.join(
            BASE_DIR,
            "index.html"
        )
    )


# ============================================================
# ADMIN WEB LOGIN
# ============================================================

@app.post(
    "/api/admin/web-login"
)
async def admin_web_login(
    payload: AdminWebLogin
):

    try:

        tg_user = validate_init_data(
            payload.init_data
        )

    except Exception as e:

        raise HTTPException(
            403,
            f"Не удалось проверить администратора: {e}"
        )

    telegram_id = str(
        tg_user["id"]
    )

    if telegram_id not in ADMIN_IDS:

        raise HTTPException(
            403,
            "Доступ запрещён"
        )

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE telegram_id=?
          AND role='admin'
        """,
        (telegram_id,)
    ).fetchone()

    if row:

        conn.execute(
            """
            UPDATE users
            SET active=1,
                name=?
            WHERE id=?
            """,
            (
                tg_user.get(
                    "first_name",
                    "Admin"
                ),
                row["id"]
            )
        )

        user_id = row["id"]

    else:

        cursor = conn.execute(
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
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                tg_user.get(
                    "first_name",
                    "Admin"
                ),
                "",
                "",
                "",
                "admin",
                telegram_id,
                1,
                now()
            )
        )

        user_id = cursor.lastrowid

    conn.commit()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE id=?
        """,
        (user_id,)
    ).fetchone()

    conn.close()

    token = create_session(
        user_id,
        "admin"
    )

    return {
        "ok": True,
        "token": token,
        "role": "admin",
        "user": user_dict(row)
    }


# ============================================================
# LOGIN
# ============================================================

@app.post(
    "/api/login"
)
async def login(
    payload: LoginRequest
):

    phone = norm_phone(
        payload.phone
    )

    pin = payload.pin.strip()

    if not phone or not pin:

        raise HTTPException(
            400,
            "Введите телефон и PIN"
        )

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE phone=?
          AND role='customer'
          AND active=1
        """,
        (phone,)
    ).fetchone()

    if not row:

        conn.close()

        raise HTTPException(
            401,
            "Неверный телефон или PIN"
        )

    if not check_pin(
        pin,
        row["pin_hash"]
    ):

        conn.close()

        raise HTTPException(
            401,
            "Неверный телефон или PIN"
        )

    conn.close()

    token = create_session(
        row["id"],
        row["role"]
    )

    return {
        "ok": True,
        "token": token,
        "role": row["role"],
        "user": user_dict(row)
    }


# ============================================================
# ME
# ============================================================

@app.get(
    "/api/me"
)
async def me(
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE id=?
        """,
        (auth["user_id"],)
    ).fetchone()

    conn.close()

    return {
        "role": row["role"],
        "user": user_dict(row)
    }


# ============================================================
# LOGOUT
# ============================================================

@app.post(
    "/api/logout"
)
async def logout(
    authorization: str | None = Header(None)
):

    if authorization:

        token = authorization

        if token.startswith("Bearer "):

            token = token[7:].strip()

        conn = db()

        conn.execute(
            "DELETE FROM sessions WHERE token=?",
            (token,)
        )

        conn.commit()
        conn.close()

    return {
        "ok": True
    }


# ============================================================
# ADMIN CUSTOMERS
# ============================================================

@app.get(
    "/api/admin/customers"
)
async def admin_customers(
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    conn = db()

    rows = conn.execute(
        """
        SELECT
            id,
            name,
            phone,
            pin_plain,
            role,
            active,
            created_at
        FROM users
        WHERE role='customer'
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return [
        dict(row)
        for row in rows
    ]


# ============================================================
# CREATE CUSTOMER
# ============================================================

@app.post(
    "/api/admin/customers"
)
async def create_customer(
    payload: CustomerCreate,
    authorization: str | None = Header(None)
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
            "Имя не может быть пустым"
        )

    if not phone:

        raise HTTPException(
            400,
            "Номер телефона не может быть пустым"
        )

    if not re.fullmatch(
        r"\d{4,8}",
        pin
    ):

        raise HTTPException(
            400,
            "PIN должен содержать 4-8 цифр"
        )

    conn = db()

    existing = conn.execute(
        """
        SELECT id
        FROM users
        WHERE phone=?
          AND role='customer'
        """,
        (phone,)
    ).fetchone()

    if existing:

        conn.close()

        raise HTTPException(
            409,
            "Клиент с таким номером уже существует"
        )

    cursor = conn.execute(
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
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            name,
            phone,
            hash_pin(pin),
            pin,
            "customer",
            1,
            now()
        )
    )

    user_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "id": user_id
    }


# ============================================================
# DELETE CUSTOMER
# ============================================================

@app.delete(
    "/api/admin/customers/{customer_id}"
)
async def delete_customer(
    customer_id: int,
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    conn = db()

    row = conn.execute(
        """
        SELECT id
        FROM users
        WHERE id=?
          AND role='customer'
        """,
        (customer_id,)
    ).fetchone()

    if not row:

        conn.close()

        raise HTTPException(
            404,
            "Клиент не найден"
        )

    conn.execute(
        """
        UPDATE users
        SET active=0
        WHERE id=?
        """,
        (customer_id,)
    )

    conn.execute(
        """
        DELETE FROM sessions
        WHERE user_id=?
        """,
        (customer_id,)
    )

    conn.commit()
    conn.close()

    return {
        "ok": True
    }


# ============================================================
# ADMIN STATS
# ============================================================

@app.get(
    "/api/admin/stats"
)
async def admin_stats(
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    conn = db()

    customers = conn.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE role='customer'
          AND active=1
        """
    ).fetchone()[0]

    couriers = conn.execute(
        """
        SELECT COUNT(*)
        FROM couriers c
        JOIN users u
          ON u.id=c.user_id
        WHERE c.active=1
          AND u.active=1
        """
    ).fetchone()[0]

    orders = conn.execute(
        """
        SELECT COUNT(*)
        FROM orders
        """
    ).fetchone()[0]

    active_orders = conn.execute(
        """
        SELECT COUNT(*)
        FROM orders
        WHERE status!='closed'
        """
    ).fetchone()[0]

    conn.close()

    return {
        "customers": customers,
        "couriers": couriers,
        "orders": orders,
        "active_orders": active_orders
    }


# ============================================================
# ADMIN ORDERS
# ============================================================

@app.get(
    "/api/admin/orders"
)
async def admin_orders(
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    conn = db()

    rows = conn.execute(
        """
        SELECT
            o.*,
            u.name AS customer_name,
            u.phone AS customer_phone
        FROM orders o
        LEFT JOIN users u
          ON u.id=o.customer_id
        ORDER BY o.id DESC
        """
    ).fetchall()

    conn.close()

    return [
        dict(row)
        for row in rows
    ]


# ============================================================
# ADMIN COURIERS
# ============================================================

@app.get(
    "/api/admin/couriers"
)
async def admin_couriers(
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    conn = db()

    rows = conn.execute(
        """
        SELECT
            c.id,
            c.user_id,
            c.approved,
            c.online,
            c.lat,
            c.lon,
            c.updated_at,
            u.name,
            u.phone,
            u.active
        FROM couriers c
        JOIN users u
          ON u.id=c.user_id
        ORDER BY c.id DESC
        """
    ).fetchall()

    conn.close()

    return [
        dict(row)
        for row in rows
    ]


# ============================================================
# CUSTOMER ORDERS
# ============================================================

@app.get(
    "/api/orders"
)
async def customer_orders(
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    if auth["role"] != "customer":

        raise HTTPException(
            403,
            "Доступ запрещён"
        )

    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM orders
        WHERE customer_id=?
        ORDER BY id DESC
        """,
        (auth["user_id"],)
    ).fetchall()

    conn.close()

    return [
        dict(row)
        for row in rows
    ]


# ============================================================
# CREATE ORDER
# ============================================================

@app.post(
    "/api/admin/orders"
)
async def create_order(
    payload: OrderCreate,
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    admin_only(auth)

    conn = db()

    customer = conn.execute(
        """
        SELECT id
        FROM users
        WHERE id=?
          AND role='customer'
          AND active=1
        """,
        (payload.customer_id,)
    ).fetchone()

    if not customer:

        conn.close()

        raise HTTPException(
            404,
            "Клиент не найден"
        )

    cursor = conn.execute(
        """
        INSERT INTO orders(
            customer_id,
            title,
            address,
            price,
            status,
            created_at
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            payload.customer_id,
            payload.title,
            payload.address,
            payload.price,
            "new",
            now()
        )
    )

    order_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "id": order_id
    }


# ============================================================
# COURIER ORDERS
# ============================================================

@app.get(
    "/api/courier/orders"
)
async def courier_orders(
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    if auth["role"] != "courier":

        raise HTTPException(
            403,
            "Доступ запрещён"
        )

    conn = db()

    courier = conn.execute(
        """
        SELECT id
        FROM couriers
        WHERE user_id=?
          AND active=1
        """,
        (auth["user_id"],)
    ).fetchone()

    if not courier:

        conn.close()

        raise HTTPException(
            403,
            "Курьер не найден"
        )

    rows = conn.execute(
        """
        SELECT
            o.*,
            u.name AS customer_name,
            u.phone AS customer_phone
        FROM orders o
        JOIN users u
          ON u.id=o.customer_id
        WHERE o.courier_id=?
        ORDER BY o.id DESC
        """,
        (courier["id"],)
    ).fetchall()

    conn.close()

    return [
        dict(row)
        for row in rows
    ]


# ============================================================
# COURIER ONLINE
# ============================================================

@app.post(
    "/api/courier/online"
)
async def courier_online(
    authorization: str | None = Header(None)
):

    auth = get_auth(
        authorization
    )

    if auth["role"] != "courier":

        raise HTTPException(
            403,
            "Доступ запрещён"
        )

    conn = db()

    courier = conn.execute(
        """
        SELECT *
        FROM couriers
        WHERE user_id=?
          AND active=1
        """,
        (auth["user_id"],)
    ).fetchone()

    if not courier:

        conn.close()

        raise HTTPException(
            404,
            "Курьер не найден"
        )

    new_value = 0 if courier["online"] else 1

    conn.execute(
        """
        UPDATE couriers
        SET online=?,
            updated_at=?
        WHERE id=?
        """,
        (
            new_value,
            now(),
            courier["id"]
        )
    )

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "online": bool(new_value)
    }


# ============================================================
# HEALTH
# ============================================================

@app.get(
    "/health"
)
async def health():

    return {
        "ok": True,
        "service": "RESTARAN",
        "time": now()
    }


# ============================================================
# RUN
# ============================================================

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
