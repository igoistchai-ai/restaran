import os
import hmac
import hashlib
import sqlite3
import asyncio
import time
import json
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
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

BOT_TOKEN = os.getenv("8849558318:AAFcnAvMwjCVIREzfYfNg7rehkHTm_ysKgI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8357023784"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

# Render автоматически даёт эту переменную.
WEB_APP_URL = os.getenv("RENDER_EXTERNAL_URL")

if not WEB_APP_URL:
    raise RuntimeError(
        "RENDER_EXTERNAL_URL was not detected"
    )

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
DB_PATH = BASE_DIR / "delivery.db"

app = FastAPI()

connections = set()


# =========================================================
# DATABASE
# =========================================================

def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():

    connection = db()

    connection.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        role TEXT DEFAULT 'customer',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
        phone TEXT UNIQUE NOT NULL,
        name TEXT DEFAULT '',
        verified INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS couriers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        phone TEXT DEFAULT '',
        name TEXT DEFAULT '',
        photo_file_id TEXT DEFAULT '',
        approved INTEGER DEFAULT 0,
        online INTEGER DEFAULT 0,
        lat REAL,
        lon REAL,
        location_time TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        courier_id INTEGER,
        address TEXT DEFAULT '',
        comment TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        customer_confirmed INTEGER DEFAULT 0,
        courier_confirmed INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS support (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        message TEXT NOT NULL,
        answer TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    connection.commit()
    connection.close()


init_db()


# =========================================================
# HELPERS
# =========================================================

def json_error(message, status=400):
    return JSONResponse(
        {"error": message},
        status_code=status
    )


def telegram_user(request: Request):

    init_data = request.headers.get(
        "X-Telegram-Init-Data",
        ""
    )

    if not init_data:
        return None

    try:

        parsed = dict(
            parse_qsl(
                init_data,
                keep_blank_values=True
            )
        )

        received_hash = parsed.pop(
            "hash",
            None
        )

        if not received_hash:
            return None

        auth_date = int(
            parsed.get(
                "auth_date",
                "0"
            )
        )

        # initData не старше 24 часов
        if time.time() - auth_date > 86400:
            return None

        data_check_string = "\n".join(
            f"{key}={parsed[key]}"
            for key in sorted(parsed)
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

        return json.loads(
            parsed.get("user", "{}")
        )

    except Exception:
        return None


async def require_user(request):

    user = telegram_user(request)

    if not user:
        return None

    return user


async def broadcast(data):

    dead = []

    for ws in list(connections):

        try:
            await ws.send_json(data)

        except Exception:
            dead.append(ws)

    for ws in dead:
        connections.discard(ws)


# =========================================================
# WEB APP
# =========================================================

@app.get("/", response_class=HTMLResponse)
async def index():

    return INDEX_FILE.read_text(
        encoding="utf-8"
    )


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }


@app.websocket("/ws")
async def websocket(ws: WebSocket):

    await ws.accept()

    connections.add(ws)

    try:

        while True:
            await ws.receive_text()

    except WebSocketDisconnect:
        connections.discard(ws)

    except Exception:
        connections.discard(ws)


# =========================================================
# TELEGRAM /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    connection = db()

    connection.execute("""
        INSERT INTO users(
            telegram_id,
            username,
            first_name
        )
        VALUES (?, ?, ?)

        ON CONFLICT(telegram_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or ""
    ))

    connection.commit()
    connection.close()

    keyboard = [
        [
            KeyboardButton(
                "🍔 Открыть приложение",
                web_app=WebAppInfo(
                    url=WEB_APP_URL
                )
            )
        ],
        [
            KeyboardButton(
                "🚴 Стать курьером"
            )
        ],
        [
            KeyboardButton(
                "💬 Поддержка"
            )
        ]
    ]

    # Админу тоже показываем приложение
    if user.id == ADMIN_ID:

        keyboard.insert(
            0,
            [
                KeyboardButton(
                    "👑 Админ-панель",
                    web_app=WebAppInfo(
                        url=WEB_APP_URL
                    )
                )
            ]
        )

    await update.message.reply_text(
        "🍔 Добро пожаловать в Delivery!\n\n"
        "Выбери нужный раздел:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


# =========================================================
# COURIER REGISTRATION
# =========================================================

async def courier_registration(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data[
        "courier_registration"
    ] = True

    keyboard = ReplyKeyboardMarkup(
        [[
            KeyboardButton(
                "📱 Отправить номер",
                request_contact=True
            )
        ]],
        resize_keyboard=True
    )

    await update.message.reply_text(
        "🚴 Регистрация курьера\n\n"
        "Для начала отправь свой номер телефона.",
        reply_markup=keyboard
    )


async def contact_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    contact = update.message.contact
    user = update.effective_user

    if not contact or not user:
        return

    if not context.user_data.get(
        "courier_registration"
    ):
        return

    phone = contact.phone_number

    connection = db()

    connection.execute("""
        INSERT INTO couriers(
            telegram_id,
            phone,
            name
        )
        VALUES (?, ?, ?)

        ON CONFLICT(telegram_id)
        DO UPDATE SET
            phone=excluded.phone,
            name=excluded.name
    """, (
        user.id,
        phone,
        user.first_name or ""
    ))

    connection.execute("""
        UPDATE users
        SET role='courier'
        WHERE telegram_id=?
    """, (
        user.id,
    ))

    connection.commit()
    connection.close()

    context.user_data[
        "courier_registration"
    ] = False

    context.user_data[
        "waiting_courier_photo"
    ] = True

    await update.message.reply_text(
        "✅ Номер получен.\n\n"
        "Теперь отправь свою фотографию "
        "обычным сообщением.",
        reply_markup=ReplyKeyboardRemove()
    )


# =========================================================
# COURIER PHOTO
# =========================================================

async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    if not context.user_data.get(
        "waiting_courier_photo"
    ):
        return

    if not update.message.photo:
        return

    photo = update.message.photo[-1]

    connection = db()

    connection.execute("""
        UPDATE couriers
        SET photo_file_id=?
        WHERE telegram_id=?
    """, (
        photo.file_id,
        user.id
    ))

    connection.commit()
    connection.close()

    context.user_data[
        "waiting_courier_photo"
    ] = False

    await update.message.reply_text(
        "📸 Фото сохранено.\n\n"
        "Теперь отправь свою геопозицию.\n\n"
        "Лучше использовать Telegram "
        "Live Location, чтобы координаты "
        "обновлялись автоматически."
    )


# =========================================================
# LOCATION
# =========================================================

async def save_location(
    telegram_id,
    latitude,
    longitude
):

    connection = db()

    courier = connection.execute("""
        SELECT *
        FROM couriers
        WHERE telegram_id=?
    """, (
        telegram_id,
    )).fetchone()

    if not courier:

        connection.close()
        return

    connection.execute("""
        UPDATE couriers
        SET
            lat=?,
            lon=?,
            online=1,
            location_time=CURRENT_TIMESTAMP
        WHERE telegram_id=?
    """, (
        latitude,
        longitude,
        telegram_id
    ))

    connection.commit()
    connection.close()

    await broadcast({
        "type": "courier_location",
        "courier_id": courier["id"],
        "lat": latitude,
        "lon": longitude
    })


async def location_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    location = update.effective_message.location
    user = update.effective_user

    if not location or not user:
        return

    await save_location(
        user.id,
        location.latitude,
        location.longitude
    )


async def edited_location_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.edited_message

    if not message:
        return

    if not message.location:
        return

    user = message.from_user

    if not user:
        return

    await save_location(
        user.id,
        message.location.latitude,
        message.location.longitude
    )


# =========================================================
# BOT TEXT
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    text = update.message.text

    if text == "🚴 Стать курьером":

        await courier_registration(
            update,
            context
        )

        return

    if text == "💬 Поддержка":

        context.user_data[
            "support"
        ] = True

        await update.message.reply_text(
            "💬 Напиши сообщение для поддержки."
        )

        return

    if context.user_data.get(
        "support"
    ):

        connection = db()

        connection.execute("""
            INSERT INTO support(
                telegram_id,
                message
            )
            VALUES (?, ?)
        """, (
            update.effective_user.id,
            text
        ))

        connection.commit()
        connection.close()

        context.user_data[
            "support"
        ] = False

        await update.message.reply_text(
            "✅ Сообщение отправлено."
        )


# =========================================================
# ME
# =========================================================

@app.get("/api/me")
async def me(request: Request):

    user = await require_user(request)

    if not user:
        return json_error(
            "Unauthorized. Открой приложение через Telegram.",
            401
        )

    telegram_id = int(user["id"])

    connection = db()

    customer = connection.execute("""
        SELECT *
        FROM customers
        WHERE telegram_id=?
    """, (
        telegram_id,
    )).fetchone()

    courier = connection.execute("""
        SELECT *
        FROM couriers
        WHERE telegram_id=?
    """, (
        telegram_id,
    )).fetchone()

    connection.close()

    if telegram_id == ADMIN_ID:
        role = "admin"

    elif courier:
        role = "courier"

    else:
        role = "customer"

    return {
        "telegram_id": telegram_id,
        "name": user.get(
            "first_name",
            ""
        ),
        "username": user.get(
            "username",
            ""
        ),
        "role": role,
        "customer": (
            dict(customer)
            if customer else None
        ),
        "courier": (
            dict(courier)
            if courier else None
        )
    }


# =========================================================
# CUSTOMER LOGIN
# =========================================================

@app.post("/api/login")
async def login(request: Request):

    user = await require_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    data = await request.json()

    phone = str(
        data.get("phone", "")
    ).strip()

    if not phone:
        return json_error(
            "Введите номер телефона"
        )

    telegram_id = int(user["id"])

    connection = db()

    customer = connection.execute("""
        SELECT *
        FROM customers
        WHERE phone=?
    """, (
        phone,
    )).fetchone()

    if not customer:

        connection.close()

        return json_error(
            "Этот номер не добавлен администратором.",
            404
        )

    if (
        customer["telegram_id"]
        and customer["telegram_id"] != telegram_id
    ):

        connection.close()

        return json_error(
            "Номер уже привязан к другому Telegram.",
            409
        )

    connection.execute("""
        UPDATE customers
        SET telegram_id=?
        WHERE id=?
    """, (
        telegram_id,
        customer["id"]
    ))

    connection.commit()
    connection.close()

    return {
        "ok": True
    }


# =========================================================
# CUSTOMER ORDERS
# =========================================================

@app.get("/api/orders")
async def customer_orders(
    request: Request
):

    user = await require_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    connection = db()

    customer = connection.execute("""
        SELECT *
        FROM customers
        WHERE telegram_id=?
    """, (
        int(user["id"]),
    )).fetchone()

    if not customer:

        connection.close()

        return {
            "orders": []
        }

    rows = connection.execute("""
        SELECT
            o.*,

            cr.name AS courier_name,
            cr.lat AS courier_lat,
            cr.lon AS courier_lon,
            cr.online AS courier_online

        FROM orders o

        LEFT JOIN couriers cr
            ON o.courier_id=cr.id

        WHERE o.customer_id=?

        AND o.status!='closed'

        ORDER BY o.id DESC
    """, (
        customer["id"],
    )).fetchall()

    connection.close()

    return {
        "orders": [
            dict(row)
            for row in rows
        ]
    }


# =========================================================
# COURIER ORDERS
# =========================================================

@app.get("/api/courier/orders")
async def courier_orders(
    request: Request
):

    user = await require_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    connection = db()

    courier = connection.execute("""
        SELECT *
        FROM couriers
        WHERE telegram_id=?
    """, (
        int(user["id"]),
    )).fetchone()

    if not courier:

        connection.close()

        return {
            "orders": []
        }

    rows = connection.execute("""
        SELECT
            o.*,
            c.name AS customer_name,
            c.phone AS customer_phone

        FROM orders o

        JOIN customers c
            ON o.customer_id=c.id

        WHERE o.courier_id=?

        AND o.status!='closed'

        ORDER BY o.id DESC
    """, (
        courier["id"],
    )).fetchall()

    connection.close()

    return {
        "orders": [
            dict(row)
            for row in rows
        ]
    }


# =========================================================
# COURIER ONLINE
# =========================================================

@app.post("/api/courier/online")
async def courier_online(
    request: Request
):

    user = await require_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    data = await request.json()

    online = 1 if data.get(
        "online"
    ) else 0

    connection = db()

    courier = connection.execute("""
        SELECT *
        FROM couriers
        WHERE telegram_id=?
    """, (
        int(user["id"]),
    )).fetchone()

    if not courier:

        connection.close()

        return json_error(
            "Курьер не найден",
            404
        )

    if not courier["approved"]:

        connection.close()

        return json_error(
            "Курьер ещё не подтверждён администратором",
            403
        )

    connection.execute("""
        UPDATE couriers
        SET online=?
        WHERE telegram_id=?
    """, (
        online,
        int(user["id"])
    ))

    connection.commit()
    connection.close()

    await broadcast({
        "type": "courier_status",
        "courier_id": courier["id"],
        "online": bool(online)
    })

    return {
        "ok": True
    }


# =========================================================
# COURIER CONFIRM ORDER
# =========================================================

@app.post(
    "/api/courier/orders/{order_id}/accept"
)
async def accept_order(
    order_id: int,
    request: Request
):

    user = await require_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    connection = db()

    courier = connection.execute("""
        SELECT *
        FROM couriers
        WHERE telegram_id=?
    """, (
        int(user["id"]),
    )).fetchone()

    if not courier:

        connection.close()

        return json_error(
            "Courier not found",
            404
        )

    if not courier["approved"]:

        connection.close()

        return json_error(
            "Курьер не подтверждён",
            403
        )

    order = connection.execute("""
        SELECT *
        FROM orders
        WHERE id=?
        AND courier_id=?
    """, (
        order_id,
        courier["id"]
    )).fetchone()

    if not order:

        connection.close()

        return json_error(
            "Заказ не найден",
            404
        )

    connection.execute("""
        UPDATE orders
        SET
            courier_confirmed=1,
            status='accepted',
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (
        order_id,
    ))

    connection.commit()
    connection.close()

    await broadcast({
        "type": "order_update",
        "order_id": order_id,
        "status": "accepted"
    })

    return {
        "ok": True
    }


# =========================================================
# COURIER START DELIVERY
# =========================================================

@app.post(
    "/api/courier/orders/{order_id}/start"
)
async def start_delivery(
    order_id: int,
    request: Request
):

    user = await require_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    connection = db()

    courier = connection.execute("""
        SELECT id
        FROM couriers
        WHERE telegram_id=?
    """, (
        int(user["id"]),
    )).fetchone()

    if not courier:

        connection.close()

        return json_error(
            "Courier not found",
            404
        )

    connection.execute("""
        UPDATE orders
        SET
            status='delivering',
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        AND courier_id=?
        AND courier_confirmed=1
    """, (
        order_id,
        courier["id"]
    ))

    connection.commit()
    connection.close()

    await broadcast({
        "type": "order_update",
        "order_id": order_id,
        "status": "delivering"
    })

    return {
        "ok": True
    }


# =========================================================
# COMPLETE DELIVERY
# =========================================================

@app.post(
    "/api/courier/orders/{order_id}/complete"
)
async def complete_order(
    order_id: int,
    request: Request
):

    user = await require_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    connection = db()

    courier = connection.execute("""
        SELECT id
        FROM couriers
        WHERE telegram_id=?
    """, (
        int(user["id"]),
    )).fetchone()

    if not courier:

        connection.close()

        return json_error(
            "Courier not found",
            404
        )

    order = connection.execute("""
        SELECT *
        FROM orders
        WHERE id=?
        AND courier_id=?
    """, (
        order_id,
        courier["id"]
    )).fetchone()

    if not order:

        connection.close()

        return json_error(
            "Order not found",
            404
        )

    connection.execute("""
        UPDATE orders
        SET
            status='delivered',
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (
        order_id,
    ))

    connection.commit()
    connection.close()

    await broadcast({
        "type": "order_update",
        "order_id": order_id,
        "status": "delivered"
    })

    return {
        "ok": True
    }


# =========================================================
# CUSTOMER CONFIRM RECEIVED
# =========================================================

@app.post(
    "/api/orders/{order_id}/confirm"
)
async def customer_confirm(
    order_id: int,
    request: Request
):

    user = await require_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    connection = db()

    customer = connection.execute("""
        SELECT id
        FROM customers
        WHERE telegram_id=?
    """, (
        int(user["id"]),
    )).fetchone()

    if not customer:

        connection.close()

        return json_error(
            "Customer not found",
            404
        )

    order = connection.execute("""
        SELECT *
        FROM orders
        WHERE id=?
        AND customer_id=?
    """, (
        order_id,
        customer["id"]
    )).fetchone()

    if not order:

        connection.close()

        return json_error(
            "Заказ не найден",
            404
        )

    if order["status"] != "delivered":

        connection.close()

        return json_error(
            "Курьер ещё не отметил заказ доставленным",
            400
        )

    connection.execute("""
        UPDATE orders
        SET
            customer_confirmed=1,
            status='closed',
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (
        order_id,
    ))

    connection.commit()
    connection.close()

    await broadcast({
        "type": "order_update",
        "order_id": order_id,
        "status": "closed"
    })

    return {
        "ok": True
    }


# =========================================================
# ADMIN STATS
# =========================================================

@app.get("/api/admin/stats")
async def admin_stats(
    request: Request
):

    user = await require_user(request)

    if not user or int(user["id"]) != ADMIN_ID:
        return json_error(
            "Forbidden",
            403
        )

    connection = db()

    customers = connection.execute(
        "SELECT COUNT(*) FROM customers"
    ).fetchone()[0]

    couriers = connection.execute(
        "SELECT COUNT(*) FROM couriers"
    ).fetchone()[0]

    pending = connection.execute("""
        SELECT COUNT(*)
        FROM couriers
        WHERE approved=0
    """).fetchone()[0]

    online = connection.execute("""
        SELECT COUNT(*)
        FROM couriers
        WHERE online=1
        AND approved=1
    """).fetchone()[0]

    active = connection.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status!='closed'
    """).fetchone()[0]

    closed = connection.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status='closed'
    """).fetchone()[0]

    connection.close()

    return {
        "customers": customers,
        "couriers": couriers,
        "pending_couriers": pending,
        "online": online,
        "active_orders": active,
        "closed_orders": closed
    }


# =========================================================
# ADMIN CUSTOMERS
# =========================================================

@app.get("/api/admin/customers")
async def admin_customers(
    request: Request
):

    user = await require_user(request)

    if not user or int(user["id"]) != ADMIN_ID:
        return json_error(
            "Forbidden",
            403
        )

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM customers
        ORDER BY id DESC
    """).fetchall()

    connection.close()

    return {
        "customers": [
            dict(x)
            for x in rows
        ]
    }


@app.post("/api/admin/customers")
async def admin_create_customer(
    request: Request
):

    user = await require_user(request)

    if not user or int(user["id"]) != ADMIN_ID:
        return json_error(
            "Forbidden",
            403
        )

    data = await request.json()

    name = str(
        data.get("name", "")
    ).strip()

    phone = str(
        data.get("phone", "")
    ).strip()

    if not phone:
        return json_error(
            "Введите номер"
        )

    connection = db()

    try:

        connection.execute("""
            INSERT INTO customers(
                name,
                phone
            )
            VALUES (?, ?)
        """, (
            name,
            phone
        ))

        connection.commit()

    except sqlite3.IntegrityError:

        connection.close()

        return json_error(
            "Такой номер уже существует",
            409
        )

    connection.close()

    return {
        "ok": True
    }


# =========================================================
# ADMIN COURIERS
# =========================================================

@app.get("/api/admin/couriers")
async def admin_couriers(
    request: Request
):

    user = await require_user(request)

    if not user or int(user["id"]) != ADMIN_ID:
        return json_error(
            "Forbidden",
            403
        )

    connection = db()

    rows = connection.execute("""
        SELECT *
        FROM couriers
        ORDER BY approved ASC, id DESC
    """).fetchall()

    connection.close()

    return {
        "couriers": [
            dict(x)
            for x in rows
        ]
    }


@app.post(
    "/api/admin/couriers/{telegram_id}/approve"
)
async def approve_courier(
    telegram_id: int,
    request: Request
):

    user = await require_user(request)

    if not user or int(user["id"]) != ADMIN_ID:
        return json_error(
            "Forbidden",
            403
        )

    connection = db()

    connection.execute("""
        UPDATE couriers
        SET approved=1
        WHERE telegram_id=?
    """, (
        telegram_id,
    ))

    connection.commit()
    connection.close()

    try:

        bot = context_bot

        await bot.send_message(
            chat_id=telegram_id,
            text=(
                "🎉 Твоя заявка курьера одобрена!\n\n"
                "Теперь ты можешь открыть приложение "
                "и принимать заказы."
            )
        )

    except Exception:
        pass

    return {
        "ok": True
    }


# =========================================================
# ADMIN ORDERS
# =========================================================

@app.get("/api/admin/orders")
async def admin_orders(
    request: Request
):

    user = await require_user(request)

    if not user or int(user["id"]) != ADMIN_ID:
        return json_error(
            "Forbidden",
            403
        )

    connection = db()

    rows = connection.execute("""
        SELECT
            o.*,
            c.name AS customer_name,
            c.phone AS customer_phone,
            cr.name AS courier_name

        FROM orders o

        JOIN customers c
            ON o.customer_id=c.id

        LEFT JOIN couriers cr
            ON o.courier_id=cr.id

        ORDER BY o.id DESC
    """).fetchall()

    connection.close()

    return {
        "orders": [
            dict(x)
            for x in rows
        ]
    }


@app.post("/api/admin/orders")
async def admin_create_order(
    request: Request
):

    user = await require_user(request)

    if not user or int(user["id"]) != ADMIN_ID:
        return json_error(
            "Forbidden",
            403
        )

    data = await request.json()

    try:
        customer_id = int(
            data["customer_id"]
        )
    except Exception:
        return json_error(
            "Выбери клиента"
        )

    courier_id = data.get(
        "courier_id"
    )

    if courier_id:
        courier_id = int(courier_id)

    address = str(
        data.get("address", "")
    ).strip()

    comment = str(
        data.get("comment", "")
    ).strip()

    connection = db()

    customer = connection.execute("""
        SELECT *
        FROM customers
        WHERE id=?
    """, (
        customer_id,
    )).fetchone()

    if not customer:

        connection.close()

        return json_error(
            "Клиент не найден"
        )

    if courier_id:

        courier = connection.execute("""
            SELECT *
            FROM couriers
            WHERE id=?
            AND approved=1
        """, (
            courier_id,
        )).fetchone()

        if not courier:

            connection.close()

            return json_error(
                "Курьер не найден или не подтверждён"
            )

    connection.execute("""
        INSERT INTO orders(
            customer_id,
            courier_id,
            address,
            comment,
            status
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        customer_id,
        courier_id,
        address,
        comment,
        "assigned" if courier_id else "new"
    ))

    order_id = connection.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    connection.commit()
    connection.close()

    await broadcast({
        "type": "new_order",
        "order_id": order_id
    })

    return {
        "ok": True,
        "order_id": order_id
    }


# =========================================================
# BOT INSTANCE
# =========================================================

context_bot = None


async def run_bot():

    global context_bot

    bot_app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    bot_app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    bot_app.add_handler(
        CommandHandler(
            "courier",
            courier_registration
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.CONTACT,
            contact_handler
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.LOCATION,
            location_handler
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    # Отдельный обработчик для изменения Live Location
    from telegram.ext import TypeHandler

    bot_app.add_handler(
        TypeHandler(
            Update,
            edited_location_handler
        )
    )

    await bot_app.initialize()

    await bot_app.start()

    await bot_app.updater.start_polling()

    context_bot = bot_app.bot

    try:

        while True:
            await asyncio.sleep(3600)

    finally:

        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


# =========================================================
# SERVER
# =========================================================

async def run_server():

    import uvicorn

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        )
    )

    server = uvicorn.Server(config)

    await server.serve()


async def main():

    await asyncio.gather(
        run_server(),
        run_bot()
    )


if __name__ == "__main__":
    asyncio.run(main())
