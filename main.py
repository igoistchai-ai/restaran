import os
import re
import hmac
import hashlib
import secrets
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "restaran.db")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-this-password")

PORT = int(os.getenv("PORT", "10000"))

app = FastAPI(title="RESTARAN")

bot_app = None
websockets = set()


# =========================================================
# DATABASE
# =========================================================

def database():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = database()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT UNIQUE NOT NULL,
        pin_hash TEXT NOT NULL,
        telegram_id INTEGER,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS couriers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT UNIQUE NOT NULL,
        pin_hash TEXT NOT NULL,
        telegram_id INTEGER,
        photo_file_id TEXT,

        verified INTEGER DEFAULT 0,
        online INTEGER DEFAULT 0,

        lat REAL,
        lng REAL,
        location_at TEXT,

        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,

        customer_id INTEGER NOT NULL,
        courier_id INTEGER,

        address TEXT NOT NULL,
        comment TEXT DEFAULT '',

        total REAL DEFAULT 0,

        status TEXT DEFAULT 'new',

        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,

        role TEXT NOT NULL,
        user_id INTEGER,

        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    """)

    conn.commit()
    conn.close()


init_db()


# =========================================================
# HELPERS
# =========================================================

def current_time():
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(phone):
    digits = re.sub(r"\D", "", phone or "")

    if digits.startswith("00"):
        digits = digits[2:]

    if not digits:
        return ""

    return "+" + digits


def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()


def create_session(role, user_id=None):
    token = secrets.token_urlsafe(40)

    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=7)

    conn = database()

    conn.execute(
        """
        INSERT INTO sessions
        (token, role, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            token,
            role,
            user_id,
            created.isoformat(),
            expires.isoformat()
        )
    )

    conn.commit()
    conn.close()

    return token


def get_session(request: Request):
    authorization = request.headers.get("Authorization", "")

    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Необходимо войти")

    token = authorization.replace("Bearer ", "").strip()

    conn = database()

    row = conn.execute(
        """
        SELECT *
        FROM sessions
        WHERE token=?
        AND expires_at>?
        """,
        (token, current_time())
    ).fetchone()

    conn.close()

    if not row:
        raise HTTPException(401, "Сессия недействительна")

    return dict(row)


def require_role(request, role):
    session = get_session(request)

    if session["role"] != role:
        raise HTTPException(403, "Недостаточно прав")

    return session


async def broadcast(data):
    dead = []

    for ws in list(websockets):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)

    for ws in dead:
        websockets.discard(ws)


# =========================================================
# MODELS
# =========================================================

class LoginData(BaseModel):
    phone: str
    pin: str


class AdminLogin(BaseModel):
    password: str


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
    comment: str = ""
    total: float = 0


class OnlineData(BaseModel):
    online: bool


# =========================================================
# PAGES
# =========================================================

@app.get("/")
async def index():
    return FileResponse(
        os.path.join(BASE_DIR, "index.html")
    )


@app.get("/admin")
async def admin():
    return FileResponse(
        os.path.join(BASE_DIR, "index.html")
    )


@app.get("/health")
async def health():
    return {
        "status": "ok"
    }


# =========================================================
# AUTH
# =========================================================

@app.post("/api/login")
async def login(data: LoginData):

    phone = normalize_phone(data.phone)

    conn = database()

    # CUSTOMER
    customer = conn.execute(
        """
        SELECT *
        FROM customers
        WHERE phone=?
        """,
        (phone,)
    ).fetchone()

    if customer:

        if hmac.compare_digest(
            customer["pin_hash"],
            hash_pin(data.pin)
        ):

            token = create_session(
                "customer",
                customer["id"]
            )

            conn.close()

            return {
                "ok": True,
                "token": token,
                "role": "customer"
            }

    # COURIER
    courier = conn.execute(
        """
        SELECT *
        FROM couriers
        WHERE phone=?
        """,
        (phone,)
    ).fetchone()

    conn.close()

    if courier:

        if not courier["verified"]:
            raise HTTPException(
                403,
                "Ваш аккаунт курьера ещё не подтверждён"
            )

        if hmac.compare_digest(
            courier["pin_hash"],
            hash_pin(data.pin)
        ):

            token = create_session(
                "courier",
                courier["id"]
            )

            return {
                "ok": True,
                "token": token,
                "role": "courier"
            }

    raise HTTPException(
        401,
        "Неверный номер телефона или PIN"
    )


