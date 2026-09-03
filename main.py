import os
import re
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
import asyncio
from urllib.parse import parse_qsl, unquote

from fastapi import FastAPI, HTTPException, Header, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
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
WEB_APP_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

ADMIN_IDS = {
    8357023784,
    7003441441,
}

DB_PATH = "restaran.db"

app = FastAPI()

# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT UNIQUE NOT NULL,
        pin_hash TEXT NOT NULL,
        pin_plain TEXT,
        role TEXT NOT NULL DEFAULT 'customer',
        telegram_id INTEGER,
        created_at INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS couriers(
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

    conn.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        courier_id INTEGER,
        title TEXT NOT NULL,
        address TEXT NOT NULL,
        price REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'new',
        created_at INTEGER NOT NULL,
        customer_confirmed INTEGER NOT NULL DEFAULT 0,
        closed_at INTEGER
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """)

    # migrations
    migrations = [
        ("users", "pin_plain", "TEXT"),
        ("users", "telegram_id", "INTEGER"),
        ("users", "active", "INTEGER NOT NULL DEFAULT 1"),
        ("couriers", "approved", "INTEGER NOT NULL DEFAULT 0"),
        ("couriers", "online", "INTEGER NOT NULL DEFAULT 0"),
        ("couriers", "lat", "REAL"),
        ("couriers", "lon", "REAL"),
        ("couriers", "updated_at", "INTEGER"),
        ("couriers", "active", "INTEGER NOT NULL DEFAULT 1"),
        ("orders", "customer_confirmed", "INTEGER NOT NULL DEFAULT 0"),
        ("orders", "closed_at", "INTEGER"),
    ]

    for table, column, definition in migrations:
        cols = [
            r["name"]
            for r in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        ]

        if column not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    conn.commit()
    conn.close()


init_db()

# =========================================================
# HELPERS
# =========================================================

def normalize_phone(phone):
    return re.sub(r"\D", "", str(phone or ""))


def phone_digits(phone):
    return re.sub(r"\D", "", str(phone or ""))


def phones_equal(a, b):
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


def hash_pin(pin):
    return hashlib.sha256(str(pin).encode()).hexdigest()


def check_pin(pin, password_hash):
    return hmac.compare_digest(
        hash_pin(pin),
        password_hash
    )


def new_pin():
    return str(secrets.randbelow(900000) + 100000)


def create_session(user_id, role):
    token = secrets.token_urlsafe(48)

    conn = db()
    conn.execute(
        "INSERT INTO sessions(token,user_id,role,created_at) VALUES(?,?,?,?)",
        (token, user_id, role, int(time.time()))
    )
    conn.commit()
    conn.close()

    return token


def get_session(authorization):
    if not authorization:
        return None

    token = authorization.replace("Bearer ", "").strip()

    if not token:
        return None

    conn = db()

    row = conn.execute("""
        SELECT
            s.token,
            s.user_id,
            s.role,
            u.name,
            u.phone,
            u.telegram_id,
            u.active
        FROM sessions s
        JOIN users u ON u.id=s.user_id
        WHERE s.token=?
    """, (token,)).fetchone()

    conn.close()

    if not row or not row["active"]:
        return None

    return row


def require_user(authorization):
    user = get_session(authorization)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Требуется авторизация"
        )

    return user


def require_admin(authorization):
    user = require_user(authorization)

    if user["role"] != "admin":
        raise HTTPException(
            status_code=403,
            detail="Нет доступа"
        )

    return user


def cleanup_old_closed():
    cutoff = int(time.time()) - 300

    conn = db()

    conn.execute("""
        DELETE FROM orders
        WHERE status='closed'
        AND closed_at IS NOT NULL
        AND closed_at <= ?
    """, (cutoff,))

    conn.commit()
    conn.close()


def order_cutoff():
    return int(time.time()) - 300


# =========================================================
# TELEGRAM WEBAPP VALIDATION
# =========================================================

def validate_telegram_init_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))

        received_hash = pairs.pop("hash", None)

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(pairs.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return None

        auth_date = int(pairs.get("auth_date", "0"))

        if int(time.time()) - auth_date > 86400:
            return None

        user_raw = pairs.get("user")

        if not user_raw:
            return None

        user = json.loads(unquote(user_raw))

        return user

    except Exception as e:
        print("Telegram validation:", e)
        return None


# =========================================================
# MODELS
# =========================================================

class LoginData(BaseModel):
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


class AssignData(BaseModel):
    courier_id: int


class OnlineData(BaseModel):
    online: bool


class LocationData(BaseModel):
    lat: float
    lon: float


# =========================================================
# WEB
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
async def login(data: LoginData):
    phone = normalize_phone(data.phone)

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM users
        WHERE role IN ('customer','courier')
        AND active=1
    """).fetchall()

    user = None

    for row in rows:
        if phones_equal(row["phone"], phone):
            user = row
            break

    if not user or not check_pin(
        data.pin,
        user["pin_hash"]
    ):
        conn.close()
        raise HTTPException(
            status_code=401,
            detail="Неверный номер или PIN"
        )

    if user["role"] == "courier":
        courier = conn.execute(
            "SELECT * FROM couriers WHERE user_id=?",
            (user["id"],)
        ).fetchone()

        if not courier or not courier["approved"]:
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="Курьер ещё не одобрен администратором"
            )

        if not courier["active"]:
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="Курьер деактивирован"
            )

    token = create_session(
        user["id"],
        user["role"]
    )

    conn.close()

    return {
        "token": token,
        "role": user["role"]
    }


