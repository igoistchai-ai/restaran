import os
import re
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Render автоматически даст этот URL
WEB_APP_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()

DB_PATH = "restaran.db"

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Add BOT_TOKEN in Render → Environment Variables."
    )

if not WEB_APP_URL:
    print("WARNING: RENDER_EXTERNAL_URL is missing.")

# =========================================================
# APP
# =========================================================

app = FastAPI(title="RESTARAN")

bot_app: Optional[Application] = None

# user_id -> websocket connections
connections = {}


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS couriers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            telegram_id INTEGER UNIQUE,
            name TEXT,
            phone TEXT,
            photo_file_id TEXT,
            latitude REAL,
            longitude REAL,
            location_updated_at TEXT,
            verified INTEGER DEFAULT 0,
            online INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            courier_id INTEGER,
            address TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL DEFAULT 0,
            status TEXT DEFAULT 'new',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


init_db()


# =========================================================
# HELPERS
# =========================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return ""
    return "+" + digits


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode(),
        salt,
        120_000
    )
    return salt.hex() + ":" + digest.hex()


def verify_pin(pin: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)

        new_digest = hashlib.pbkdf2_hmac(
            "sha256",
            pin.encode(),
            salt,
            120_000
        )

        return hmac.compare_digest(
            new_digest.hex(),
            digest_hex
        )
    except Exception:
        return False


def create_session(user_id: int):
    token = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(days=30)

    conn = db()
    conn.execute(
        "INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
        (token, user_id, expires.isoformat())
    )
    conn.commit()
    conn.close()

    return token


def get_user_from_token(token: str):
    if not token:
        return None

    conn = db()

    row = conn.execute("""
        SELECT users.*
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?
    """, (token,)).fetchone()

    conn.close()

    if not row:
        return None

    return row


def auth_user(authorization: Optional[str]):
    if not authorization:
        raise HTTPException(401, "Authorization required")

    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization")

    token = authorization.replace("Bearer ", "", 1).strip()

    user = get_user_from_token(token)

    if not user:
        raise HTTPException(401, "Session expired")

    return user


def admin_required(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")


# =========================================================
# TELEGRAM WEBAPP AUTH
# =========================================================

def validate_telegram_init_data(init_data: str):
    if not init_data:
        return None

    try:
        from urllib.parse import parse_qsl

        data = dict(parse_qsl(init_data, keep_blank_values=True))

        received_hash = data.pop("hash", None)

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}"
            for k, v in sorted(data.items())
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

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        user_data = json.loads(data.get("user", "{}"))

        return user_data

    except Exception:
        return None


# =========================================================
# WEBSOCKET
# =========================================================

async def send_to_user(user_id: int, payload: dict):
    sockets = connections.get(user_id, set())

    dead = []

    for ws in sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)

    for ws in dead:
        sockets.discard(ws)


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


class CourierCreate(BaseModel):
    name: str
    phone: str
    pin: str


class OrderCreate(BaseModel):
    customer_id: int
    courier_id: Optional[int] = None
    address: str
    description: str
    amount: float = 0


class OnlineRequest(BaseModel):
    online: bool


# =========================================================
# FRONTEND
# =========================================================

@app.get("/")
async def index():
    return FileResponse("index.html")


@app.head("/")
async def head_index():
    return Response(status_code=200)


# =========================================================
# LOGIN
# =========================================================

@app.post("/api/login")
async def login(data: LoginRequest):
    phone = normalize_phone(data.phone)

    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE phone = ?",
        (phone,)
    ).fetchone()

    conn.close()

    if not user or not verify_pin(data.pin, user["pin_hash"]):
        raise HTTPException(
            401,
            "Неверный номер телефона или PIN"
        )

    token = create_session(user["id"])

    return {
        "ok": True,
        "token": token,
        "role": user["role"]
    }


@app.post("/api/logout")
async def logout(authorization: Optional[str] = Header(None)):
    if not authorization:
        return {"ok": True}

    token = authorization.replace("Bearer ", "", 1).strip()

    conn = db()
    conn.execute(
        "DELETE FROM sessions WHERE token = ?",
        (token,)
    )
    conn.commit()
    conn.close()

    return {"ok": True}