@app.post("/api/admin/login")
async def admin_login(data: AdminLogin):

    if not hmac.compare_digest(
        data.password,
        ADMIN_PASSWORD
    ):
        raise HTTPException(
            401,
            "Неверный пароль"
        )

    return {
        "ok": True,
        "token": create_session("admin"),
        "role": "admin"
    }


@app.get("/api/me")
async def me(request: Request):

    session = get_session(request)

    conn = database()

    if session["role"] == "customer":

        user = conn.execute(
            """
            SELECT id,name,phone
            FROM customers
            WHERE id=?
            """,
            (session["user_id"],)
        ).fetchone()

    elif session["role"] == "courier":

        user = conn.execute(
            """
            SELECT
                id,
                name,
                phone,
                verified,
                online,
                lat,
                lng,
                location_at
            FROM couriers
            WHERE id=?
            """,
            (session["user_id"],)
        ).fetchone()

    else:

        user = {
            "name": "Administrator"
        }

    conn.close()

    return {
        "role": session["role"],
        "user": dict(user) if user else None
    }


# =========================================================
# CUSTOMER
# =========================================================

@app.get("/api/customer/orders")
async def customer_orders(request: Request):

    session = require_role(
        request,
        "customer"
    )

    conn = database()

    rows = conn.execute(
        """
        SELECT

            o.id,
            o.address,
            o.comment,
            o.total,
            o.status,
            o.created_at,
            o.updated_at,

            cr.id AS courier_id,
            cr.name AS courier_name,

            cr.lat AS courier_lat,
            cr.lng AS courier_lng,

            cr.online AS courier_online,
            cr.location_at AS courier_location_at

        FROM orders o

        LEFT JOIN couriers cr
        ON cr.id=o.courier_id

        WHERE o.customer_id=?

        ORDER BY o.id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    conn.close()

    return {
        "orders": [
            dict(row)
            for row in rows
        ]
    }


@app.post("/api/customer/orders/{order_id}/confirm")
async def confirm_order(
    order_id: int,
    request: Request
):

    session = require_role(
        request,
        "customer"
    )

    conn = database()

    order = conn.execute(
        """
        SELECT *
        FROM orders
        WHERE id=?
        AND customer_id=?
        """,
        (
            order_id,
            session["user_id"]
        )
    ).fetchone()

    if not order:
        conn.close()

        raise HTTPException(
            404,
            "Заказ не найден"
        )

    if order["status"] != "delivered":
        conn.close()

        raise HTTPException(
            400,
            "Заказ ещё не доставлен"
        )

    conn.execute(
        """
        UPDATE orders

        SET status='closed',
            updated_at=?

        WHERE id=?
        """,
        (
            current_time(),
            order_id
        )
    )

    conn.commit()
    conn.close()

    await broadcast({
        "type": "order_updated",
        "order_id": order_id,
        "status": "closed"
    })

    return {
        "ok": True
    }


# =========================================================
# COURIER
# =========================================================

@app.get("/api/courier/orders")
async def courier_orders(request: Request):

    session = require_role(
        request,
        "courier"
    )

    conn = database()

    rows = conn.execute(
        """
        SELECT

            o.id,
            o.address,
            o.comment,
            o.total,
            o.status,
            o.created_at,

            c.name AS customer_name,
            c.phone AS customer_phone

        FROM orders o

        JOIN customers c
        ON c.id=o.customer_id

        WHERE o.courier_id=?

        ORDER BY o.id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    conn.close()

    return {
        "orders": [
            dict(row)
            for row in rows
        ]
    }


@app.post("/api/courier/online")
async def courier_online(
    data: OnlineData,
    request: Request
):

    session = require_role(
        request,
        "courier"
    )

    conn = database()

    conn.execute(
        """
        UPDATE couriers

        SET online=?

        WHERE id=?
        """,
        (
            1 if data.online else 0,
            session["user_id"]
        )
    )

    conn.commit()
    conn.close()

    await broadcast({
        "type": "courier_online",
        "courier_id": session["user_id"],
        "online": data.online
    })

    return {
        "ok": True
    }


async def courier_status(
    request,
    order_id,
    old_status,
    new_status
):

    session = require_role(
        request,
        "courier"
    )

    conn = database()

    order = conn.execute(
        """
        SELECT *
        FROM orders

        WHERE id=?
        AND courier_id=?
        """,
        (
            order_id,
            session["user_id"]
        )
    ).fetchone()

    if not order:
        conn.close()

        raise HTTPException(
            404,
            "Заказ не найден"
        )

    if order["status"] != old_status:
        conn.close()

        raise HTTPException(
            400,
            "Невозможно выполнить действие"
        )

    conn.execute(
        """
        UPDATE orders

        SET status=?,
            updated_at=?

        WHERE id=?
        """,
        (
            new_status,
            current_time(),
            order_id
        )
    )

    conn.commit()
    conn.close()

    await broadcast({
        "type": "order_updated",
        "order_id": order_id,
        "status": new_status
    })

    return {
        "ok": True
    }


@app.post("/api/courier/orders/{order_id}/accept")
async def courier_accept(
    order_id: int,
    request: Request
):
    return await courier_status(
        request,
        order_id,
        "assigned",
        "accepted"
    )


@app.post("/api/courier/orders/{order_id}/start")
async def courier_start(
    order_id: int,
    request: Request
):
    return await courier_status(
        request,
        order_id,
        "accepted",
        "delivering"
    )


@app.post("/api/courier/orders/{order_id}/complete")
async def courier_complete(
    order_id: int,
    request: Request
):
    return await courier_status(
        request,
        order_id,
        "delivering",
        "delivered"
    )


# =========================================================
# ADMIN
# =========================================================

@app.get("/api/admin/stats")
async def admin_stats(request: Request):

    require_role(
        request,
        "admin"
    )

    conn = database()

    result = {

        "customers":
            conn.execute(
                "SELECT COUNT(*) FROM customers"
            ).fetchone()[0],

        "couriers":
            conn.execute(
                "SELECT COUNT(*) FROM couriers"
            ).fetchone()[0],

        "verified_couriers":
            conn.execute(
                """
                SELECT COUNT(*)
                FROM couriers
                WHERE verified=1
                """
            ).fetchone()[0],

        "pending_couriers":
            conn.execute(
                """
                SELECT COUNT(*)
                FROM couriers
                WHERE verified=0
                """
            ).fetchone()[0],

        "online_couriers":
            conn.execute(
                """
                SELECT COUNT(*)
                FROM couriers
                WHERE verified=1
                AND online=1
                """
            ).fetchone()[0],

        "active_orders":
            conn.execute(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE status!='closed'
                """
            ).fetchone()[0],

        "closed_orders":
            conn.execute(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE status='closed'
                """
            ).fetchone()[0]
    }

    conn.close()

    return result


@app.get("/api/admin/customers")
async def admin_customers(request: Request):

    require_role(
        request,
        "admin"
    )

    conn = database()

    rows = conn.execute(
        """
        SELECT *
        FROM customers
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return {
        "customers": [
            dict(row)
            for row in rows
        ]
    }


@app.post("/api/admin/customers")
async def admin_create_customer(
    data: CustomerCreate,
    request: Request
):

    require_role(
        request,
        "admin"
    )

    phone = normalize_phone(
        data.phone
    )

    if len(re.sub(r"\D", "", phone)) < 8:
        raise HTTPException(
            400,
            "Некорректный номер"
        )

    if len(data.pin) < 4:
        raise HTTPException(
            400,
            "PIN должен быть минимум 4 символа"
        )

    conn = database()

    try:

        cur = conn.execute(
            """
            INSERT INTO customers
            (name,phone,pin_hash,created_at)

            VALUES (?,?,?,?)
            """,
            (
                data.name.strip(),
                phone,
                hash_pin(data.pin),
                current_time()
            )
        )

        conn.commit()

        customer_id = cur.lastrowid

    except sqlite3.IntegrityError:

        conn.close()

        raise HTTPException(
            409,
            "Клиент уже существует"
        )

    conn.close()

    return {
        "ok": True,
        "id": customer_id
    }


@app.get("/api/admin/couriers")
async def admin_couriers(request: Request):

    require_role(
        request,
        "admin"
    )

    conn = database()

    rows = conn.execute(
        """
        SELECT *

        FROM couriers

        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return {
        "couriers": [
            dict(row)
            for row in rows
        ]
    }


@app.post("/api/admin/couriers")
async def admin_create_courier(
    data: CourierCreate,
    request: Request
):

    require_role(
        request,
        "admin"
    )

    phone = normalize_phone(
        data.phone
    )

    conn = database()

    try:

        cur = conn.execute(
            """
            INSERT INTO couriers
            (name,phone,pin_hash,created_at)

            VALUES (?,?,?,?)
            """,
            (
                data.name.strip(),
                phone,
                hash_pin(data.pin),
                current_time()
            )
        )

        conn.commit()

        courier_id = cur.lastrowid

    except sqlite3.IntegrityError:

        conn.close()

        raise HTTPException(
            409,
            "Курьер уже существует"
        )

    conn.close()

    return {
        "ok": True,
        "id": courier_id
    }


@app.post("/api/admin/couriers/{courier_id}/approve")
async def approve_courier(
    courier_id: int,
    request: Request
):

    require_role(
        request,
        "admin"
    )

    conn = database()

    courier = conn.execute(
        """
        SELECT *
        FROM couriers
        WHERE id=?
        """,
        (courier_id,)
    ).fetchone()

    if not courier:
        conn.close()

        raise HTTPException(
            404,
            "Курьер не найден"
        )

    conn.execute(
        """
        UPDATE couriers

        SET verified=1

        WHERE id=?
        """,
        (courier_id,)
    )

    conn.commit()
    conn.close()

    if courier["telegram_id"] and bot_app:

        try:

            await bot_app.bot.send_message(
                courier["telegram_id"],
                "✅ Ваша заявка курьера подтверждена."
            )

        except Exception:
            pass

    return {
        "ok": True
    }


@app.get("/api/admin/orders")
async def admin_orders(request: Request):

    require_role(
        request,
        "admin"
    )

    conn = database()

    rows = conn.execute(
        """
        SELECT

            o.*,

            c.name AS customer_name,
            c.phone AS customer_phone,

            cr.name AS courier_name,
            cr.phone AS courier_phone,

            cr.lat AS courier_lat,
            cr.lng AS courier_lng,
            cr.online AS courier_online

        FROM orders o

        JOIN customers c
        ON c.id=o.customer_id

        LEFT JOIN couriers cr
        ON cr.id=o.courier_id

        ORDER BY o.id DESC
        """
    ).fetchall()

    conn.close()

    return {
        "orders": [
            dict(row)
            for row in rows
        ]
    }


@app.post("/api/admin/orders")
async def admin_create_order(
    data: OrderCreate,
    request: Request
):

    require_role(
        request,
        "admin"
    )

    conn = database()

    customer = conn.execute(
        """
        SELECT *
        FROM customers
        WHERE id=?
        """,
        (data.customer_id,)
    ).fetchone()

    if not customer:
        conn.close()

        raise HTTPException(
            404,
            "Клиент не найден"
        )

    courier = None

    status = "new"

    if data.courier_id:

        courier = conn.execute(
            """
            SELECT *
            FROM couriers

            WHERE id=?
            AND verified=1
            """,
            (data.courier_id,)
        ).fetchone()

        if not courier:

            conn.close()

            raise HTTPException(
                400,
                "Курьер не найден или не подтверждён"
            )

        status = "assigned"

    cur = conn.execute(
        """
        INSERT INTO orders

        (
            customer_id,
            courier_id,
            address,
            comment,
            total,
            status,
            created_at,
            updated_at
        )

        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            data.customer_id,
            data.courier_id,
            data.address.strip(),
            data.comment.strip(),
            data.total,
            status,
            current_time(),
            current_time()
        )
    )

    conn.commit()

    order_id = cur.lastrowid

    conn.close()

    await broadcast({
        "type": "new_order",
        "order_id": order_id
    })

    # Notify courier
    if courier and courier["telegram_id"] and bot_app:

        try:

            await bot_app.bot.send_message(
                courier["telegram_id"],
                (
                    f"📦 Новый заказ #{order_id}\n\n"
                    f"📍 {data.address}\n"
                    f"💰 {data.total:.2f}"
                )
            )

        except Exception:
            pass

    return {
        "ok": True,
        "id": order_id
    }


# =========================================================
# WEBSOCKET
# =========================================================

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket
):

    await websocket.accept()

    websockets.add(
        websocket
    )

    try:

        while True:

            await websocket.receive_text()

    except WebSocketDisconnect:

        websockets.discard(
            websocket
        )

    except Exception:

        websockets.discard(
            websocket
        )


# =========================================================
# TELEGRAM COURIER BOT
# =========================================================

def courier_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 Отправить номер",
                    request_contact=True
                )
            ],

            [
                KeyboardButton(
                    "📍 Отправить геопозицию",
                    request_location=True
                )
            ]
        ],
        resize_keyboard=True
    )


async def telegram_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "RESTARAN\n\n"
        "Для регистрации курьера нажмите /courier",
        reply_markup=courier_keyboard()
    )


async def telegram_courier(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["courier_registration"] = True

    await update.message.reply_text(
        "🛵 Регистрация курьера\n\n"
        "Отправьте свой номер телефона.",
        reply_markup=courier_keyboard()
    )


async def telegram_contact(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    contact = update.message.contact

    telegram_id = update.effective_user.id

    phone = normalize_phone(
        contact.phone_number
    )

    conn = database()

    courier = conn.execute(
        """
        SELECT *
        FROM couriers
        WHERE phone=?
        """,
        (phone,)
    ).fetchone()

    if courier:

        conn.execute(
            """
            UPDATE couriers

            SET telegram_id=?

            WHERE id=?
            """,
            (
                telegram_id,
                courier["id"]
            )
        )

        conn.commit()

        context.user_data[
            "courier_phone"
        ] = phone

        await update.message.reply_text(
            "✅ Номер найден.\n\n"
            "Теперь отправьте фотографию."
        )

    else:

        pin = str(
            secrets.randbelow(9000) + 1000
        )

        conn.execute(
            """
            INSERT INTO couriers

            (
                name,
                phone,
                pin_hash,
                telegram_id,
                verified,
                created_at
            )

            VALUES (?,?,?,?,?,?)
            """,
            (
                update.effective_user.full_name,
                phone,
                hash_pin(pin),
                telegram_id,
                0,
                current_time()
            )
        )

        conn.commit()

        context.user_data[
            "courier_phone"
        ] = phone

        await update.message.reply_text(
            "📸 Отправьте фотографию для профиля.\n\n"
            f"Ваш PIN для входа: {pin}"
        )

    conn.close()


async def telegram_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    phone = context.user_data.get(
        "courier_phone"
    )

    if not phone:

        await update.message.reply_text(
            "Сначала используйте /courier"
        )

        return

    file_id = (
        update.message
        .photo[-1]
        .file_id
    )

    conn = database()

    conn.execute(
        """
        UPDATE couriers

        SET photo_file_id=?

        WHERE phone=?
        """,
        (
            file_id,
            phone
        )
    )

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ Фото сохранено.\n\n"
        "Теперь отправьте вашу геопозицию."
    )


async def save_location(
    telegram_id,
    lat,
    lng
):

    conn = database()

    courier = conn.execute(
        """
        SELECT id
        FROM couriers

        WHERE telegram_id=?
        """,
        (telegram_id,)
    ).fetchone()

    if not courier:

        conn.close()
        return

    location_time = current_time()

    conn.execute(
        """
        UPDATE couriers

        SET
            lat=?,
            lng=?,
            location_at=?,
            online=1

        WHERE id=?
        """,
        (
            lat,
            lng,
            location_time,
            courier["id"]
        )
    )

    conn.commit()
    conn.close()

    await broadcast({
        "type": "courier_location",

        "courier_id":
            courier["id"],

        "lat": lat,
        "lng": lng,

        "time":
            location_time
    })


async def telegram_location(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    location = update.message.location

    await save_location(
        update.effective_user.id,
        location.latitude,
        location.longitude
    )

    await update.message.reply_text(
        "📍 Геопозиция получена.\n"
        "Отслеживание включено."
    )


async def telegram_edited_location(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.edited_message:
        return

    location = (
        update.edited_message.location
    )

    if not location:
        return

    await save_location(
        update.effective_user.id,
        location.latitude,
        location.longitude
    )


async def run_telegram():

    global bot_app

    if not BOT_TOKEN:

        print(
            "BOT_TOKEN is not configured."
        )

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
            telegram_start
        )
    )

    bot_app.add_handler(
        CommandHandler(
            "courier",
            telegram_courier
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.CONTACT,
            telegram_contact
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.PHOTO,
            telegram_photo
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.LOCATION,
            telegram_location
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE
            & filters.LOCATION,
            telegram_edited_location
        )
    )

    await bot_app.initialize()

    await bot_app.start()

    if bot_app.updater:

        await bot_app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES
        )

    print(
        "Telegram bot started."
    )

    while True:

        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup():

    if BOT_TOKEN:

        asyncio.create_task(
            run_telegram()
        )


# =========================================================
# RENDER ENTRYPOINT
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT
    )
