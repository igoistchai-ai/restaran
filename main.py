import os
import re
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
from urllib.parse import parse_qsl, unquote

from fastapi import FastAPI, HTTPException, Header, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = 8357023784
WEB_APP_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
DB_PATH = "restaran.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

app = FastAPI(title="RESTARAN")


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
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            pin_hash TEXT NOT NULL,
            pin_plain TEXT,
            role TEXT NOT NULL DEFAULT 'customer',
            telegram_id INTEGER,
            active INTEGER DEFAULT 1,
            created_at INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS couriers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            approved INTEGER DEFAULT 0,
            online INTEGER DEFAULT 0,
            lat REAL,
            lon REAL,
            updated_at INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            courier_id INTEGER,
            title TEXT NOT NULL,
            address TEXT NOT NULL,
            price REAL DEFAULT 0,
            status TEXT DEFAULT 'new',
            created_at INTEGER NOT NULL,
            customer_confirmed INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

    conn.commit()

    def add_column(table, column, definition):
        columns = [
            x["name"]
            for x in cur.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        ]

        if column not in columns:
            cur.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    add_column("users", "pin_plain", "TEXT")
    add_column("users", "telegram_id", "INTEGER")
    add_column("users", "active", "INTEGER DEFAULT 1")

    add_column("couriers", "approved", "INTEGER DEFAULT 0")
    add_column("couriers", "online", "INTEGER DEFAULT 0")
    add_column("couriers", "lat", "REAL")
    add_column("couriers", "lon", "REAL")
    add_column("couriers", "updated_at", "INTEGER")

    add_column("orders", "courier_id", "INTEGER")
    add_column("orders", "price", "REAL DEFAULT 0")
    add_column("orders", "status", "TEXT DEFAULT 'new'")
    add_column("orders", "created_at", "INTEGER")
    add_column("orders", "customer_confirmed", "INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


init_db()


def normalize_phone(phone):
    return re.sub(r"\D", "", str(phone or ""))


def hash_pin(pin):
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode(),
        salt.encode(),
        120000
    ).hex()
    return f"{salt}${hashed}"


def check_pin(pin, stored):
    try:
        salt, hashed = stored.split("$", 1)
        test = hashlib.pbkdf2_hmac(
            "sha256",
            pin.encode(),
            salt.encode(),
            120000
        ).hex()
        return hmac.compare_digest(test, hashed)
    except Exception:
        return False


def create_session(user_id, role):
    token = secrets.token_urlsafe(48)

    conn = db()
    conn.execute(
        """
        INSERT INTO sessions(token,user_id,role,created_at)
        VALUES(?,?,?,?)
        """,
        (token, user_id, role, int(time.time()))
    )
    conn.commit()
    conn.close()

    return token


def get_session(token):
    if not token:
        return None

    conn = db()
    row = conn.execute(
        "SELECT * FROM sessions WHERE token=?",
        (token,)
    ).fetchone()
    conn.close()

    return row


def auth_user(authorization):
    if not authorization:
        raise HTTPException(401, "Авторизация отсутствует")

    token = authorization.replace("Bearer ", "").strip()
    session = get_session(token)

    if not session:
        raise HTTPException(401, "Сессия недействительна")

    if session["role"] != "admin":
        conn = db()
        user = conn.execute(
            "SELECT active FROM users WHERE id=?",
            (session["user_id"],)
        ).fetchone()
        conn.close()

        if not user or not user["active"]:
            raise HTTPException(
                403,
                "Аккаунт деактивирован"
            )

    return session


def admin_only(authorization):
    session = auth_user(authorization)

    if session["role"] != "admin":
        raise HTTPException(403, "Только администратор")

    return session