# =========================================================
# ADMIN AUTO LOGIN
# =========================================================

@app.post("/api/admin/web-login")
async def admin_web_login(data: AdminWebLogin):

    telegram_user = validate_telegram_init_data(
        data.init_data
    )

    if not telegram_user:
        raise HTTPException(
            status_code=401,
            detail="Недействительные данные Telegram"
        )

    telegram_id = int(
        telegram_user.get("id", 0)
    )

    # ТОЛЬКО ЭТИ ДВА ID АДМИНЫ
    if telegram_id not in ADMIN_IDS:
        raise HTTPException(
            status_code=403,
            detail="Вы не являетесь администратором"
        )

    conn = db()

    admin = conn.execute(
        "SELECT * FROM users WHERE telegram_id=? AND role='admin'",
        (telegram_id,)
    ).fetchone()

    if not admin:

        name = (
            telegram_user.get("first_name", "")
            + " "
            + telegram_user.get("last_name", "")
        ).strip()

        if not name:
            name = telegram_user.get(
                "username",
                "Администратор"
            )

        # Админ создаётся автоматически.
        # Телефон/PIN НЕ НУЖНЫ.
        conn.execute("""
            INSERT INTO users(
                name,
                phone,
                pin_hash,
                pin_plain,
                role,
                telegram_id,
                created_at,
                active
            )
            VALUES(?,?,?,?,?,?,?,1)
        """, (
            name,
            f"admin_{telegram_id}",
            hash_pin(secrets.token_urlsafe(32)),
            "",
            "admin",
            telegram_id,
            int(time.time())
        ))

        conn.commit()

        admin = conn.execute(
            "SELECT * FROM users WHERE telegram_id=? AND role='admin'",
            (telegram_id,)
        ).fetchone()

    else:
        conn.execute(
            "UPDATE users SET active=1 WHERE id=?",
            (admin["id"],)
        )
        conn.commit()

    token = create_session(
        admin["id"],
        "admin"
    )

    conn.close()

    return {
        "token": token,
        "role": "admin",
        "telegram_id": telegram_id
    }


# =========================================================
# ME
# =========================================================

@app.get("/api/me")
async def me(
    authorization: str = Header(default="")
):
    user = require_user(authorization)

    return {
        "id": user["user_id"],
        "name": user["name"],
        "phone": user["phone"],
        "telegram_id": user["telegram_id"],
        "role": user["role"]
    }


# =========================================================
# ADMIN STATS
# =========================================================

