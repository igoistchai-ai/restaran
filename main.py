import os
import re
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
import asyncio
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, unquote

from fastapi import FastAPI, HTTPException, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    8357023784,
    7003441441,
}

WEB_APP_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://restaran-xtny.onrender.com"
).rstrip("/")

DB_PATH = "restaran.db"

if not BOT_TOKEN:
    print("WARNING: BOT_TOKEN is not set")

# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(title="RESTARAN")

telegram_app = None
cleanup_task = None


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_column(conn, table, column, definition):
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }

    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            pin_hash TEXT NOT NULL,
            pin_plain TEXT,
            role TEXT NOT NULL,
            telegram_id INTEGER,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
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
            updated_at TEXT,
            active INTEGER DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(id)
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
            created_at TEXT NOT NULL,
            customer_confirmed INTEGER DEFAULT 0,
            closed_at TEXT,
            FOREIGN KEY(customer_id) REFERENCES users(id),
            FOREIGN KEY(courier_id) REFERENCES couriers(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Migrations for old database
    ensure_column(conn, "users", "pin_plain", "TEXT")
    ensure_column(conn, "users", "telegram_id", "INTEGER")
    ensure_column(conn, "users", "active", "INTEGER DEFAULT 1")

    ensure_column(conn, "couriers", "approved", "INTEGER DEFAULT 0")
    ensure_column(conn, "couriers", "online", "INTEGER DEFAULT 0")
    ensure_column(conn, "couriers", "lat", "REAL")
    ensure_column(conn, "couriers", "lon", "REAL")
    ensure_column(conn, "couriers", "updated_at", "TEXT")
    ensure_column(conn, "couriers", "active", "INTEGER DEFAULT 1")

    ensure_column(conn, "orders", "customer_confirmed", "INTEGER DEFAULT 0")
    ensure_column(conn, "orders", "closed_at", "TEXT")

    conn.execute("""
        UPDATE users
        SET active = 1
        WHERE active IS NULL
    """)

    conn.execute("""
        UPDATE couriers
        SET active = 1
        WHERE active IS NULL
    """)

    conn.commit()
    conn.close()


# =========================================================
# HELPERS
# =========================================================

def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def normalize_phone(phone: str):
    phone = str(phone or "").strip()
    digits = re.sub(r"\D", "", phone)

    if not digits:
        return ""

    return "+" + digits


def phone_digits(phone: str):
    return re.sub(r"\D", "", str(phone or ""))


def phones_equal(a: str, b: str):
    a = phone_digits(a)
    b = phone_digits(b)

    if not a or not b:
        return False

    return (
        a == b
        or a.lstrip("0") == b.lstrip("0")
        or a.endswith(b)
        or b.endswith(a)
    )


def hash_pin(pin: str):
    salt = secrets.token_hex(16)
    digest = hashlib.sha256(
        (salt + pin).encode("utf-8")
    ).hexdigest()

    return f"{salt}${digest}"


def check_pin(pin: str, stored: str):
    try:
        salt, digest = stored.split("$", 1)
        actual = hashlib.sha256(
            (salt + pin).encode("utf-8")
        ).hexdigest()

        return hmac.compare_digest(actual, digest)
    except Exception:
        return False


def create_session(user_id: int, role: str):
    token = secrets.token_urlsafe(48)

    conn = db()
    conn.execute(
        "INSERT INTO sessions(token,user_id,role,created_at) VALUES(?,?,?,?)",
        (token, user_id, role, now_str()),
    )
    conn.commit()
    conn.close()

    return token


def get_session(authorization):
    if not authorization:
        raise HTTPException(401, "Необходима авторизация")

    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    else:
        token = authorization.strip()

    if not token:
        raise HTTPException(401, "Сессия не найдена")

    conn = db()
    row = conn.execute("""
        SELECT
            sessions.*,
            users.name,
            users.phone,
            users.telegram_id,
            users.active
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?
    """, (token,)).fetchone()
    conn.close()

    if not row:
        raise HTTPException(401, "Сессия истекла")

    if not row["active"]:
        raise HTTPException(403, "Аккаунт деактивирован")

    return row


def require_admin(authorization):
    session = get_session(authorization)

    telegram_id = session["telegram_id"]

    if telegram_id not in ADMIN_IDS:
        raise HTTPException(403, "Нет доступа администратора")

    return session


def cleanup_old_orders():
    cutoff = datetime.utcnow() - timedelta(minutes=5)

    conn = db()

    conn.execute("""
        DELETE FROM orders
        WHERE status = 'closed'
        AND closed_at IS NOT NULL
        AND closed_at <= ?
    """, (cutoff.strftime("%Y-%m-%d %H:%M:%S"),))

    conn.commit()
    conn.close()


async def cleanup_loop():
    while True:
        try:
            cleanup_old_orders()
        except Exception as e:
            print("Cleanup error:", e)

        await asyncio.sleep(30)


# =========================================================
# TELEGRAM WEB APP AUTH
# =========================================================

def validate_telegram_init_data(init_data: str):
    if not init_data or not BOT_TOKEN:
        return None

    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))

        received_hash = data.pop("hash", None)

        if not received_hash:
            return None

        pairs = [
            f"{key}={value}"
            for key, value in sorted(data.items())
        ]

        data_check_string = "\n".join(pairs)

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return None

        user_data = json.loads(
            unquote(data.get("user", "{}"))
        )

        return user_data

    except Exception as e:
        print("Telegram auth error:", e)
        return None