def validate_telegram_init_data(init_data):
    if not init_data:
        return None

    try:
        data = dict(
            parse_qsl(
                init_data,
                keep_blank_values=True
            )
        )

        received_hash = data.pop("hash", None)

        if not received_hash:
            return None

        check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(data.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated = hmac.new(
            secret_key,
            check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated,
            received_hash
        ):
            return None

        if "user" not in data:
            return None

        return json.loads(
            unquote(data["user"])
        )

    except Exception:
        return None


class LoginData(BaseModel):
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
    phone: str
    title: str
    address: str
    price: float = 0


class AssignData(BaseModel):
    courier_id: int


@app.get("/")
async def index():
    return FileResponse("index.html")


@app.head("/")
async def head_index():
    return {}


# ================= LOGIN =================

@app.post("/api/login")
async def login(data: LoginData):
    phone = normalize_phone(data.phone)

    conn = db()

    users = conn.execute(
        """
        SELECT *
        FROM users
        WHERE role IN ('customer','courier')
        AND active=1
        """
    ).fetchall()

    conn.close()

    user = next(
        (
            u for u in users
            if normalize_phone(u["phone"]) == phone
        ),
        None
    )

    if not user or not check_pin(
        data.pin,
        user["pin_hash"]
    ):
        raise HTTPException(
            401,
            "Неверный номер или PIN"
        )

    if user["role"] == "courier":
        conn = db()

        courier = conn.execute(
            "SELECT * FROM couriers WHERE user_id=?",
            (user["id"],)
        ).fetchone()

        conn.close()

        if not courier or not courier["approved"]:
            raise HTTPException(
                403,
                "Курьер ещё не подтверждён"
            )

    token = create_session(
        user["id"],
        user["role"]
    )

    return {
        "ok": True,
        "token": token,
        "role": user["role"],
        "name": user["name"]
    }


@app.post("/api/admin/web-login")
async def admin_web_login(
    init_data: str = Form(...)
):
    tg_user = validate_telegram_init_data(init_data)

    if not tg_user:
        raise HTTPException(
            401,
            "Не удалось проверить Telegram"
        )

    if int(tg_user["id"]) != ADMIN_ID:
        raise HTTPException(
            403,
            "Вы не администратор"
        )

    token = create_session(0, "admin")

    return {
        "ok": True,
        "token": token,
        "role": "admin",
        "name": "Администратор"
    }


@app.get("/api/me")
async def me(
    authorization: str | None = Header(None)
):
    session = auth_user(authorization)

    if session["role"] == "admin":
        return {
            "id": 0,
            "name": "Администратор",
            "role": "admin"
        }

    conn = db()

    user = conn.execute(
        """
        SELECT id,name,phone,role
        FROM users
        WHERE id=? AND active=1
        """,
        (session["user_id"],)
    ).fetchone()

    conn.close()

    if not user:
        raise HTTPException(
            404,
            "Пользователь не найден"
        )

    return dict(user)


# ================= CUSTOMER =================

@app.get("/api/customer/orders")
async def customer_orders(
    authorization: str | None = Header(None)
):
    session = auth_user(authorization)

    if session["role"] != "customer":
        raise HTTPException(403)

    conn = db()

    rows = conn.execute(
        """
        SELECT
            o.*,
            courier.name AS courier_name,
            cr.lat AS courier_lat,
            cr.lon AS courier_lon
        FROM orders o

        LEFT JOIN couriers cr
            ON cr.id=o.courier_id

        LEFT JOIN users courier
            ON courier.id=cr.user_id

        WHERE o.customer_id=?

        ORDER BY o.id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/customer/orders/{order_id}/confirm")
async def customer_confirm(
    order_id: int,
    authorization: str | None = Header(None)
):
    session = auth_user(authorization)

    if session["role"] != "customer":
        raise HTTPException(403)

    conn = db()

    result = conn.execute(
        """
        UPDATE orders
        SET customer_confirmed=1,
            status='closed'
        WHERE id=?
        AND customer_id=?
        AND status='delivered'
        """,
        (
            order_id,
            session["user_id"]
        )
    )

    conn.commit()
    conn.close()

    if result.rowcount == 0:
        raise HTTPException(
            400,
            "Заказ нельзя подтвердить"
        )

    return {"ok": True}


# ================= COURIER =================

def get_courier(session):
    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM couriers
        WHERE user_id=?
        """,
        (session["user_id"],)
    ).fetchone()

    conn.close()

    return row


@app.get("/api/courier/orders")
async def courier_orders(
    authorization: str | None = Header(None)
):
    session = auth_user(authorization)

    if session["role"] != "courier":
        raise HTTPException(403)

    courier = get_courier(session)

    if not courier:
        return []

    conn = db()

    rows = conn.execute(
        """
        SELECT
            o.*,
            u.name AS customer_name,
            u.phone AS customer_phone
        FROM orders o
        JOIN users u ON u.id=o.customer_id
        WHERE o.courier_id=?
        ORDER BY o.id DESC
        """,
        (courier["id"],)
    ).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/courier/online")
async def courier_online(
    authorization: str | None = Header(None)
):
    session = auth_user(authorization)

    if session["role"] != "courier":
        raise HTTPException(403)

    courier = get_courier(session)

    if not courier:
        raise HTTPException(404)

    value = 0 if courier["online"] else 1

    conn = db()

    conn.execute(
        """
        UPDATE couriers
        SET online=?
        WHERE id=?
        """,
        (value, courier["id"])
    )

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "online": bool(value)
    }


