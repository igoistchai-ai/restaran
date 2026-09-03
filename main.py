# main.py
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
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import uvicorn


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEB_APP_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "restaran.db")

ADMIN_IDS = {
    8357023784,
    7003441441,
}

app = FastAPI(title="RESTARAN")
bot_app = None


# ============================================================
# DATABASE
# ============================================================

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT UNIQUE,
                pin_hash TEXT,
                pin_plain TEXT,
                role TEXT NOT NULL,
                telegram_id INTEGER,
                active INTEGER DEFAULT 1,
                created_at REAL NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                approved INTEGER DEFAULT 0,
                online INTEGER DEFAULT 0,
                lat REAL,
                lon REAL,
                updated_at REAL,
                active INTEGER DEFAULT 1
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                courier_id INTEGER,
                title TEXT NOT NULL,
                address TEXT NOT NULL,
                price REAL DEFAULT 0,
                status TEXT DEFAULT 'new',
                created_at REAL NOT NULL,
                customer_confirmed INTEGER DEFAULT 0,
                closed_at REAL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)

        migrations = [
            ("users", "pin_plain", "TEXT"),
            ("users", "telegram_id", "INTEGER"),
            ("users", "active", "INTEGER DEFAULT 1"),
            ("couriers", "lat", "REAL"),
            ("couriers", "lon", "REAL"),
            ("couriers", "updated_at", "REAL"),
            ("couriers", "active", "INTEGER DEFAULT 1"),
            ("orders", "customer_confirmed", "INTEGER DEFAULT 0"),
            ("orders", "closed_at", "REAL"),
        ]

        for table, column, definition in migrations:
            columns = conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()

            names = {row["name"] for row in columns}

            if column not in names:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )


# ============================================================
# HELPERS
# ============================================================

def norm_phone(phone):
    return re.sub(r"\D", "", phone or "")


def hash_pin(pin):
    salt = secrets.token_hex(16)

    digest = hashlib.sha256(
        (salt + pin).encode()
    ).hexdigest()

    return salt + ":" + digest


def check_pin(pin, stored):
    try:
        salt, digest = stored.split(":", 1)

        actual = hashlib.sha256(
            (salt + pin).encode()
        ).hexdigest()

        return hmac.compare_digest(actual, digest)

    except Exception:
        return False


def new_token():
    return secrets.token_urlsafe(48)


def validate_init_data(init_data):

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

    received_hash = data.pop("hash", None)

    if not received_hash:
        raise ValueError(
            "Hash отсутствует"
        )

    check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(data.items())
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
            data.get("auth_date", "0")
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
            data.get("user", "{}")
        )
    except Exception:
        raise ValueError(
            "Невозможно прочитать Telegram user"
        )

    if not user.get("id"):
        raise ValueError(
            "Telegram user отсутствует"
        )

    return user


def auth_user(authorization):

    if not authorization:
        raise HTTPException(
            401,
            "Нет авторизации"
        )

    token = authorization.replace(
        "Bearer ",
        ""
    ).strip()

    with db() as conn:

        row = conn.execute("""
            SELECT
                sessions.*,
                users.name,
                users.phone,
                users.active
            FROM sessions
            JOIN users
                ON users.id = sessions.user_id
            WHERE sessions.token = ?
        """, (token,)).fetchone()

    if not row:
        raise HTTPException(
            401,
            "Сессия недействительна"
        )

    if not row["active"]:
        raise HTTPException(
            403,
            "Аккаунт деактивирован"
        )

    return dict(row)


def admin_only(user):

    if user["role"] != "admin":
        raise HTTPException(
            403,
            "Только администратор"
        )


# ============================================================
# MODELS
# ============================================================

class LoginIn(BaseModel):
    phone: str
    pin: str


class AdminWebLogin(BaseModel):
    init_data: str


class CustomerCreate(BaseModel):
    name: str
    phone: str
    pin: str


class CourierCreate(BaseModel):
    name: str
    phone: str
    pin: str = ""


class OrderCreate(BaseModel):
    phone: str
    title: str
    address: str
    price: float = 0


class AssignIn(BaseModel):
    courier_id: int


class OnlineIn(BaseModel):
    online: bool


class LocationIn(BaseModel):
    lat: float
    lon: float