# =========================================================
# MODELS
# =========================================================

class LoginRequest(BaseModel):
    phone: str
    pin: str


class AdminWebLogin(BaseModel):
    init_data: str


class CustomerCreate(BaseModel):
    name: str
    phone: str


class CourierCreate(BaseModel):
    name: str
    phone: str


class OrderCreate(BaseModel):
    phone: str
    title: str
    address: str
    price: float = 0


class AssignOrder(BaseModel):
    courier_id: int


class LocationUpdate(BaseModel):
    lat: float
    lon: float


class OnlineRequest(BaseModel):
    online: bool


# =========================================================
# MAIN PAGE
# =========================================================

@app.get("/")
async def index():
    return FileResponse("index.html")


@app.head("/")
async def head_index():
    return {}


# =========================================================
# LOGIN
# =========================================================

@app.post("/api/login")
async def login(data: LoginRequest):
    phone = normalize_phone(data.phone)

    conn = db()

    users = conn.execute("""
        SELECT *
        FROM users
        WHERE role IN ('customer','courier')
        AND active = 1
    """).fetchall()

    user = None

    for row in users:
        if phones_equal(row["phone"], phone):
            user = row
            break

    if not user:
        conn.close()
        raise HTTPException(401, "Пользователь с таким номером не найден")

    if not check_pin(data.pin, user["pin_hash"]):
        conn.close()
        raise HTTPException(401, "Неверный PIN")

    if user["role"] == "courier":
        courier = conn.execute("""
            SELECT *
            FROM couriers
            WHERE user_id = ?
        """, (user["id"],)).fetchone()

        if not courier or not courier["approved"] or not courier["active"]:
            conn.close()
            raise HTTPException(
                403,
                "Курьер ещё не одобрен или деактивирован"
            )

    token = create_session(user["id"], user["role"])

    conn.close()

    return {
        "ok": True,
        "token": token,
        "role": user["role"],
        "name": user["name"],
    }


@app.post("/api/admin/web-login")
async def admin_web_login(data: AdminWebLogin):
    user = validate_telegram_init_data(data.init_data)

    if not user:
        raise HTTPException(
            401,
            "Telegram WebApp авторизация не прошла"
        )

    telegram_id = int(user.get("id", 0))

    if telegram_id not in ADMIN_IDS:
        raise HTTPException(
            403,
            "Вы не являетесь администратором"
        )

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_id = ?
        AND role = 'admin'
        AND active = 1
    """, (telegram_id,)).fetchone()

    if not row:
        name = (
            user.get("first_name", "")
            + " "
            + user.get("last_name", "")
        ).strip()

        if not name:
            name = "Администратор"

        conn.execute("""
            INSERT INTO users
            (
                name,
                phone,
                pin_hash,
                pin_plain,
                role,
                telegram_id,
                active,
                created_at
            )
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            name,
            f"tg:{telegram_id}",
            hash_pin(secrets.token_hex(8)),
            None,
            "admin",
            telegram_id,
            1,
            now_str(),
        ))

        conn.commit()

        row = conn.execute("""
            SELECT *
            FROM users
            WHERE telegram_id = ?
            AND role = 'admin'
        """, (telegram_id,)).fetchone()

    token = create_session(
        row["id"],
        "admin"
    )

    conn.close()

    return {
        "ok": True,
        "token": token,
        "role": "admin",
        "name": row["name"],
    }