@app.post("/api/courier/orders/{order_id}/accept")
async def courier_accept(
    order_id: int,
    authorization: str | None = Header(None)
):
    session = auth_user(authorization)

    if session["role"] != "courier":
        raise HTTPException(403)

    courier = get_courier(session)

    if not courier:
        raise HTTPException(404)

    conn = db()

    result = conn.execute(
        """
        UPDATE orders
        SET status='accepted'
        WHERE id=?
        AND courier_id=?
        AND status='assigned'
        """,
        (order_id, courier["id"])
    )

    conn.commit()
    conn.close()

    return {"ok": result.rowcount > 0}


@app.post("/api/courier/orders/{order_id}/start")
async def courier_start(
    order_id: int,
    authorization: str | None = Header(None)
):
    session = auth_user(authorization)

    if session["role"] != "courier":
        raise HTTPException(403)

    courier = get_courier(session)

    if not courier:
        raise HTTPException(404)

    conn = db()

    result = conn.execute(
        """
        UPDATE orders
        SET status='delivering'
        WHERE id=?
        AND courier_id=?
        AND status='accepted'
        """,
        (order_id, courier["id"])
    )

    conn.commit()
    conn.close()

    return {"ok": result.rowcount > 0}


@app.post("/api/courier/orders/{order_id}/complete")
async def courier_complete(
    order_id: int,
    authorization: str | None = Header(None)
):
    session = auth_user(authorization)

    if session["role"] != "courier":
        raise HTTPException(403)

    courier = get_courier(session)

    if not courier:
        raise HTTPException(404)

    conn = db()

    result = conn.execute(
        """
        UPDATE orders
        SET status='delivered'
        WHERE id=?
        AND courier_id=?
        AND status='delivering'
        """,
        (order_id, courier["id"])
    )

    conn.commit()
    conn.close()

    return {"ok": result.rowcount > 0}


# ================= ADMIN STATS =================