# ============================================================
# TELEGRAM
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not WEB_APP_URL:

        await update.message.reply_text(
            "Web App URL ещё не настроен."
        )

        return

    keyboard = [[
        KeyboardButton(
            "🍽 Открыть приложение",
            web_app=WebAppInfo(
                url=WEB_APP_URL
            )
        )
    ]]

    await update.message.reply_text(
        "Добро пожаловать в RESTARAN.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


async def random_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    keyboard = [[
        KeyboardButton(
            "📱 Поделиться контактом",
            request_contact=True
        )
    ]]

    await update.message.reply_text(
        "Поделитесь контактом клиента, "
        "чтобы получить его PIN.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


async def contact_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.contact:
        return

    phone = norm_phone(
        update.message.contact.phone_number
    )

    with db() as conn:

        row = conn.execute("""
            SELECT
                name,
                phone,
                pin_plain
            FROM users
            WHERE role = 'customer'
              AND phone = ?
              AND active = 1
        """, (phone,)).fetchone()

    if not row:

        await update.message.reply_text(
            "Клиент с таким номером не найден."
        )

        return

    await update.message.reply_text(
        "Клиент: "
        + str(row["name"])
        + "\nТелефон: "
        + str(row["phone"])
        + "\nPIN: "
        + str(row["pin_plain"] or "—")
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    init_db()

    asyncio.create_task(
        cleanup_loop()
    )

    global bot_app

    if not BOT_TOKEN:
        return

    bot_app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    bot_app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    bot_app.add_handler(
        CommandHandler(
            "random",
            random_command
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.CONTACT,
            contact_handler
        )
    )

    await bot_app.initialize()
    await bot_app.start()

    if bot_app.updater:
        await bot_app.updater.start_polling()


@app.on_event("shutdown")
async def shutdown():

    global bot_app

    if not bot_app:
        return

    try:

        if bot_app.updater:
            await bot_app.updater.stop()

        await bot_app.stop()
        await bot_app.shutdown()

    except Exception:
        pass


async def cleanup_loop():

    while True:

        try:

            with db() as conn:

                conn.execute("""
                    DELETE FROM orders
                    WHERE status = 'closed'
                      AND closed_at IS NOT NULL
                      AND closed_at <= ?
                """, (
                    time.time() - 300,
                ))

        except Exception:
            pass

        await asyncio.sleep(30)


# ============================================================
# FRONT PAGE
# ============================================================

@app.get("/")
async def index():
    return FileResponse(
        "index.html"
    )


# ============================================================
# AUTH
# ============================================================

@app.post("/api/login")
async def login(data: LoginIn):

    phone = norm_phone(
        data.phone
    )

    with db() as conn:

        user = conn.execute("""
            SELECT *
            FROM users
            WHERE phone = ?
              AND active = 1
              AND role IN ('customer','courier')
        """, (phone,)).fetchone()

    if not user:

        raise HTTPException(
            401,
            "Неверный номер или PIN"
        )

    if not user["pin_hash"]:

        raise HTTPException(
            401,
            "PIN не установлен"
        )

    if not check_pin(
        data.pin,
        user["pin_hash"]
    ):

        raise HTTPException(
            401,
            "Неверный номер или PIN"
        )

    token = new_token()

    with db() as conn:

        conn.execute("""
            INSERT INTO sessions(
                token,
                user_id,
                role,
                created_at
            )
            VALUES(?,?,?,?)
        """, (
            token,
            user["id"],
            user["role"],
            time.time()
        ))

    return {
        "token": token,
        "role": user["role"]
    }


@app.post("/api/admin/web-login")
async def admin_web_login(
    data: AdminWebLogin
):

    try:

        tg_user = validate_init_data(
            data.init_data
        )

    except Exception as error:

        raise HTTPException(
            403,
            "Ошибка доступа: "
            "не вышло проверить админа: "
            + str(error)
        )

    try:
        telegram_id = int(
            tg_user["id"]
        )
    except Exception:

        raise HTTPException(
            403,
            "Неверный Telegram ID"
        )

    if telegram_id not in ADMIN_IDS:

        raise HTTPException(
            403,
            "Нет прав администратора"
        )

    name = (
        tg_user.get(
            "first_name",
            "Admin"
        )
    )

    username = tg_user.get(
        "username"
    )

    if username:
        name += " @" + username

    with db() as conn:

        existing = conn.execute("""
            SELECT id
            FROM users
            WHERE telegram_id = ?
              AND role = 'admin'
        """, (
            telegram_id,
        )).fetchone()

        if existing:

            user_id = existing["id"]

            conn.execute("""
                UPDATE users
                SET
                    name = ?,
                    active = 1
                WHERE id = ?
            """, (
                name,
                user_id
            ))

        else:

            conn.execute("""
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
            """, (
                name,
                None,
                None,
                None,
                "admin",
                telegram_id,
                1,
                time.time()
            ))

            user_id = conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

    token = new_token()

    with db() as conn:

        conn.execute("""
            INSERT INTO sessions(
                token,
                user_id,
                role,
                created_at
            )
            VALUES(?,?,?,?)
        """, (
            token,
            user_id,
            "admin",
            time.time()
        ))

    return {
        "token": token,
        "role": "admin"
    }


@app.get("/api/me")
async def me(
    authorization: str = Header(None)
):

    user = auth_user(
        authorization
    )

    return {
        "id": user["user_id"],
        "name": user["name"],
        "phone": user["phone"],
        "role": user["role"],
        "active": user["active"]
    }


# ============================================================
# ADMIN STATS
# ============================================================

@app.get("/api/admin/stats")
async def admin_stats(
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    with db() as conn:

        customers = conn.execute("""
            SELECT COUNT(*) AS count
            FROM users
            WHERE role='customer'
              AND active=1
        """).fetchone()["count"]

        couriers = conn.execute("""
            SELECT COUNT(*) AS count
            FROM users
            WHERE role='courier'
              AND active=1
        """).fetchone()["count"]

        orders = conn.execute("""
            SELECT COUNT(*) AS count
            FROM orders
            WHERE status != 'closed'
        """).fetchone()["count"]

        delivered = conn.execute("""
            SELECT COUNT(*) AS count
            FROM orders
            WHERE status='delivered'
        """).fetchone()["count"]

        closed = conn.execute("""
            SELECT COUNT(*) AS count
            FROM orders
            WHERE status='closed'
        """).fetchone()["count"]

        revenue = conn.execute("""
            SELECT COALESCE(
                SUM(price),
                0
            ) AS total
            FROM orders
            WHERE status IN (
                'delivered',
                'closed'
            )
        """).fetchone()["total"]

    return {
        "customers": customers,
        "couriers": couriers,
        "active_orders": orders,
        "delivered": delivered,
        "closed": closed,
        "revenue": revenue
    }


# ============================================================
# CUSTOMERS
# ============================================================

@app.get("/api/admin/customers")
async def admin_customers(
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    with db() as conn:

        rows = conn.execute("""
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
        """).fetchall()

    return [
        dict(row)
        for row in rows
    ]


@app.post("/api/admin/customers")
async def admin_create_customer(
    data: CustomerCreate,
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    name = data.name.strip()
    phone = norm_phone(
        data.phone
    )
    pin = data.pin.strip()

    if not name:
        raise HTTPException(
            400,
            "Введите имя клиента"
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
            "PIN должен содержать 4–8 цифр"
        )

    with db() as conn:

        exists = conn.execute("""
            SELECT id
            FROM users
            WHERE phone = ?
        """, (
            phone,
        )).fetchone()

        if exists:

            raise HTTPException(
                409,
                "Номер уже зарегистрирован"
            )

        conn.execute("""
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
        """, (
            name,
            phone,
            hash_pin(pin),
            pin,
            "customer",
            1,
            time.time()
        ))

        user_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

    return {
        "ok": True,
        "id": user_id,
        "name": name,
        "phone": phone,
        "pin": pin
    }


@app.delete(
    "/api/admin/customers/{customer_id}"
)
async def delete_customer(
    customer_id: int,
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    with db() as conn:

        customer = conn.execute("""
            SELECT id
            FROM users
            WHERE id=?
              AND role='customer'
        """, (
            customer_id,
        )).fetchone()

        if not customer:

            raise HTTPException(
                404,
                "Клиент не найден"
            )

        conn.execute("""
            UPDATE users
            SET active=0
            WHERE id=?
        """, (
            customer_id,
        ))

        conn.execute("""
            DELETE FROM sessions
            WHERE user_id=?
        """, (
            customer_id,
        ))

    return {
        "ok": True
    }


# ============================================================
# COURIERS
# ============================================================

@app.get("/api/admin/couriers")
async def admin_couriers(
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    with db() as conn:

        rows = conn.execute("""
            SELECT
                c.id,
                c.approved,
                c.online,
                c.lat,
                c.lon,
                c.updated_at,
                c.active,
                u.name,
                u.phone,
                u.pin_plain
            FROM couriers c
            JOIN users u
                ON u.id=c.user_id
            ORDER BY c.id DESC
        """).fetchall()

    return [
        dict(row)
        for row in rows
    ]


@app.post("/api/admin/couriers")
async def admin_create_courier(
    data: CourierCreate,
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    name = data.name.strip()
    phone = norm_phone(
        data.phone
    )

    pin = data.pin.strip()

    if not pin:
        pin = str(
            secrets.randbelow(900000)
            + 100000
        )

    if not re.fullmatch(
        r"\d{4,8}",
        pin
    ):
        raise HTTPException(
            400,
            "PIN должен содержать 4–8 цифр"
        )

    with db() as conn:

        exists = conn.execute("""
            SELECT id
            FROM users
            WHERE phone=?
        """, (
            phone,
        )).fetchone()

        if exists:

            raise HTTPException(
                409,
                "Номер уже зарегистрирован"
            )

        conn.execute("""
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
        """, (
            name,
            phone,
            hash_pin(pin),
            pin,
            "courier",
            1,
            time.time()
        ))

        user_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        conn.execute("""
            INSERT INTO couriers(
                user_id,
                approved,
                online,
                active
            )
            VALUES(?,?,?,?)
        """, (
            user_id,
            0,
            0,
            1
        ))

        courier_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

    return {
        "ok": True,
        "id": courier_id,
        "name": name,
        "phone": phone,
        "pin": pin
    }


@app.post(
    "/api/admin/couriers/{courier_id}/approve"
)
async def approve_courier(
    courier_id: int,
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    with db() as conn:

        courier = conn.execute("""
            SELECT user_id
            FROM couriers
            WHERE id=?
        """, (
            courier_id,
        )).fetchone()

        if not courier:

            raise HTTPException(
                404,
                "Курьер не найден"
            )

        conn.execute("""
            UPDATE couriers
            SET
                approved=1,
                active=1
            WHERE id=?
        """, (
            courier_id,
        ))

        conn.execute("""
            UPDATE users
            SET active=1
            WHERE id=?
        """, (
            courier["user_id"],
        ))

    return {
        "ok": True
    }


@app.post(
    "/api/admin/couriers/{courier_id}/fire"
)
async def fire_courier(
    courier_id: int,
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    with db() as conn:

        courier = conn.execute("""
            SELECT user_id
            FROM couriers
            WHERE id=?
        """, (
            courier_id,
        )).fetchone()

        if not courier:

            raise HTTPException(
                404,
                "Курьер не найден"
            )

        user_id = courier["user_id"]

        conn.execute("""
            UPDATE couriers
            SET
                active=0,
                online=0
            WHERE id=?
        """, (
            courier_id,
        ))

        conn.execute("""
            UPDATE users
            SET active=0
            WHERE id=?
        """, (
            user_id,
        ))

        conn.execute("""
            DELETE FROM sessions
            WHERE user_id=?
        """, (
            user_id,
        ))

        conn.execute("""
            UPDATE orders
            SET
                courier_id=NULL,
                status='new'
            WHERE courier_id=?
              AND status NOT IN (
                  'closed',
                  'delivered'
              )
        """, (
            courier_id,
        ))

    return {
        "ok": True
    }


# ============================================================
# ORDERS
# ============================================================

@app.get("/api/admin/orders")
async def admin_orders(
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    with db() as conn:

        rows = conn.execute("""
            SELECT
                o.*,
                cu.name AS customer_name,
                cu.phone AS customer_phone,
                cr.name AS courier_name,
                co.lat AS courier_lat,
                co.lon AS courier_lon
            FROM orders o
            JOIN users cu
                ON cu.id=o.customer_id
            LEFT JOIN couriers co
                ON co.id=o.courier_id
            LEFT JOIN users cr
                ON cr.id=co.user_id
            WHERE
                o.status!='closed'
                OR o.closed_at IS NULL
                OR o.closed_at>?
            ORDER BY o.id DESC
        """, (
            time.time() - 300,
        )).fetchall()

    return [
        dict(row)
        for row in rows
    ]


@app.post("/api/admin/orders")
async def admin_create_order(
    data: OrderCreate,
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    phone = norm_phone(
        data.phone
    )

    with db() as conn:

        customer = conn.execute("""
            SELECT id
            FROM users
            WHERE phone=?
              AND role='customer'
              AND active=1
        """, (
            phone,
        )).fetchone()

        if not customer:

            raise HTTPException(
                404,
                "Активный клиент не найден"
            )

        conn.execute("""
            INSERT INTO orders(
                customer_id,
                title,
                address,
                price,
                status,
                created_at
            )
            VALUES(?,?,?,?,?,?)
        """, (
            customer["id"],
            data.title.strip(),
            data.address.strip(),
            max(0, data.price),
            "new",
            time.time()
        ))

        order_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

    return {
        "ok": True,
        "id": order_id
    }


@app.post(
    "/api/admin/orders/{order_id}/assign"
)
async def assign_order(
    order_id: int,
    data: AssignIn,
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    with db() as conn:

        courier = conn.execute("""
            SELECT id
            FROM couriers
            WHERE id=?
              AND active=1
              AND approved=1
        """, (
            data.courier_id,
        )).fetchone()

        if not courier:

            raise HTTPException(
                404,
                "Курьер не найден или не одобрен"
            )

        order = conn.execute("""
            SELECT id
            FROM orders
            WHERE id=?
        """, (
            order_id,
        )).fetchone()

        if not order:

            raise HTTPException(
                404,
                "Заказ не найден"
            )

        conn.execute("""
            UPDATE orders
            SET
                courier_id=?,
                status='assigned'
            WHERE id=?
        """, (
            data.courier_id,
            order_id
        ))

    return {
        "ok": True
    }


@app.post(
    "/api/admin/orders/{order_id}/close"
)
async def close_order(
    order_id: int,
    authorization: str = Header(None)
):

    admin_only(
        auth_user(authorization)
    )

    with db() as conn:

        order = conn.execute("""
            SELECT id
            FROM orders
            WHERE id=?
        """, (
            order_id,
        )).fetchone()

        if not order:

            raise HTTPException(
                404,
                "Заказ не найден"
            )

        conn.execute("""
            UPDATE orders
            SET
                status='closed',
                closed_at=?
            WHERE id=?
        """, (
            time.time(),
            order_id
        ))

    return {
        "ok": True
    }


# ============================================================
# CUSTOMER
# ============================================================

@app.get("/api/customer/orders")
async def customer_orders(
    authorization: str = Header(None)
):

    user = auth_user(
        authorization
    )

    if user["role"] != "customer":

        raise HTTPException(
            403,
            "Только клиент"
        )

    with db() as conn:

        rows = conn.execute("""
            SELECT
                o.*,
                co.lat,
                co.lon
            FROM orders o
            LEFT JOIN couriers co
                ON co.id=o.courier_id
            WHERE
                o.customer_id=?
                AND (
                    o.status!='closed'
                    OR o.closed_at IS NULL
                    OR o.closed_at>?
                )
            ORDER BY o.id DESC
        """, (
            user["user_id"],
            time.time() - 300
        )).fetchall()

    return [
        dict(row)
        for row in rows
    ]


@app.post(
    "/api/customer/orders/{order_id}/confirm"
)
async def customer_confirm(
    order_id: int,
    authorization: str = Header(None)
):

    user = auth_user(
        authorization
    )

    if user["role"] != "customer":

        raise HTTPException(
            403,
            "Только клиент"
        )

    with db() as conn:

        order = conn.execute("""
            SELECT
                id,
                status
            FROM orders
            WHERE id=?
              AND customer_id=?
        """, (
            order_id,
            user["user_id"]
        )).fetchone()

        if not order:

            raise HTTPException(
                404,
                "Заказ не найден"
            )

        if order["status"] != "delivered":

            raise HTTPException(
                400,
                "Заказ ещё не доставлен"
            )

        conn.execute("""
            UPDATE orders
            SET
                customer_confirmed=1,
                status='closed',
                closed_at=?
            WHERE id=?
        """, (
            time.time(),
            order_id
        ))

    return {
        "ok": True
    }


# ============================================================
# COURIER
# ============================================================

def get_courier(user_id):

    with db() as conn:

        return conn.execute("""
            SELECT *
            FROM couriers
            WHERE user_id=?
              AND active=1
        """, (
            user_id,
        )).fetchone()


@app.get("/api/courier/orders")
async def courier_orders(
    authorization: str = Header(None)
):

    user = auth_user(
        authorization
    )

    if user["role"] != "courier":

        raise HTTPException(
            403,
            "Только курьер"
        )

    courier = get_courier(
        user["user_id"]
    )

    if not courier:

        raise HTTPException(
            403,
            "Курьер неактивен"
        )

    with db() as conn:

        rows = conn.execute("""
            SELECT
                o.*,
                cu.name AS customer_name,
                cu.phone AS customer_phone
            FROM orders o
            JOIN users cu
                ON cu.id=o.customer_id
            WHERE o.courier_id=?
              AND o.status!='closed'
            ORDER BY o.id DESC
        """, (
            courier["id"],
        )).fetchall()

    return {
        "online": bool(
            courier["online"]
        ),
        "orders": [
            dict(row)
            for row in rows
        ]
    }


@app.post("/api/courier/online")
async def courier_online(
    data: OnlineIn,
    authorization: str = Header(None)
):

    user = auth_user(
        authorization
    )

    if user["role"] != "courier":

        raise HTTPException(
            403,
            "Только курьер"
        )

    courier = get_courier(
        user["user_id"]
    )

    if not courier:

        raise HTTPException(
            403,
            "Курьер не найден"
        )

    if not courier["approved"]:

        raise HTTPException(
            403,
            "Курьер не одобрен"
        )

    with db() as conn:

        conn.execute("""
            UPDATE couriers
            SET online=?
            WHERE id=?
        """, (
            int(data.online),
            courier["id"]
        ))

    return {
        "ok": True,
        "online": data.online
    }


async def courier_action(
    order_id,
    user_id,
    old_status,
    new_status
):

    courier = get_courier(
        user_id
    )

    if not courier:

        raise HTTPException(
            403,
            "Курьер не найден"
        )

    with db() as conn:

        order = conn.execute("""
            SELECT
                id,
                status
            FROM orders
            WHERE id=?
              AND courier_id=?
        """, (
            order_id,
            courier["id"]
        )).fetchone()

        if not order:

            raise HTTPException(
                404,
                "Заказ не найден"
            )

        if order["status"] != old_status:

            raise HTTPException(
                400,
                "Неверный статус заказа"
            )

        conn.execute("""
            UPDATE orders
            SET status=?
            WHERE id=?
        """, (
            new_status,
            order_id
        ))

    return {
        "ok": True
    }


@app.post(
    "/api/courier/orders/{order_id}/accept"
)
async def courier_accept(
    order_id: int,
    authorization: str = Header(None)
):

    user = auth_user(
        authorization
    )

    return await courier_action(
        order_id,
        user["user_id"],
        "assigned",
        "accepted"
    )


@app.post(
    "/api/courier/orders/{order_id}/start"
)
async def courier_start(
    order_id: int,
    authorization: str = Header(None)
):

    user = auth_user(
        authorization
    )

    return await courier_action(
        order_id,
        user["user_id"],
        "accepted",
        "delivering"
    )


@app.post(
    "/api/courier/orders/{order_id}/complete"
)
async def courier_complete(
    order_id: int,
    authorization: str = Header(None)
):

    user = auth_user(
        authorization
    )

    return await courier_action(
        order_id,
        user["user_id"],
        "delivering",
        "delivered"
    )


@app.post("/api/courier/location")
async def courier_location(
    data: LocationIn,
    authorization: str = Header(None)
):

    user = auth_user(
        authorization
    )

    if user["role"] != "courier":

        raise HTTPException(
            403,
            "Только курьер"
        )

    courier = get_courier(
        user["user_id"]
    )

    if not courier:

        raise HTTPException(
            403,
            "Курьер не найден"
        )

    with db() as conn:

        conn.execute("""
            UPDATE couriers
            SET
                lat=?,
                lon=?,
                updated_at=?
            WHERE id=?
        """, (
            data.lat,
            data.lon,
            time.time(),
            courier["id"]
        ))

    return {
        "ok": True
    }


# ============================================================
# SERVER
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