@app.get("/api/me")
async def me(
    authorization: str | None = Header(default=None)
):
    session = get_session(authorization)

    return {
        "id": session["user_id"],
        "name": session["name"],
        "phone": session["phone"],
        "telegram_id": session["telegram_id"],
        "role": session["role"],
    }


# =========================================================
# CUSTOMER
# =========================================================

@app.get("/api/customer/orders")
async def customer_orders(
    authorization: str | None = Header(default=None)
):
    session = get_session(authorization)

    if session["role"] != "customer":
        raise HTTPException(403, "Недоступно")

    cleanup_old_orders()

    conn = db()

    rows = conn.execute("""
        SELECT
            orders.*,
            users.name AS customer_name,
            users.phone AS customer_phone,
            couriers.id AS courier_id,
            courier_users.name AS courier_name,
            couriers.lat,
            couriers.lon
        FROM orders
        JOIN users
            ON users.id = orders.customer_id
        LEFT JOIN couriers
            ON couriers.id = orders.courier_id
        LEFT JOIN users AS courier_users
            ON courier_users.id = couriers.user_id
        WHERE orders.customer_id = ?
        ORDER BY orders.id DESC
    """, (session["user_id"],)).fetchall()

    conn.close()

    return [dict(row) for row in rows]


@app.post("/api/customer/orders/{order_id}/confirm")
async def customer_confirm(
    order_id: int,
    authorization: str | None = Header(default=None)
):
    session = get_session(authorization)

    if session["role"] != "customer":
        raise HTTPException(403, "Недоступно")

    conn = db()

    order = conn.execute("""
        SELECT *
        FROM orders
        WHERE id = ?
        AND customer_id = ?
    """, (
        order_id,
        session["user_id"],
    )).fetchone()

    if not order:
        conn.close()
        raise HTTPException(404, "Заказ не найден")

    if order["status"] != "delivered":
        conn.close()
        raise HTTPException(
            400,
            "Заказ ещё не отмечен как доставленный"
        )

    conn.execute("""
        UPDATE orders
        SET
            customer_confirmed = 1,
            status = 'closed',
            closed_at = ?
        WHERE id = ?
    """, (
        now_str(),
        order_id,
    ))

    conn.commit()
    conn.close()

    return {"ok": True}


# =========================================================
# COURIER
# =========================================================