@app.get("/api/admin/stats")
async def admin_stats(
    authorization: str = Header(default="")
):
    require_admin(authorization)
    cleanup_old_closed()

    conn = db()

    customers = conn.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE role='customer'
        AND active=1
    """).fetchone()[0]

    couriers = conn.execute("""
        SELECT COUNT(*)
        FROM couriers c
        JOIN users u ON u.id=c.user_id
        WHERE c.active=1
        AND u.active=1
    """).fetchone()[0]

    active_orders = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status!='closed'
    """).fetchone()[0]

    delivered = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status='delivered'
    """).fetchone()[0]

    closed = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status='closed'
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
        "revenue": revenue
    }


# =========================================================
# CUSTOMERS
# =========================================================

@app.get("/api/admin/customers")
async def admin_customers(
    authorization: str = Header(default="")
):
    require_admin(authorization)

    conn = db()

    rows = conn.execute("""
        SELECT id,name,phone,active,created_at
        FROM users
        WHERE role='customer'
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/admin/customers")
async def admin_create_customer(
    data: CustomerCreate,
    authorization: str = Header(default="")
):
    require_admin(authorization)

    phone = normalize_phone(data.phone)

    if len(phone) < 5:
        raise HTTPException(
            status_code=400,
            detail="Неверный номер телефона"
        )

    pin = new_pin()

    conn = db()

    try:
        cur = conn.execute("""
            INSERT INTO users(
                name,
                phone,
                pin_hash,
                pin_plain,
                role,
                created_at,
                active
            )
            VALUES(?,?,?,?,?,?,1)
        """, (
            data.name.strip(),
            phone,
            hash_pin(pin),
            pin,
            "customer",
            int(time.time())
        ))

        conn.commit()

        return {
            "id": cur.lastrowid,
            "name": data.name.strip(),
            "phone": phone,
            "pin": pin
        }

    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=400,
            detail="Клиент с таким номером уже существует"
        )

    finally:
        conn.close()


# =========================================================
# COURIERS
# =========================================================

@app.get("/api/admin/couriers")
async def admin_couriers(
    authorization: str = Header(default="")
):
    require_admin(authorization)

    conn = db()

    rows = conn.execute("""
        SELECT
            c.id,
            c.user_id,
            c.approved,
            c.online,
            c.lat,
            c.lon,
            c.updated_at,
            c.active,
            u.name,
            u.phone
        FROM couriers c
        JOIN users u ON u.id=c.user_id
        ORDER BY c.id DESC
    """).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/admin/couriers")
async def admin_create_courier(
    data: CourierCreate,
    authorization: str = Header(default="")
):
    require_admin(authorization)

    phone = normalize_phone(data.phone)
    pin = new_pin()

    conn = db()

    try:

        cur = conn.execute("""
            INSERT INTO users(
                name,
                phone,
                pin_hash,
                pin_plain,
                role,
                created_at,
                active
            )
            VALUES(?,?,?,?,?,?,1)
        """, (
            data.name.strip(),
            phone,
            hash_pin(pin),
            pin,
            "courier",
            int(time.time())
        ))

        user_id = cur.lastrowid

        conn.execute("""
            INSERT INTO couriers(
                user_id,
                approved,
                online,
                active
            )
            VALUES(?,0,0,1)
        """, (user_id,))

        conn.commit()

        return {
            "id": user_id,
            "name": data.name.strip(),
            "phone": phone,
            "pin": pin
        }

    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=400,
            detail="Курьер с таким номером уже существует"
        )

    finally:
        conn.close()


@app.post("/api/admin/couriers/{courier_id}/approve")
async def approve_courier(
    courier_id: int,
    authorization: str = Header(default="")
):
    require_admin(authorization)

    conn = db()

    conn.execute("""
        UPDATE couriers
        SET approved=1, active=1
        WHERE id=?
    """, (courier_id,))

    conn.execute("""
        UPDATE users
        SET active=1
        WHERE id=(
            SELECT user_id
            FROM couriers
            WHERE id=?
        )
    """, (courier_id,))

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/admin/couriers/{courier_id}/fire")
async def fire_courier(
    courier_id: int,
    authorization: str = Header(default="")
):
    require_admin(authorization)

    conn = db()

    courier = conn.execute(
        "SELECT user_id FROM couriers WHERE id=?",
        (courier_id,)
    ).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail="Курьер не найден"
        )

    conn.execute("""
        UPDATE couriers
        SET active=0, approved=0, online=0
        WHERE id=?
    """, (courier_id,))

    conn.execute("""
        UPDATE users
        SET active=0
        WHERE id=?
    """, (courier["user_id"],))

    # Снимаем активные заказы
    conn.execute("""
        UPDATE orders
        SET courier_id=NULL,
            status='new'
        WHERE courier_id=?
        AND status IN ('assigned','accepted','delivering')
    """, (courier_id,))

    conn.commit()
    conn.close()

    return {"ok": True}