@app.get("/api/admin/stats")
async def admin_stats(
    authorization: str | None = Header(None)
):
    admin_only(authorization)

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
        JOIN users u ON u.id=c.user_id
        WHERE u.active=1
        """
    ).fetchone()[0]

    orders = conn.execute(
        "SELECT COUNT(*) FROM orders"
    ).fetchone()[0]

    active_orders = conn.execute(
        """
        SELECT COUNT(*)
        FROM orders
        WHERE status NOT IN ('closed','delivered')
        """
    ).fetchone()[0]

    revenue = conn.execute(
        """
        SELECT COALESCE(SUM(price),0)
        FROM orders
        WHERE status IN ('delivered','closed')
        """
    ).fetchone()[0]

    conn.close()

    return {
        "customers": customers,
        "couriers": couriers,
        "orders": orders,
        "active_orders": active_orders,
        "revenue": revenue
    }


# ================= ADMIN CUSTOMERS =================

@app.get("/api/admin/customers")
async def admin_customers(
    authorization: str | None = Header(None)
):
    admin_only(authorization)

    conn = db()

    rows = conn.execute(
        """
        SELECT id,name,phone,telegram_id,active,created_at
        FROM users
        WHERE role='customer'
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/admin/customers")
async def admin_create_customer(
    data: CustomerCreate,
    authorization: str | None = Header(None)
):
    admin_only(authorization)

    phone = normalize_phone(data.phone)

    if not phone:
        raise HTTPException(
            400,
            "Введите номер телефона"
        )

    if not data.pin.isdigit() or len(data.pin) < 4:
        raise HTTPException(
            400,
            "PIN должен содержать минимум 4 цифры"
        )

    conn = db()

    exists = conn.execute(
        "SELECT id FROM users WHERE phone=?",
        (phone,)
    ).fetchone()

    if exists:
        conn.close()
        raise HTTPException(
            400,
            "Пользователь с таким номером уже существует"
        )

    cur = conn.execute(
        """
        INSERT INTO users
        (name,phone,pin_hash,pin_plain,role,active,created_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            data.name.strip(),
            phone,
            hash_pin(data.pin),
            data.pin,
            "customer",
            1,
            int(time.time())
        )
    )

    conn.commit()
    user_id = cur.lastrowid
    conn.close()

    return {
        "ok": True,
        "id": user_id
    }


# ================= ADMIN COURIERS =================

@app.get("/api/admin/couriers")
async def admin_couriers(
    authorization: str | None = Header(None)
):
    admin_only(authorization)

    conn = db()

    rows = conn.execute(
        """
        SELECT
            c.id,
            c.approved,
            c.online,
            c.lat,
            c.lon,
            u.name,
            u.phone,
            u.telegram_id,
            u.active
        FROM couriers c
        JOIN users u ON u.id=c.user_id
        ORDER BY c.id DESC
        """
    ).fetchall()

    conn.close()

    return [dict(x) for x in rows]


@app.post("/api/admin/couriers")
async def admin_create_courier(
    data: CourierCreate,
    authorization: str | None = Header(None)
):
    admin_only(authorization)

    phone = normalize_phone(data.phone)

    if not phone:
        raise HTTPException(
            400,
            "Введите номер телефона"
        )

    if not data.pin.isdigit() or len(data.pin) < 4:
        raise HTTPException(
            400,
            "PIN должен содержать минимум 4 цифры"
        )

    conn = db()

    exists = conn.execute(
        "SELECT id FROM users WHERE phone=?",
        (phone,)
    ).fetchone()

    if exists:
        conn.close()
        raise HTTPException(
            400,
            "Пользователь с таким номером уже существует"
        )

    cur = conn.execute(
        """
        INSERT INTO users
        (name,phone,pin_hash,pin_plain,role,active,created_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            data.name.strip(),
            phone,
            hash_pin(data.pin),
            data.pin,
            "courier",
            1,
            int(time.time())
        )
    )

    user_id = cur.lastrowid

    conn.execute(
        "INSERT INTO couriers(user_id) VALUES(?)",
        (user_id,)
    )

    conn.commit()
    conn.close()

    return {"ok": True}


@app.post("/api/admin/couriers/{courier_id}/verify")
async def verify_courier(
    courier_id: int,
    authorization: str | None = Header(None)
):
    admin_only(authorization)

    conn = db()

    result = conn.execute(
        """
        UPDATE couriers
        SET approved=1
        WHERE id=?
        """,
        (courier_id,)
    )

    conn.commit()
    conn.close()

    return {"ok": result.rowcount > 0}