@app.get("/api/courier/orders")
async def courier_orders(
    authorization: str | None = Header(default=None)
):
    session = get_session(authorization)

    if session["role"] != "courier":
        raise HTTPException(403, "Недоступно")

    cleanup_old_orders()

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE user_id = ?
        AND active = 1
    """, (session["user_id"],)).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(403, "Курьер не найден")

    rows = conn.execute("""
        SELECT
            orders.*,
            users.name AS customer_name,
            users.phone AS customer_phone
        FROM orders
        JOIN users
            ON users.id = orders.customer_id
        WHERE orders.courier_id = ?
        ORDER BY orders.id DESC
    """, (courier["id"],)).fetchall()

    conn.close()

    return {
        "online": bool(courier["online"]),
        "orders": [dict(row) for row in rows],
    }


@app.post("/api/courier/online")
async def courier_online(
    data: OnlineRequest,
    authorization: str | None = Header(default=None)
):
    session = get_session(authorization)

    if session["role"] != "courier":
        raise HTTPException(403, "Недоступно")

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE user_id = ?
        AND active = 1
        AND approved = 1
    """, (session["user_id"],)).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(403, "Курьер не активен")

    conn.execute("""
        UPDATE couriers
        SET
            online = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        1 if data.online else 0,
        now_str(),
        courier["id"],
    ))

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/courier/location")
async def courier_location(
    data: LocationUpdate,
    authorization: str | None = Header(default=None)
):
    session = get_session(authorization)

    if session["role"] != "courier":
        raise HTTPException(403, "Недоступно")

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE user_id = ?
        AND active = 1
    """, (session["user_id"],)).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(404, "Курьер не найден")

    conn.execute("""
        UPDATE couriers
        SET
            lat = ?,
            lon = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        data.lat,
        data.lon,
        now_str(),
        courier["id"],
    ))

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/courier/orders/{order_id}/accept")
async def courier_accept(
    order_id: int,
    authorization: str | None = Header(default=None)
):
    session = get_session(authorization)

    if session["role"] != "courier":
        raise HTTPException(403, "Недоступно")

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE user_id = ?
        AND active = 1
        AND approved = 1
    """, (session["user_id"],)).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(403, "Курьер не найден")

    order = conn.execute("""
        SELECT *
        FROM orders
        WHERE id = ?
        AND courier_id = ?
    """, (
        order_id,
        courier["id"],
    )).fetchone()

    if not order:
        conn.close()
        raise HTTPException(404, "Заказ не найден")

    if order["status"] != "assigned":
        conn.close()
        raise HTTPException(
            400,
            "Заказ уже обработан"
        )

    conn.execute("""
        UPDATE orders
        SET status = 'accepted'
        WHERE id = ?
    """, (order_id,))

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/courier/orders/{order_id}/start")
async def courier_start(
    order_id: int,
    authorization: str | None = Header(default=None)
):
    session = get_session(authorization)

    if session["role"] != "courier":
        raise HTTPException(403, "Недоступно")

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE user_id = ?
        AND active = 1
    """, (session["user_id"],)).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(403, "Курьер не найден")

    result = conn.execute("""
        UPDATE orders
        SET status = 'delivering'
        WHERE id = ?
        AND courier_id = ?
        AND status = 'accepted'
    """, (
        order_id,
        courier["id"],
    ))

    if result.rowcount == 0:
        conn.close()
        raise HTTPException(
            400,
            "Нельзя начать этот заказ"
        )

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/courier/orders/{order_id}/complete")
async def courier_complete(
    order_id: int,
    authorization: str | None = Header(default=None)
):
    session = get_session(authorization)

    if session["role"] != "courier":
        raise HTTPException(403, "Недоступно")

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE user_id = ?
        AND active = 1
    """, (session["user_id"],)).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(403, "Курьер не найден")

    result = conn.execute("""
        UPDATE orders
        SET status = 'delivered'
        WHERE id = ?
        AND courier_id = ?
        AND status = 'delivering'
    """, (
        order_id,
        courier["id"],
    ))

    if result.rowcount == 0:
        conn.close()
        raise HTTPException(
            400,
            "Нельзя завершить этот заказ"
        )

    conn.commit()
    conn.close()

    return {"ok": True}


# =========================================================
# ADMIN - STATS
# =========================================================

@app.get("/api/admin/stats")
async def admin_stats(
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    cleanup_old_orders()

    conn = db()

    customers = conn.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE role = 'customer'
        AND active = 1
    """).fetchone()[0]

    couriers = conn.execute("""
        SELECT COUNT(*)
        FROM couriers
        WHERE active = 1
        AND approved = 1
    """).fetchone()[0]

    active_orders = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status NOT IN ('closed','delivered')
    """).fetchone()[0]

    delivered = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status = 'delivered'
    """).fetchone()[0]

    closed = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status = 'closed'
    """).fetchone()[0]

    revenue = conn.execute("""
        SELECT COALESCE(SUM(price),0)
        FROM orders
        WHERE status IN ('delivered','closed')
    """).fetchone()[0]

    conn.close()

    return {
        "customers": customers,
        "couriers": couriers,
        "active_orders": active_orders,
        "delivered": delivered,
        "closed": closed,
        "revenue": round(float(revenue or 0), 2),
    }


# =========================================================
# ADMIN - CUSTOMERS
# =========================================================