@app.get("/api/me")
async def me(authorization: Optional[str] = Header(None)):
    user = auth_user(authorization)

    result = {
        "id": user["id"],
        "phone": user["phone"],
        "role": user["role"]
    }

    conn = db()

    if user["role"] == "customer":
        row = conn.execute(
            "SELECT * FROM customers WHERE user_id = ?",
            (user["id"],)
        ).fetchone()

        if row:
            result["name"] = row["name"]
            result["customer_id"] = row["id"]

    elif user["role"] == "courier":
        row = conn.execute(
            "SELECT * FROM couriers WHERE user_id = ?",
            (user["id"],)
        ).fetchone()

        if row:
            result["name"] = row["name"]
            result["courier_id"] = row["id"]
            result["verified"] = bool(row["verified"])
            result["online"] = bool(row["online"])

    conn.close()

    return result


# =========================================================
# CUSTOMER
# =========================================================

@app.get("/api/customer/orders")
async def customer_orders(
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)

    if user["role"] != "customer":
        raise HTTPException(403, "Customer only")

    conn = db()

    customer = conn.execute(
        "SELECT * FROM customers WHERE user_id = ?",
        (user["id"],)
    ).fetchone()

    if not customer:
        conn.close()
        return []

    orders = conn.execute("""
        SELECT
            orders.*,
            couriers.name AS courier_name,
            couriers.latitude AS latitude,
            couriers.longitude AS longitude,
            couriers.location_updated_at AS location_updated_at
        FROM orders
        LEFT JOIN couriers
            ON couriers.id = orders.courier_id
        WHERE orders.customer_id = ?
        ORDER BY orders.id DESC
    """, (customer["id"],)).fetchall()

    conn.close()

    return [dict(x) for x in orders]


@app.post("/api/orders/{order_id}/confirm")
async def confirm_order(
    order_id: int,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)

    if user["role"] != "customer":
        raise HTTPException(403)

    conn = db()

    customer = conn.execute(
        "SELECT id FROM customers WHERE user_id = ?",
        (user["id"],)
    ).fetchone()

    order = conn.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    ).fetchone()

    if not customer or not order:
        conn.close()
        raise HTTPException(404)

    if order["customer_id"] != customer["id"]:
        conn.close()
        raise HTTPException(403)

    if order["status"] != "delivered":
        conn.close()
        raise HTTPException(
            400,
            "Заказ ещё не доставлен"
        )

    conn.execute("""
        UPDATE orders
        SET status = 'closed', updated_at = ?
        WHERE id = ?
    """, (now_iso(), order_id))

    conn.commit()
    conn.close()

    return {"ok": True}


# =========================================================
# COURIER
# =========================================================

@app.get("/api/courier/orders")
async def courier_orders(
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)

    if user["role"] != "courier":
        raise HTTPException(403)

    conn = db()

    courier = conn.execute(
        "SELECT * FROM couriers WHERE user_id = ?",
        (user["id"],)
    ).fetchone()

    if not courier:
        conn.close()
        return []

    orders = conn.execute("""
        SELECT
            orders.*,
            customers.name AS customer_name,
            customers.phone AS customer_phone
        FROM orders
        JOIN customers
            ON customers.id = orders.customer_id
        WHERE orders.courier_id = ?
        ORDER BY orders.id DESC
    """, (courier["id"],)).fetchall()

    conn.close()

    return [dict(x) for x in orders]


@app.post("/api/courier/online")
async def courier_online(
    data: OnlineRequest,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)

    if user["role"] != "courier":
        raise HTTPException(403)

    conn = db()

    conn.execute("""
        UPDATE couriers
        SET online = ?
        WHERE user_id = ?
    """, (1 if data.online else 0, user["id"]))

    conn.commit()
    conn.close()

    return {"ok": True}


async def change_order_status(
    order_id: int,
    new_status: str,
    user
):
    if user["role"] != "courier":
        raise HTTPException(403)

    conn = db()

    courier = conn.execute(
        "SELECT id FROM couriers WHERE user_id = ?",
        (user["id"],)
    ).fetchone()

    order = conn.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    ).fetchone()

    if not courier or not order:
        conn.close()
        raise HTTPException(404)

    if order["courier_id"] != courier["id"]:
        conn.close()
        raise HTTPException(403)

    conn.execute("""
        UPDATE orders
        SET status = ?, updated_at = ?
        WHERE id = ?
    """, (new_status, now_iso(), order_id))

    customer = conn.execute("""
        SELECT users.id
        FROM customers
        JOIN users ON users.id = customers.user_id
        WHERE customers.id = ?
    """, (order["customer_id"],)).fetchone()

    conn.commit()
    conn.close()

    if customer:
        await send_to_user(
            customer["id"],
            {
                "type": "order_status",
                "order_id": order_id,
                "status": new_status
            }
        )

    return {"ok": True, "status": new_status}