@app.delete("/api/admin/couriers/{courier_id}")
async def fire_courier(
    courier_id: int,
    authorization: str | None = Header(None)
):
    admin_only(authorization)

    conn = db()

    courier = conn.execute(
        """
        SELECT user_id
        FROM couriers
        WHERE id=?
        """,
        (courier_id,)
    ).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(
            404,
            "Сотрудник не найден"
        )

    conn.execute(
        """
        UPDATE users
        SET active=0
        WHERE id=?
        """,
        (courier["user_id"],)
    )

    conn.execute(
        """
        UPDATE couriers
        SET approved=0, online=0
        WHERE id=?
        """,
        (courier_id,)
    )

    conn.execute(
        """
        UPDATE orders
        SET courier_id=NULL,
            status='new'
        WHERE courier_id=?
        AND status NOT IN ('delivered','closed')
        """,
        (courier_id,)
    )

    conn.commit()
    conn.close()

    return {"ok": True}


# ================= ADMIN ORDERS =================

@app.get("/api/admin/orders")
async def admin_orders(
    authorization: str | None = Header(None)
):
    admin_only(authorization)

    conn = db()

    rows = conn.execute(
        """
        SELECT
            o.*,
            cu.name AS customer_name,
            cu.phone AS customer_phone,
            cr.id AS courier_id,
            courier.name AS courier_name,
            courier.phone AS courier_phone
        FROM orders o
        JOIN users cu ON cu.id=o.customer_id
        LEFT JOIN couriers cr ON cr.id=o.courier_id
        LEFT JOIN users courier ON courier.id=cr.user_id
        ORDER BY o.id DESC
        """
    ).fetchall()

    conn.close()

    return [dict(x) for x in rows]


# =========================================================
# ВАЖНО:
# ЗАКАЗ СОЗДАЁТСЯ ПО НОМЕРУ ТЕЛЕФОНА
# =========================================================