# =========================================================
# ORDERS
# =========================================================

@app.get("/api/admin/orders")
async def admin_orders(
    authorization: str = Header(default="")
):
    require_admin(authorization)
    cleanup_old_closed()

    cutoff = order_cutoff()

    conn = db()

    rows = conn.execute("""
        SELECT
            o.*,
            cu.name AS customer_name,
            cu.phone AS customer_phone,
            co.name AS courier_name,
            c.lat AS courier_lat,
            c.lon AS courier_lon
        FROM orders o
        JOIN users cu ON cu.id=o.customer_id
        LEFT JOIN couriers c ON c.id=o.courier_id
        LEFT JOIN users co ON co.id=c.user_id
        WHERE
            o.status!='closed'
            OR o.closed_at IS NULL
            OR o.closed_at>?
        ORDER BY o.id DESC
    """, (cutoff,)).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/admin/orders")
async def admin_create_order(
    data: OrderCreate,
    authorization: str = Header(default="")
):
    require_admin(authorization)

    phone = normalize_phone(data.phone)

    conn = db()

    customers = conn.execute("""
        SELECT id,name,phone,telegram_id
        FROM users
        WHERE role='customer'
        AND active=1
    """).fetchall()

    customer = None

    for row in customers:
        if phones_equal(row["phone"], phone):
            customer = row
            break

    if not customer:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail="Клиент с таким номером не найден"
        )

    cur = conn.execute("""
        INSERT INTO orders(
            customer_id,
            title,
            address,
            price,
            status,
            created_at
        )
        VALUES(?,?,?,?,'new',?)
    """, (
        customer["id"],
        data.title.strip(),
        data.address.strip(),
        float(data.price),
        int(time.time())
    ))

    conn.commit()

    order_id = cur.lastrowid

    conn.close()

    return {
        "ok": True,
        "id": order_id
    }


@app.post("/api/admin/orders/{order_id}/assign")
async def assign_order(
    order_id: int,
    data: AssignData,
    authorization: str = Header(default="")
):
    require_admin(authorization)

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE id=?
        AND approved=1
        AND active=1
    """, (data.courier_id,)).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail="Курьер не найден или не одобрен"
        )

    order = conn.execute(
        "SELECT * FROM orders WHERE id=?",
        (order_id,)
    ).fetchone()

    if not order:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail="Заказ не найден"
        )

    conn.execute("""
        UPDATE orders
        SET courier_id=?, status='assigned'
        WHERE id=?
        AND status!='closed'
    """, (
        data.courier_id,
        order_id
    ))

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/admin/orders/{order_id}/close")
async def close_order(
    order_id: int,
    authorization: str = Header(default="")
):
    require_admin(authorization)

    now = int(time.time())

    conn = db()

    cur = conn.execute("""
        UPDATE orders
        SET status='closed',
            closed_at=?
        WHERE id=?
    """, (now, order_id))

    conn.commit()
    conn.close()

    if cur.rowcount == 0:
        raise HTTPException(
            status_code=404,
            detail="Заказ не найден"
        )

    return {
        "ok": True,
        "closed_at": now,
        "delete_after": now + 300
    }


# =========================================================
# CUSTOMER ORDERS
# =========================================================

@app.get("/api/customer/orders")
async def customer_orders(
    authorization: str = Header(default="")
):
    user = require_user(authorization)

    if user["role"] != "customer":
        raise HTTPException(
            status_code=403,
            detail="Нет доступа"
        )

    cleanup_old_closed()

    cutoff = order_cutoff()

    conn = db()

    rows = conn.execute("""
        SELECT
            o.*,
            c.lat,
            c.lon,
            co.name AS courier_name
        FROM orders o
        LEFT JOIN couriers c ON c.id=o.courier_id
        LEFT JOIN users co ON co.id=c.user_id
        WHERE o.customer_id=?
        AND (
            o.status!='closed'
            OR o.closed_at IS NULL
            OR o.closed_at>?
        )
        ORDER BY o.id DESC
    """, (
        user["user_id"],
        cutoff
    )).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/customer/orders/{order_id}/confirm")
async def customer_confirm(
    order_id: int,
    authorization: str = Header(default="")
):
    user = require_user(authorization)

    if user["role"] != "customer":
        raise HTTPException(
            status_code=403,
            detail="Нет доступа"
        )

    conn = db()

    order = conn.execute("""
        SELECT *
        FROM orders
        WHERE id=?
        AND customer_id=?
    """, (
        order_id,
        user["user_id"]
    )).fetchone()

    if not order:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail="Заказ не найден"
        )

    if order["status"] != "delivered":
        conn.close()
        raise HTTPException(
            status_code=400,
            detail="Заказ ещё не доставлен"
        )

    now = int(time.time())

    conn.execute("""
        UPDATE orders
        SET customer_confirmed=1,
            status='closed',
            closed_at=?
        WHERE id=?
    """, (
        now,
        order_id
    ))

    conn.commit()
    conn.close()

    return {"ok": True}


# =========================================================
# COURIER
# =========================================================

@app.get("/api/courier/orders")
async def courier_orders(
    authorization: str = Header(default="")
):
    user = require_user(authorization)

    if user["role"] != "courier":
        raise HTTPException(
            status_code=403,
            detail="Нет доступа"
        )

    cleanup_old_closed()

    cutoff = order_cutoff()

    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE user_id=?
    """, (user["user_id"],)).fetchone()

    if not courier or not courier["active"]:
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="Курьер деактивирован"
        )

    rows = conn.execute("""
        SELECT
            o.*,
            u.name AS customer_name,
            u.phone AS customer_phone
        FROM orders o
        JOIN users u ON u.id=o.customer_id
        WHERE o.courier_id=?
        AND (
            o.status!='closed'
            OR o.closed_at IS NULL
            OR o.closed_at>?
        )
        ORDER BY o.id DESC
    """, (
        courier["id"],
        cutoff
    )).fetchall()

    conn.close()

    return {
        "online": bool(courier["online"]),
        "orders": [dict(x) for x in rows]
    }