@app.get("/api/admin/customers")
async def admin_customers(
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    conn = db()

    rows = conn.execute("""
        SELECT
            id,
            name,
            phone,
            telegram_id,
            active,
            created_at
        FROM users
        WHERE role = 'customer'
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return [dict(row) for row in rows]


@app.post("/api/admin/customers")
async def admin_create_customer(
    data: CustomerCreate,
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    phone = normalize_phone(data.phone)

    if not phone:
        raise HTTPException(400, "Введите номер телефона")

    pin = str(secrets.randbelow(9000) + 1000)

    conn = db()

    existing = conn.execute("""
        SELECT *
        FROM users
        WHERE role = 'customer'
        AND phone = ?
    """, (phone,)).fetchone()

    if existing:
        conn.close()
        raise HTTPException(
            409,
            "Клиент с таким номером уже существует"
        )

    cursor = conn.execute("""
        INSERT INTO users
        (
            name,
            phone,
            pin_hash,
            pin_plain,
            role,
            active,
            created_at
        )
        VALUES (?,?,?,?,?,?,?)
    """, (
        data.name.strip() or "Клиент",
        phone,
        hash_pin(pin),
        pin,
        "customer",
        1,
        now_str(),
    ))

    conn.commit()
    customer_id = cursor.lastrowid
    conn.close()

    return {
        "ok": True,
        "id": customer_id,
        "name": data.name,
        "phone": phone,
        "pin": pin,
    }


# =========================================================
# ADMIN - COURIERS
# =========================================================

@app.get("/api/admin/couriers")
async def admin_couriers(
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    conn = db()

    rows = conn.execute("""
        SELECT
            couriers.id,
            couriers.user_id,
            couriers.approved,
            couriers.online,
            couriers.lat,
            couriers.lon,
            couriers.updated_at,
            couriers.active,
            users.name,
            users.phone
        FROM couriers
        JOIN users
            ON users.id = couriers.user_id
        ORDER BY couriers.id DESC
    """).fetchall()

    conn.close()

    return [dict(row) for row in rows]


@app.post("/api/admin/couriers")
async def admin_create_courier(
    data: CourierCreate,
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    phone = normalize_phone(data.phone)

    if not phone:
        raise HTTPException(400, "Введите номер телефона")

    pin = str(secrets.randbelow(9000) + 1000)

    conn = db()

    existing = conn.execute("""
        SELECT *
        FROM users
        WHERE phone = ?
        AND active = 1
    """, (phone,)).fetchone()

    if existing:
        conn.close()
        raise HTTPException(
            409,
            "Пользователь с таким номером уже существует"
        )

    cursor = conn.execute("""
        INSERT INTO users
        (
            name,
            phone,
            pin_hash,
            pin_plain,
            role,
            active,
            created_at
        )
        VALUES (?,?,?,?,?,?,?)
    """, (
        data.name.strip() or "Курьер",
        phone,
        hash_pin(pin),
        pin,
        "courier",
        1,
        now_str(),
    ))

    user_id = cursor.lastrowid

    cursor = conn.execute("""
        INSERT INTO couriers
        (
            user_id,
            approved,
            online,
            active
        )
        VALUES (?,?,?,?)
    """, (
        user_id,
        0,
        0,
        1,
    ))

    courier_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "courier_id": courier_id,
        "name": data.name,
        "phone": phone,
        "pin": pin,
        "approved": False,
    }


@app.post("/api/admin/couriers/{courier_id}/approve")
async def admin_approve_courier(
    courier_id: int,
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    conn = db()

    result = conn.execute("""
        UPDATE couriers
        SET
            approved = 1,
            active = 1
        WHERE id = ?
    """, (courier_id,))

    conn.execute("""
        UPDATE users
        SET active = 1
        WHERE id = (
            SELECT user_id
            FROM couriers
            WHERE id = ?
        )
    """, (courier_id,))

    conn.commit()
    conn.close()

    if result.rowcount == 0:
        raise HTTPException(404, "Курьер не найден")

    return {"ok": True}


@app.post("/api/admin/couriers/{courier_id}/fire")
async def admin_fire_courier(
    courier_id: int,
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE id = ?
    """, (courier_id,)).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(404, "Курьер не найден")

    # Деактивируем, но не удаляем историю.
    conn.execute("""
        UPDATE users
        SET active = 0
        WHERE id = ?
    """, (courier["user_id"],))

    conn.execute("""
        UPDATE couriers
        SET
            active = 0,
            online = 0,
            approved = 0
        WHERE id = ?
    """, (courier_id,))

    # Активные заказы возвращаются в общий пул.
    conn.execute("""
        UPDATE orders
        SET
            courier_id = NULL,
            status = 'new'
        WHERE courier_id = ?
        AND status IN ('assigned','accepted','delivering')
    """, (courier_id,))

    conn.commit()
    conn.close()

    return {"ok": True}


# =========================================================
# ADMIN - ORDERS
# =========================================================

@app.get("/api/admin/orders")
async def admin_orders(
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    cleanup_old_orders()

    conn = db()

    rows = conn.execute("""
        SELECT
            orders.id,
            orders.title,
            orders.address,
            orders.price,
            orders.status,
            orders.created_at,
            orders.customer_confirmed,
            orders.closed_at,

            customers.id AS customer_id,
            customers.name AS customer_name,
            customers.phone AS customer_phone,

            couriers.id AS courier_id,
            courier_users.name AS courier_name,
            courier_users.phone AS courier_phone,

            couriers.lat AS courier_lat,
            couriers.lon AS courier_lon

        FROM orders

        JOIN users AS customers
            ON customers.id = orders.customer_id

        LEFT JOIN couriers
            ON couriers.id = orders.courier_id

        LEFT JOIN users AS courier_users
            ON courier_users.id = couriers.user_id

        ORDER BY orders.id DESC
    """).fetchall()

    conn.close()

    return [dict(row) for row in rows]


@app.post("/api/admin/orders")
async def admin_create_order(
    data: OrderCreate,
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    phone = normalize_phone(data.phone)

    if not phone:
        raise HTTPException(
            400,
            "Введите номер телефона клиента"
        )

    if not data.title.strip():
        raise HTTPException(
            400,
            "Введите название заказа"
        )

    if not data.address.strip():
        raise HTTPException(
            400,
            "Введите адрес доставки"
        )

    conn = db()

    customers = conn.execute("""
        SELECT *
        FROM users
        WHERE role = 'customer'
        AND active = 1
    """).fetchall()

    customer = None

    for row in customers:
        if phones_equal(row["phone"], phone):
            customer = row
            break

    if not customer:
        conn.close()
        raise HTTPException(
            404,
            "Активный клиент с таким номером не найден"
        )

    cursor = conn.execute("""
        INSERT INTO orders
        (
            customer_id,
            courier_id,
            title,
            address,
            price,
            status,
            created_at,
            customer_confirmed,
            closed_at
        )
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        customer["id"],
        None,
        data.title.strip(),
        data.address.strip(),
        float(data.price or 0),
        "new",
        now_str(),
        0,
        None,
    ))

    order_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "order_id": order_id,
        "customer": customer["name"],
        "phone": customer["phone"],
    }


@app.post("/api/admin/orders/{order_id}/assign")
async def admin_assign_order(
    order_id: int,
    data: AssignOrder,
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE id = ?
        AND approved = 1
        AND active = 1
    """, (data.courier_id,)).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(
            404,
            "Активный одобренный курьер не найден"
        )

    result = conn.execute("""
        UPDATE orders
        SET
            courier_id = ?,
            status = 'assigned'
        WHERE id = ?
        AND status NOT IN ('closed','delivered')
    """, (
        data.courier_id,
        order_id,
    ))

    conn.commit()
    conn.close()

    if result.rowcount == 0:
        raise HTTPException(
            404,
            "Заказ не найден или уже закрыт"
        )

    return {"ok": True}


@app.post("/api/admin/orders/{order_id}/close")
async def admin_close_order(
    order_id: int,
    authorization: str | None = Header(default=None)
):
    require_admin(authorization)

    conn = db()

    result = conn.execute("""
        UPDATE orders
        SET
            status = 'closed',
            closed_at = ?
        WHERE id = ?
        AND status != 'closed'
    """, (
        now_str(),
        order_id,
    ))

    conn.commit()
    conn.close()

    if result.rowcount == 0:
        raise HTTPException(
            404,
            "Заказ уже закрыт или не найден"
        )

    return {
        "ok": True,
        "message": "Заказ закрыт. Через 5 минут он будет удалён из базы."
    }


# =========================================================
# TELEGRAM BOT
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    keyboard = [
        [
            KeyboardButton(
                "🍽 Открыть приложение",
                web_app=WebAppInfo(url=WEB_APP_URL),
            )
        ]
    ]

    await update.message.reply_text(
        "🍽 RESTARAN\n\n"
        "Добро пожаловать!\n"
        "Откройте приложение кнопкой ниже.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True,
        ),
    )


async def random_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    keyboard = [
        [
            KeyboardButton(
                "📱 Отправить номер",
                request_contact=True,
            )
        ]
    ]

    await update.message.reply_text(
        "📱 Отправьте ваш номер телефона.\n"
        "После проверки я выдам PIN для входа.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


async def contact_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    contact = update.message.contact

    if not contact:
        return

    phone = normalize_phone(contact.phone_number)
    telegram_id = update.effective_user.id

    conn = db()

    user = None

    customers = conn.execute("""
        SELECT *
        FROM users
        WHERE role = 'customer'
        AND active = 1
    """).fetchall()

    for row in customers:
        if phones_equal(row["phone"], phone):
            user = row
            break

    if not user:
        conn.close()

        await update.message.reply_text(
            "❌ Этот номер не найден среди клиентов."
        )
        return

    conn.execute("""
        UPDATE users
        SET telegram_id = ?
        WHERE id = ?
    """, (
        telegram_id,
        user["id"],
    ))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ Номер найден!\n\n"
        f"👤 {user['name']}\n"
        f"📱 {user['phone']}\n"
        f"🔐 Ваш PIN: {user['pin_plain']}\n\n"
        "Используйте номер телефона и этот PIN для входа."
    )


async def location_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    location = update.message.location

    if not location:
        return

    telegram_id = update.effective_user.id

    conn = db()

    courier = conn.execute("""
        SELECT couriers.*
        FROM couriers
        JOIN users
            ON users.id = couriers.user_id
        WHERE users.telegram_id = ?
        AND users.role = 'courier'
        AND users.active = 1
        AND couriers.active = 1
    """, (telegram_id,)).fetchone()

    if courier:
        conn.execute("""
            UPDATE couriers
            SET
                lat = ?,
                lon = ?,
                online = 1,
                updated_at = ?
            WHERE id = ?
        """, (
            location.latitude,
            location.longitude,
            now_str(),
            courier["id"],
        ))

        conn.commit()

    conn.close()

    await update.message.reply_text(
        "📍 Геопозиция обновлена."
    )


# =========================================================
# WEBSOCKET
# =========================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    try:
        while True:
            await websocket.send_json({
                "type": "ping",
                "time": int(time.time()),
            })

            await asyncio.sleep(15)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# =========================================================
# STARTUP / SHUTDOWN
# =========================================================

@app.on_event("startup")
async def startup():
    global telegram_app
    global cleanup_task

    init_db()

    cleanup_task = asyncio.create_task(
        cleanup_loop()
    )

    if BOT_TOKEN:
        telegram_app = (
            Application.builder()
            .token(BOT_TOKEN)
            .build()
        )

        telegram_app.add_handler(
            CommandHandler("start", start_command)
        )

        telegram_app.add_handler(
            CommandHandler("random", random_command)
        )

        telegram_app.add_handler(
            MessageHandler(
                filters.CONTACT,
                contact_handler
            )
        )

        telegram_app.add_handler(
            MessageHandler(
                filters.LOCATION,
                location_handler
            )
        )

        await telegram_app.initialize()
        await telegram_app.start()

        if telegram_app.updater:
            await telegram_app.updater.start_polling()

        print("Telegram bot started")

    print("RESTARAN started")


@app.on_event("shutdown")
async def shutdown():
    global telegram_app
    global cleanup_task

    if cleanup_task:
        cleanup_task.cancel()

    if telegram_app:
        try:
            if telegram_app.updater:
                await telegram_app.updater.stop()

            await telegram_app.stop()
            await telegram_app.shutdown()

        except Exception as e:
            print("Telegram shutdown error:", e)


# =========================================================
# LOCAL
# =========================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