@app.post("/api/admin/orders")
async def admin_create_order(
    data: OrderCreate,
    authorization: str | None = Header(None)
):
    admin_only(authorization)

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

    if data.price < 0:
        raise HTTPException(
            400,
            "Цена не может быть отрицательной"
        )

    conn = db()

    customers = conn.execute(
        """
        SELECT id,name,phone
        FROM users
        WHERE role='customer'
        AND active=1
        """
    ).fetchall()

    customer = None

    for user in customers:
        if normalize_phone(user["phone"]) == phone:
            customer = user
            break

    if customer is None:
        conn.close()
        raise HTTPException(
            404,
            "Клиент с таким номером не найден"
        )

    cur = conn.execute(
        """
        INSERT INTO orders
        (
            customer_id,
            title,
            address,
            price,
            status,
            created_at,
            customer_confirmed
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            customer["id"],
            data.title.strip(),
            data.address.strip(),
            float(data.price),
            "new",
            int(time.time()),
            0
        )
    )

    conn.commit()

    order_id = cur.lastrowid

    conn.close()

    return {
        "ok": True,
        "id": order_id,
        "customer": {
            "name": customer["name"],
            "phone": customer["phone"]
        }
    }


@app.post("/api/admin/orders/{order_id}/assign")
async def assign_order(
    order_id: int,
    data: AssignData,
    authorization: str | None = Header(None)
):
    admin_only(authorization)

    conn = db()

    courier = conn.execute(
        """
        SELECT c.id
        FROM couriers c
        JOIN users u ON u.id=c.user_id
        WHERE c.id=?
        AND c.approved=1
        AND u.active=1
        """,
        (data.courier_id,)
    ).fetchone()

    if not courier:
        conn.close()
        raise HTTPException(
            400,
            "Курьер не найден или не подтверждён"
        )

    result = conn.execute(
        """
        UPDATE orders
        SET courier_id=?, status='assigned'
        WHERE id=?
        AND status NOT IN ('closed','delivered')
        """,
        (data.courier_id, order_id)
    )

    conn.commit()
    conn.close()

    return {"ok": result.rowcount > 0}


@app.post("/api/admin/orders/{order_id}/close")
async def close_order(
    order_id: int,
    authorization: str | None = Header(None)
):
    admin_only(authorization)

    conn = db()

    result = conn.execute(
        """
        UPDATE orders
        SET status='closed',
            customer_confirmed=1
        WHERE id=?
        AND status!='closed'
        """,
        (order_id,)
    )

    conn.commit()
    conn.close()

    if result.rowcount == 0:
        raise HTTPException(
            404,
            "Заказ не найден"
        )

    return {"ok": True}


# ================= TELEGRAM =================

telegram_app = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [[
        InlineKeyboardButton(
            "🍽 Открыть приложение",
            web_app=WebAppInfo(
                url=WEB_APP_URL
            )
        )
    ]]

    await update.message.reply_text(
        "🍽 Добро пожаловать в RESTARAN!\n\n"
        "Для получения данных для входа используйте /random",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def random_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    keyboard = ReplyKeyboardMarkup(
        [[
            KeyboardButton(
                "📱 Отправить номер",
                request_contact=True
            )
        ]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    context.user_data["waiting_phone"] = True

    await update.message.reply_text(
        "📱 Отправьте номер телефона, "
        "который зарегистрирован в RESTARAN.",
        reply_markup=keyboard
    )


async def contact_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.user_data.get("waiting_phone"):
        return

    contact = update.message.contact

    if not contact:
        return

    telegram_id = update.effective_user.id
    phone = normalize_phone(contact.phone_number)

    conn = db()

    customers = conn.execute(
        """
        SELECT *
        FROM users
        WHERE role='customer'
        AND active=1
        """
    ).fetchall()

    user = next(
        (
            x for x in customers
            if normalize_phone(x["phone"]) == phone
        ),
        None
    )

    if not user:
        conn.close()

        context.user_data["waiting_phone"] = False

        await update.message.reply_text(
            "❌ Клиент с таким номером не найден."
        )
        return

    conn.execute(
        """
        UPDATE users
        SET telegram_id=?
        WHERE id=?
        """,
        (telegram_id, user["id"])
    )

    conn.commit()
    conn.close()

    context.user_data["waiting_phone"] = False

    keyboard = [[
        InlineKeyboardButton(
            "🍽 Открыть приложение",
            web_app=WebAppInfo(
                url=WEB_APP_URL
            )
        )
    ]]

    await update.message.reply_text(
        "✅ Аккаунт найден!\n\n"
        f"📱 Номер: {user['phone']}\n"
        f"🔐 PIN: {user['pin_plain']}\n\n"
        "Введите эти данные в приложении.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def telegram_location(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    location = update.message.location

    if not location:
        return

    telegram_id = update.effective_user.id

    conn = db()

    courier = conn.execute(
        """
        SELECT c.id
        FROM couriers c
        JOIN users u ON u.id=c.user_id
        WHERE u.telegram_id=?
        AND u.active=1
        """,
        (telegram_id,)
    ).fetchone()

    if courier:
        conn.execute(
            """
            UPDATE couriers
            SET lat=?,lon=?,updated_at=?
            WHERE id=?
            """,
            (
                location.latitude,
                location.longitude,
                int(time.time()),
                courier["id"]
            )
        )
        conn.commit()

    conn.close()


# ================= WEBSOCKET =================

connected_clients = set()


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = ""
):
    await websocket.accept()

    session = get_session(token)

    if not session:
        await websocket.close(code=1008)
        return

    connected_clients.add(websocket)

    try:
        while True:
            await websocket.receive_text()
    except Exception:
        connected_clients.discard(websocket)


# ================= START =================

@app.on_event("startup")
async def startup():

    global telegram_app

    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler("start", start)
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
            telegram_location
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()


@app.on_event("shutdown")
async def shutdown():

    global telegram_app

    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


if __name__ == "__main__":
    import uvicorn

    port = int(
        os.getenv("PORT", "10000")
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