@app.post("/api/courier/orders/{order_id}/accept")
async def accept_order(
    order_id: int,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    return await change_order_status(
        order_id,
        "accepted",
        user
    )


@app.post("/api/courier/orders/{order_id}/start")
async def start_order(
    order_id: int,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    return await change_order_status(
        order_id,
        "delivering",
        user
    )


@app.post("/api/courier/orders/{order_id}/complete")
async def complete_order(
    order_id: int,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    return await change_order_status(
        order_id,
        "delivered",
        user
    )


# =========================================================
# ADMIN AUTH THROUGH TELEGRAM WEB APP
# =========================================================

@app.post("/api/admin/web-login")
async def admin_web_login(
    init_data: str
):
    tg_user = validate_telegram_init_data(init_data)

    if not tg_user:
        raise HTTPException(
            401,
            "Telegram authorization invalid"
        )

    telegram_id = int(tg_user.get("id", 0))

    if telegram_id != ADMIN_ID:
        raise HTTPException(
            403,
            "Admin access denied"
        )

    # Создаём отдельного admin пользователя
    phone = f"telegram_admin_{telegram_id}"

    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE phone = ?",
        (phone,)
    ).fetchone()

    if not user:
        pin = secrets.token_urlsafe(16)

        conn.execute("""
            INSERT INTO users(phone,pin_hash,role,created_at)
            VALUES(?,?,?,?)
        """, (
            phone,
            hash_pin(pin),
            "admin",
            now_iso()
        ))

        conn.commit()

        user = conn.execute(
            "SELECT * FROM users WHERE phone = ?",
            (phone,)
        ).fetchone()

    conn.close()

    token = create_session(user["id"])

    return {
        "ok": True,
        "token": token,
        "role": "admin"
    }


# =========================================================
# ADMIN - CUSTOMERS
# =========================================================

@app.get("/api/admin/customers")
async def admin_customers(
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    conn = db()

    rows = conn.execute("""
        SELECT
            customers.*,
            users.phone,
            (
                SELECT COUNT(*)
                FROM orders
                WHERE orders.customer_id = customers.id
            ) AS orders_count
        FROM customers
        JOIN users ON users.id = customers.user_id
        ORDER BY customers.id DESC
    """).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/admin/customers")
async def admin_add_customer(
    data: CustomerCreate,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    phone = normalize_phone(data.phone)

    if len(data.pin) < 4:
        raise HTTPException(
            400,
            "PIN должен быть минимум 4 символа"
        )

    conn = db()

    existing = conn.execute(
        "SELECT id FROM users WHERE phone = ?",
        (phone,)
    ).fetchone()

    if existing:
        conn.close()
        raise HTTPException(
            400,
            "Пользователь с таким номером уже существует"
        )

    cur = conn.execute("""
        INSERT INTO users(phone,pin_hash,role,created_at)
        VALUES(?,?,?,?)
    """, (
        phone,
        hash_pin(data.pin),
        "customer",
        now_iso()
    ))

    user_id = cur.lastrowid

    conn.execute("""
        INSERT INTO customers(user_id,name,phone,created_at)
        VALUES(?,?,?,?)
    """, (
        user_id,
        data.name,
        phone,
        now_iso()
    ))

    conn.commit()
    conn.close()

    return {"ok": True}


# =========================================================
# ADMIN - COURIERS
# =========================================================

@app.get("/api/admin/couriers")
async def admin_couriers(
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    conn = db()

    rows = conn.execute("""
        SELECT * FROM couriers
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/admin/couriers")
async def admin_add_courier(
    data: CourierCreate,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    phone = normalize_phone(data.phone)

    conn = db()

    existing = conn.execute(
        "SELECT id FROM users WHERE phone = ?",
        (phone,)
    ).fetchone()

    if existing:
        conn.close()
        raise HTTPException(
            400,
            "Пользователь с таким номером уже существует"
        )

    cur = conn.execute("""
        INSERT INTO users(phone,pin_hash,role,created_at)
        VALUES(?,?,?,?)
    """, (
        phone,
        hash_pin(data.pin),
        "courier",
        now_iso()
    ))

    user_id = cur.lastrowid

    conn.execute("""
        INSERT INTO couriers(
            user_id,name,phone,verified,created_at
        )
        VALUES(?,?,?,?,?)
    """, (
        user_id,
        data.name,
        phone,
        1,
        now_iso()
    ))

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/admin/couriers/{courier_id}/verify")
async def verify_courier(
    courier_id: int,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    conn = db()

    conn.execute("""
        UPDATE couriers
        SET verified = 1
        WHERE id = ?
    """, (courier_id,))

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/admin/couriers/{courier_id}/toggle")
async def toggle_courier(
    courier_id: int,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    conn = db()

    courier = conn.execute(
        "SELECT online FROM couriers WHERE id = ?",
        (courier_id,)
    ).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(404)

    new_value = 0 if courier["online"] else 1

    conn.execute("""
        UPDATE couriers
        SET online = ?
        WHERE id = ?
    """, (new_value, courier_id))

    conn.commit()
    conn.close()

    return {"ok": True}


# =========================================================
# ADMIN - ORDERS
# =========================================================

@app.get("/api/admin/orders")
async def admin_orders(
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    conn = db()

    rows = conn.execute("""
        SELECT
            orders.*,
            customers.name AS customer_name,
            customers.phone AS customer_phone,
            couriers.name AS courier_name,
            couriers.phone AS courier_phone
        FROM orders
        JOIN customers
            ON customers.id = orders.customer_id
        LEFT JOIN couriers
            ON couriers.id = orders.courier_id
        ORDER BY orders.id DESC
    """).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/admin/orders")
async def admin_create_order(
    data: OrderCreate,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    if not data.address.strip():
        raise HTTPException(400, "Введите адрес")

    if not data.description.strip():
        raise HTTPException(400, "Введите описание заказа")

    conn = db()

    customer = conn.execute(
        "SELECT id FROM customers WHERE id = ?",
        (data.customer_id,)
    ).fetchone()

    if not customer:
        conn.close()
        raise HTTPException(404, "Клиент не найден")

    courier_id = data.courier_id

    if courier_id:
        courier = conn.execute(
            "SELECT id, verified FROM couriers WHERE id = ?",
            (courier_id,)
        ).fetchone()

        if not courier:
            conn.close()
            raise HTTPException(404, "Курьер не найден")

        if not courier["verified"]:
            conn.close()
            raise HTTPException(
                400,
                "Курьер не подтверждён"
            )

    status = "assigned" if courier_id else "new"

    cur = conn.execute("""
        INSERT INTO orders(
            customer_id,
            courier_id,
            address,
            description,
            amount,
            status,
            created_at,
            updated_at
        )
        VALUES(?,?,?,?,?,?,?,?)
    """, (
        data.customer_id,
        courier_id,
        data.address,
        data.description,
        data.amount,
        status,
        now_iso(),
        now_iso()
    ))

    order_id = cur.lastrowid

    # Получаем владельца клиента
    customer_user = conn.execute("""
        SELECT user_id
        FROM customers
        WHERE id = ?
    """, (data.customer_id,)).fetchone()

    courier_user = None

    if courier_id:
        courier_user = conn.execute("""
            SELECT user_id
            FROM couriers
            WHERE id = ?
        """, (courier_id,)).fetchone()

    conn.commit()
    conn.close()

    if customer_user:
        await send_to_user(
            customer_user["user_id"],
            {
                "type": "new_order",
                "order_id": order_id
            }
        )

    if courier_user:
        await send_to_user(
            courier_user["user_id"],
            {
                "type": "new_order",
                "order_id": order_id
            }
        )

    return {
        "ok": True,
        "order_id": order_id
    }


@app.post("/api/admin/orders/{order_id}/assign")
async def admin_assign_order(
    order_id: int,
    courier_id: int,
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    conn = db()

    courier = conn.execute(
        "SELECT * FROM couriers WHERE id = ?",
        (courier_id,)
    ).fetchone()

    order = conn.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,)
    ).fetchone()

    if not courier or not order:
        conn.close()
        raise HTTPException(404)

    if not courier["verified"]:
        conn.close()
        raise HTTPException(
            400,
            "Курьер не подтверждён"
        )

    conn.execute("""
        UPDATE orders
        SET courier_id = ?,
            status = 'assigned',
            updated_at = ?
        WHERE id = ?
    """, (
        courier_id,
        now_iso(),
        order_id
    ))

    courier_user = courier["user_id"]

    customer = conn.execute("""
        SELECT user_id
        FROM customers
        WHERE id = ?
    """, (order["customer_id"],)).fetchone()

    conn.commit()
    conn.close()

    await send_to_user(
        courier_user,
        {
            "type": "order_assigned",
            "order_id": order_id
        }
    )

    if customer:
        await send_to_user(
            customer["user_id"],
            {
                "type": "order_assigned",
                "order_id": order_id
            }
        )

    return {"ok": True}


# =========================================================
# ADMIN STATS
# =========================================================

@app.get("/api/admin/stats")
async def admin_stats(
    authorization: Optional[str] = Header(None)
):
    user = auth_user(authorization)
    admin_required(user)

    conn = db()

    customers = conn.execute(
        "SELECT COUNT(*) AS c FROM customers"
    ).fetchone()["c"]

    couriers = conn.execute(
        "SELECT COUNT(*) AS c FROM couriers"
    ).fetchone()["c"]

    online = conn.execute(
        "SELECT COUNT(*) AS c FROM couriers WHERE online = 1"
    ).fetchone()["c"]

    active_orders = conn.execute("""
        SELECT COUNT(*) AS c
        FROM orders
        WHERE status NOT IN ('closed')
    """).fetchone()["c"]

    completed = conn.execute("""
        SELECT COUNT(*) AS c
        FROM orders
        WHERE status = 'closed'
    """).fetchone()["c"]

    revenue = conn.execute("""
        SELECT COALESCE(SUM(amount),0) AS total
        FROM orders
        WHERE status = 'closed'
    """).fetchone()["total"]

    conn.close()

    return {
        "customers": customers,
        "couriers": couriers,
        "online_couriers": online,
        "active_orders": active_orders,
        "completed_orders": completed,
        "revenue": revenue
    }


# =========================================================
# COURIER LOCATION FROM API
# =========================================================

async def update_courier_location(
    telegram_id: int,
    latitude: float,
    longitude: float
):
    conn = db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE telegram_id = ?
    """, (telegram_id,)).fetchone()

    if not courier:
        conn.close()
        return

    conn.execute("""
        UPDATE couriers
        SET latitude = ?,
            longitude = ?,
            location_updated_at = ?
        WHERE telegram_id = ?
    """, (
        latitude,
        longitude,
        now_iso(),
        telegram_id
    ))

    orders = conn.execute("""
        SELECT * FROM orders
        WHERE courier_id = ?
        AND status IN ('assigned','accepted','delivering')
    """, (courier["id"],)).fetchall()

    customers = []

    for order in orders:
        customer = conn.execute("""
            SELECT user_id
            FROM customers
            WHERE id = ?
        """, (order["customer_id"],)).fetchone()

        if customer:
            customers.append(
                (customer["user_id"], order["id"])
            )

    conn.commit()
    conn.close()

    for user_id, order_id in customers:
        await send_to_user(
            user_id,
            {
                "type": "courier_location",
                "order_id": order_id,
                "latitude": latitude,
                "longitude": longitude,
                "updated_at": now_iso()
            }
        )


# =========================================================
# WEBSOCKET
# =========================================================

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str
):
    user = get_user_from_token(token)

    if not user:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    user_id = user["id"]

    if user_id not in connections:
        connections[user_id] = set()

    connections[user_id].add(websocket)

    try:
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass

    finally:
        if user_id in connections:
            connections[user_id].discard(websocket)


# =========================================================
# TELEGRAM BOT
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    keyboard = []

    if WEB_APP_URL:
        keyboard.append([
            InlineKeyboardButton(
                "🍽 Открыть приложение",
                web_app=WebAppInfo(url=WEB_APP_URL)
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "🚚 Стать курьером",
            callback_data="courier_info"
        )
    ])

    await update.message.reply_text(
        "🍽 RESTARAN\n\n"
        "Добро пожаловать!\n"
        "Откройте приложение, чтобы пользоваться сервисом.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def courier_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "🚚 Регистрация курьера\n\n"
        "Отправьте свой номер телефона через Telegram Contact."
    )


async def location_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    location = update.effective_message.location

    if not location:
        return

    await update_courier_location(
        update.effective_user.id,
        location.latitude,
        location.longitude
    )

    await update.effective_message.reply_text(
        "📍 Геопозиция получена."
    )


async def edited_location_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.edited_message

    if not message or not message.location:
        return

    await update_courier_location(
        update.effective_user.id,
        message.location.latitude,
        message.location.longitude
    )


# =========================================================
# STARTUP / SHUTDOWN
# =========================================================

@app.on_event("startup")
async def startup():
    global bot_app

    bot_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    bot_app.add_handler(
        CommandHandler("start", start_command)
    )

    bot_app.add_handler(
        CommandHandler("courier", courier_command)
    )

    bot_app.add_handler(
        MessageHandler(
            filters.LOCATION,
            location_handler
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE & filters.LOCATION,
            edited_location_handler
        )
    )

    await bot_app.initialize()
    await bot_app.start()

    if bot_app.updater:
        await bot_app.updater.start_polling()

    print("RESTARAN bot started")


@app.on_event("shutdown")
async def shutdown():
    global bot_app

    if bot_app:
        if bot_app.updater:
            await bot_app.updater.stop()

        await bot_app.stop()
        await bot_app.shutdown()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "10000"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
)