@app.post("/api/courier/online")
async def courier_online(
    data: OnlineData,
    authorization: str = Header(default="")
):
    user = require_user(authorization)

    if user["role"] != "courier":
        raise HTTPException(
            status_code=403,
            detail="Нет доступа"
        )

    conn = db()

    courier = conn.execute(
        "SELECT * FROM couriers WHERE user_id=?",
        (user["user_id"],)
    ).fetchone()

    if not courier or not courier["active"]:
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="Курьер деактивирован"
        )

    conn.execute("""
        UPDATE couriers
        SET online=?
        WHERE id=?
    """, (
        1 if data.online else 0,
        courier["id"]
    ))

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/courier/orders/{order_id}/accept")
async def courier_accept(
    order_id: int,
    authorization: str = Header(default="")
):
    user = require_user(authorization)

    if user["role"] != "courier":
        raise HTTPException(
            status_code=403,
            detail="Нет доступа"
        )

    conn = db()

    courier = conn.execute(
        "SELECT * FROM couriers WHERE user_id=? AND active=1",
        (user["user_id"],)
    ).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="Курьер не активен"
        )

    cur = conn.execute("""
        UPDATE orders
        SET status='accepted'
        WHERE id=?
        AND courier_id=?
        AND status='assigned'
    """, (
        order_id,
        courier["id"]
    ))

    conn.commit()
    conn.close()

    if cur.rowcount == 0:
        raise HTTPException(
            status_code=400,
            detail="Заказ нельзя принять"
        )

    return {"ok": True}


@app.post("/api/courier/orders/{order_id}/start")
async def courier_start(
    order_id: int,
    authorization: str = Header(default="")
):
    user = require_user(authorization)

    if user["role"] != "courier":
        raise HTTPException(
            status_code=403,
            detail="Нет доступа"
        )

    conn = db()

    courier = conn.execute(
        "SELECT * FROM couriers WHERE user_id=? AND active=1",
        (user["user_id"],)
    ).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="Курьер не активен"
        )

    cur = conn.execute("""
        UPDATE orders
        SET status='delivering'
        WHERE id=?
        AND courier_id=?
        AND status='accepted'
    """, (
        order_id,
        courier["id"]
    ))

    conn.commit()
    conn.close()

    if cur.rowcount == 0:
        raise HTTPException(
            status_code=400,
            detail="Нельзя начать эту доставку"
        )

    return {"ok": True}


@app.post("/api/courier/orders/{order_id}/complete")
async def courier_complete(
    order_id: int,
    authorization: str = Header(default="")
):
    user = require_user(authorization)

    if user["role"] != "courier":
        raise HTTPException(
            status_code=403,
            detail="Нет доступа"
        )

    conn = db()

    courier = conn.execute(
        "SELECT * FROM couriers WHERE user_id=? AND active=1",
        (user["user_id"],)
    ).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="Курьер не активен"
        )

    cur = conn.execute("""
        UPDATE orders
        SET status='delivered'
        WHERE id=?
        AND courier_id=?
        AND status='delivering'
    """, (
        order_id,
        courier["id"]
    ))

    conn.commit()
    conn.close()

    if cur.rowcount == 0:
        raise HTTPException(
            status_code=400,
            detail="Нельзя завершить эту доставку"
        )

    return {"ok": True}


@app.post("/api/courier/location")
async def courier_location(
    data: LocationData,
    authorization: str = Header(default="")
):
    user = require_user(authorization)

    if user["role"] != "courier":
        raise HTTPException(
            status_code=403,
            detail="Нет доступа"
        )

    conn = db()

    courier = conn.execute(
        "SELECT * FROM couriers WHERE user_id=? AND active=1",
        (user["user_id"],)
    ).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="Курьер не активен"
        )

    conn.execute("""
        UPDATE couriers
        SET lat=?, lon=?, updated_at=?
        WHERE id=?
    """, (
        data.lat,
        data.lon,
        int(time.time()),
        courier["id"]
    ))

    conn.commit()
    conn.close()

    return {"ok": True}


# =========================================================
# TELEGRAM BOT
# =========================================================

telegram_app = None


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    keyboard = [
        [
            InlineKeyboardButton(
                "🍽 Открыть приложение",
                web_app=WebAppInfo(
                    url=WEB_APP_URL
                )
            )
        ]
    ]

    await update.message.reply_text(
        "🍽 RESTARAN\n\n"
        "Нажмите кнопку ниже, чтобы открыть приложение.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def random_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📱 Отправьте свой Telegram-контакт.\n\n"
        "Нажмите кнопку скрепки → Контакт."
    )


async def contact_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    contact = update.message.contact

    phone = normalize_phone(
        contact.phone_number
    )

    telegram_id = update.effective_user.id

    conn = db()

    conn.execute("""
        UPDATE users
        SET telegram_id=?
        WHERE phone=?
        AND role='customer'
    """, (
        telegram_id,
        phone
    ))

    row = conn.execute("""
        SELECT name,pin_plain
        FROM users
        WHERE phone=?
        AND role='customer'
        AND active=1
    """, (phone,)).fetchone()

    conn.commit()
    conn.close()

    if not row:
        await update.message.reply_text(
            "❌ Клиент с таким номером не найден."
        )
        return

    await update.message.reply_text(
        "✅ Вы найдены!\n\n"
        f"Имя: {row['name']}\n"
        f"PIN: {row['pin_plain']}\n\n"
        "Используйте эти данные для входа."
    )


async def location_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message.location:
        return

    telegram_id = update.effective_user.id

    lat = update.message.location.latitude
    lon = update.message.location.longitude

    conn = db()

    user = conn.execute("""
        SELECT id
        FROM users
        WHERE telegram_id=?
        AND role='courier'
        AND active=1
    """, (telegram_id,)).fetchone()

    if user:

        conn.execute("""
            UPDATE couriers
            SET lat=?,lon=?,updated_at=?
            WHERE user_id=?
        """, (
            lat,
            lon,
            int(time.time()),
            user["id"]
        ))

        conn.commit()

    conn.close()


# =========================================================
# CLEANUP
# =========================================================

async def cleanup_loop():

    while True:

        try:
            cleanup_old_closed()
        except Exception as e:
            print("Cleanup:", e)

        await asyncio.sleep(30)


@app.on_event("startup")
async def startup():

    global telegram_app

    if not BOT_TOKEN:
        print("WARNING: BOT_TOKEN is missing")
        return

    telegram_app = (
        __import__("telegram.ext", fromlist=["Application"])
        .Application.builder()
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
    await telegram_app.updater.start_polling()

    asyncio.create_task(
        cleanup_loop()
    )

    print("RESTARAN started")


@app.on_event("shutdown")
async def shutdown():

    global telegram_app

    if telegram_app:

        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception as e:
            print("Telegram shutdown:", e)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
)
