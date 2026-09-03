import os
import json
import hmac
import hashlib
import sqlite3
import asyncio
import time
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
    TypeHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8357023784"))

# Render автоматически передаёт эту переменную.
# НИКАКОЙ Render URL сюда вписывать не нужно.
WEB_APP_URL = os.getenv("RENDER_EXTERNAL_URL")

if not WEB_APP_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is not available")

DB_PATH = "delivery.db"

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

app = FastAPI()

websockets = set()


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.executescript("""
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
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS couriers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        phone TEXT DEFAULT '',
        name TEXT DEFAULT '',
        approved INTEGER DEFAULT 0,
        online INTEGER DEFAULT 0,
        lat REAL,
        lon REAL,
        location_time TEXT
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        courier_id INTEGER,
        address TEXT DEFAULT '',
        comment TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS closed_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        customer_id INTEGER,
        courier_id INTEGER,
        closed_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS support (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        message TEXT NOT NULL,
        answer TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    conn.commit()
    conn.close()


init_db()


# =========================================================
# TELEGRAM WEB APP AUTH
# =========================================================

def validate_init_data(init_data: str):

    if not init_data:
        return None

    try:
        data = dict(parse_qsl(
            init_data,
            keep_blank_values=True
        ))

        received_hash = data.pop("hash", None)

        if not received_hash:
            return None

        auth_date = int(data.get("auth_date", "0"))

        if time.time() - auth_date > 86400:
            return None

        data_check_string = "\n".join(
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
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return None

        return json.loads(
            data.get("user", "{}")
        )

    except Exception:
        return None


async def get_user(request: Request):

    init_data = request.headers.get(
        "X-Telegram-Init-Data",
        ""
    )

    return validate_init_data(init_data)


# =========================================================
# WEBSOCKET
# =========================================================

async def broadcast(data):

    dead = []

    for ws in websockets:

        try:
            await ws.send_json(data)

        except Exception:
            dead.append(ws)

    for ws in dead:
        websockets.discard(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):

    await ws.accept()
    websockets.add(ws)

    try:

        while True:
            await ws.receive_text()

    except WebSocketDisconnect:
        websockets.discard(ws)

    except Exception:
        websockets.discard(ws)


# =========================================================
# TELEGRAM BOT
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    conn = get_db()

    conn.execute("""
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

    conn.commit()
    conn.close()

    keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "🍔 Открыть приложение",
                    web_app=WebAppInfo(
                        url=WEB_APP_URL
                    )
                )
            ],
            [
                KeyboardButton("🚴 Стать курьером")
            ],
            [
                KeyboardButton("💬 Поддержка")
            ]
        ],
        resize_keyboard=True
    )

    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n\n"
        "Добро пожаловать в Delivery.",
        reply_markup=keyboard
    )


# =========================================================
# COURIER REGISTRATION
# =========================================================

async def courier_start(
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

    await update.message.reply_text(
        "🚴 Регистрация курьера\n\n"
        "Нажми кнопку и отправь номер телефона.",
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

    conn = get_db()

    conn.execute("""
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
        contact.phone_number,
        user.first_name or ""
    ))

    conn.execute("""
        UPDATE users
        SET role='courier'
        WHERE telegram_id=?
    """, (user.id,))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ Заявка отправлена администратору.\n\n"
        "После регистрации отправь этому боту "
        "свою геопозицию.",
        reply_markup=ReplyKeyboardRemove()
    )


# =========================================================
# COURIER LOCATION
# =========================================================

async def save_location(
    telegram_id,
    latitude,
    longitude
):

    conn = get_db()

    courier = conn.execute("""
        SELECT id, name, approved
        FROM couriers
        WHERE telegram_id=?
    """, (telegram_id,)).fetchone()

    if not courier:
        conn.close()
        return

    conn.execute("""
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

    conn.commit()
    conn.close()

    await broadcast({
        "type": "courier_location",
        "courier_id": courier["id"],
        "name": courier["name"],
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

    conn = get_db()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE telegram_id=?
    """, (user.id,)).fetchone()

    conn.close()

    if not courier:

        await update.effective_message.reply_text(
            "❌ Сначала зарегистрируйся как курьер."
        )

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

    if not message or not message.location:
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
        await courier_start(update, context)
        return

    if text == "💬 Поддержка":

        await update.message.reply_text(
            "💬 Напиши сообщение для поддержки."
        )

        context.user_data["support"] = True
        return

    if text == "🍔 Открыть приложение":
        return

    user = update.effective_user

    if not user:
        return

    if context.user_data.get("support"):

        conn = get_db()

        conn.execute("""
            INSERT INTO support(
                telegram_id,
                message
            )
            VALUES (?, ?)
        """, (
            user.id,
            text
        ))

        conn.commit()
        conn.close()

        context.user_data["support"] = False

        await update.message.reply_text(
            "✅ Сообщение отправлено."
        )


# =========================================================
# API
# =========================================================

@app.get("/health")
async def health():

    return {
        "status": "ok"
    }


@app.get("/", response_class=HTMLResponse)
async def homepage():

    return INDEX_FILE.read_text(
        encoding="utf-8"
    )


@app.get("/api/me")
async def api_me(request: Request):

    user = await get_user(request)

    if not user:
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401
        )

    telegram_id = int(user["id"])

    conn = get_db()

    customer = conn.execute("""
        SELECT *
        FROM customers
        WHERE telegram_id=?
    """, (telegram_id,)).fetchone()

    courier = conn.execute("""
        SELECT *
        FROM couriers
        WHERE telegram_id=?
    """, (telegram_id,)).fetchone()

    conn.close()

    if telegram_id == ADMIN_ID:
        role = "admin"

    elif courier:
        role = "courier"

    else:
        role = "customer"

    return {
        "telegram_id": telegram_id,
        "name": user.get("first_name", ""),
        "username": user.get("username", ""),
        "role": role,
        "customer": dict(customer)
        if customer else None,
        "courier": dict(courier)
        if courier else None
    }


# =========================================================
# CUSTOMER LOGIN
# =========================================================

@app.post("/api/login")
async def login(request: Request):

    user = await get_user(request)

    if not user:
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401
        )

    data = await request.json()

    phone = str(
        data.get("phone", "")
    ).strip()

    if not phone:
        return JSONResponse(
            {"error": "Введите номер телефона"},
            status_code=400
        )

    telegram_id = int(user["id"])

    conn = get_db()

    customer = conn.execute("""
        SELECT *
        FROM customers
        WHERE phone=?
    """, (phone,)).fetchone()

    if not customer:

        conn.close()

        return JSONResponse(
            {
                "error":
                "Этот номер не зарегистрирован."
            },
            status_code=404
        )

    if (
        customer["telegram_id"] is not None
        and customer["telegram_id"] != telegram_id
    ):

        conn.close()

        return JSONResponse(
            {
                "error":
                "Номер уже привязан к другому аккаунту."
            },
            status_code=409
        )

    conn.execute("""
        UPDATE customers
        SET telegram_id=?
        WHERE id=?
    """, (
        telegram_id,
        customer["id"]
    ))

    conn.commit()
    conn.close()

    return {
        "ok": True
    }


# =========================================================
# CUSTOMER ORDERS
# =========================================================

@app.get("/api/orders")
async def orders(request: Request):

    user = await get_user(request)

    if not user:
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401
        )

    telegram_id = int(user["id"])

    conn = get_db()

    customer = conn.execute("""
        SELECT id
        FROM customers
        WHERE telegram_id=?
    """, (telegram_id,)).fetchone()

    if not customer:
        conn.close()
        return {"orders": []}

    rows = conn.execute("""
        SELECT
            o.*,
            c.name AS courier_name,
            c.lat AS courier_lat,
            c.lon AS courier_lon,
            c.online AS courier_online
        FROM orders o
        LEFT JOIN couriers c
            ON o.courier_id=c.id
        WHERE o.customer_id=?
        AND o.status!='closed'
        ORDER BY o.id DESC
    """, (
        customer["id"],
    )).fetchall()

    conn.close()

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
async def courier_online(request: Request):

    user = await get_user(request)

    if not user:
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401
        )

    data = await request.json()

    online = 1 if data.get("online") else 0

    conn = get_db()

    conn.execute("""
        UPDATE couriers
        SET online=?
        WHERE telegram_id=?
    """, (
        online,
        int(user["id"])
    ))

    conn.commit()
    conn.close()

    await broadcast({
        "type": "courier_status",
        "telegram_id": int(user["id"]),
        "online": bool(online)
    })

    return {
        "ok": True
    }


# =========================================================
# COURIER ORDERS
# =========================================================

@app.get("/api/courier/orders")
async def courier_orders(request: Request):

    user = await get_user(request)

    if not user:
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401
        )

    conn = get_db()

    courier = conn.execute("""
        SELECT id
        FROM couriers
        WHERE telegram_id=?
    """, (
        int(user["id"]),
    )).fetchone()

    if not courier:
        conn.close()
        return {"orders": []}

    rows = conn.execute("""
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

    conn.close()

    return {
        "orders": [
            dict(row)
            for row in rows
        ]
    }


@app.post("/api/courier/orders/{order_id}/complete")
async def complete_order(
    order_id: int,
    request: Request
):

    user = await get_user(request)

    if not user:
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401
        )

    conn = get_db()

    courier = conn.execute("""
        SELECT id
        FROM couriers
        WHERE telegram_id=?
    """, (
        int(user["id"]),
    )).fetchone()

    if not courier:
        conn.close()

        return JSONResponse(
            {"error": "Courier not found"},
            status_code=404
        )

    order = conn.execute("""
        SELECT *
        FROM orders
        WHERE id=?
        AND courier_id=?
    """, (
        order_id,
        courier["id"]
    )).fetchone()

    if not order:
        conn.close()

        return JSONResponse(
            {"error": "Order not found"},
            status_code=404
        )

    conn.execute("""
        UPDATE orders
        SET
            status='closed',
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (
        order_id,
    ))

    conn.execute("""
        INSERT INTO closed_orders(
            order_id,
            customer_id,
            courier_id
        )
        VALUES (?, ?, ?)
    """, (
        order["id"],
        order["customer_id"],
        order["courier_id"]
    ))

    conn.commit()
    conn.close()

    await broadcast({
        "type": "order_update",
        "order_id": order_id,
        "status": "closed"
    })

    return {
        "ok": True
    }


# =========================================================
# ADMIN HELPERS
# =========================================================

def is_admin(user):

    return (
        user
        and int(user["id"]) == ADMIN_ID
    )


# =========================================================
# ADMIN STATS
# =========================================================

@app.get("/api/admin/stats")
async def admin_stats(request: Request):

    user = await get_user(request)

    if not is_admin(user):
        return JSONResponse(
            {"error": "Forbidden"},
            status_code=403
        )

    conn = get_db()

    customers = conn.execute(
        "SELECT COUNT(*) FROM customers"
    ).fetchone()[0]

    couriers = conn.execute(
        "SELECT COUNT(*) FROM couriers"
    ).fetchone()[0]

    online = conn.execute("""
        SELECT COUNT(*)
        FROM couriers
        WHERE online=1
    """).fetchone()[0]

    active_orders = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status!='closed'
    """).fetchone()[0]

    closed = conn.execute(
        "SELECT COUNT(*) FROM closed_orders"
    ).fetchone()[0]

    conn.close()

    return {
        "customers": customers,
        "couriers": couriers,
        "online": online,
        "active_orders": active_orders,
        "closed_orders": closed
    }


# =========================================================
# ADMIN COURIERS
# =========================================================

@app.get("/api/admin/couriers")
async def admin_couriers(request: Request):

    user = await get_user(request)

    if not is_admin(user):
        return JSONResponse(
            {"error": "Forbidden"},
            status_code=403
        )

    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM couriers
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return {
        "couriers": [
            dict(row)
            for row in rows
        ]
    }


@app.post(
    "/api/admin/couriers/{telegram_id}/approve"
)
async def approve_courier(
    telegram_id: int,
    request: Request
):

    user = await get_user(request)

    if not is_admin(user):
        return JSONResponse(
            {"error": "Forbidden"},
            status_code=403
        )

    conn = get_db()

    conn.execute("""
        UPDATE couriers
        SET approved=1
        WHERE telegram_id=?
    """, (
        telegram_id,
    ))

    conn.commit()
    conn.close()

    return {
        "ok": True
    }


# =========================================================
# ADMIN CUSTOMERS
# =========================================================

@app.get("/api/admin/customers")
async def admin_customers(request: Request):

    user = await get_user(request)

    if not is_admin(user):
        return JSONResponse(
            {"error": "Forbidden"},
            status_code=403
        )

    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM customers
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return {
        "customers": [
            dict(row)
            for row in rows
        ]
    }


@app.post("/api/admin/customers")
async def create_customer(request: Request):

    user = await get_user(request)

    if not is_admin(user):
        return JSONResponse(
            {"error": "Forbidden"},
            status_code=403
        )

    data = await request.json()

    name = str(
        data.get("name", "")
    ).strip()

    phone = str(
        data.get("phone", "")
    ).strip()

    if not phone:
        return JSONResponse(
            {"error": "Введите номер"},
            status_code=400
        )

    conn = get_db()

    try:

        conn.execute("""
            INSERT INTO customers(
                name,
                phone
            )
            VALUES (?, ?)
        """, (
            name,
            phone
        ))

        conn.commit()

    except sqlite3.IntegrityError:

        conn.close()

        return JSONResponse(
            {"error": "Такой номер уже существует"},
            status_code=409
        )

    conn.close()

    return {
        "ok": True
    }


# =========================================================
# ADMIN ORDERS
# =========================================================

@app.get("/api/admin/orders")
async def admin_orders(request: Request):

    user = await get_user(request)

    if not is_admin(user):
        return JSONResponse(
            {"error": "Forbidden"},
            status_code=403
        )

    conn = get_db()

    rows = conn.execute("""
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

    conn.close()

    return {
        "orders": [
            dict(row)
            for row in rows
        ]
    }


@app.post("/api/admin/orders")
async def create_order(request: Request):

    user = await get_user(request)

    if not is_admin(user):
        return JSONResponse(
            {"error": "Forbidden"},
            status_code=403
        )

    data = await request.json()

    customer_id = int(
        data["customer_id"]
    )

    courier_id = data.get(
        "courier_id"
    )

    address = str(
        data.get("address", "")
    )

    comment = str(
        data.get("comment", "")
    )

    conn = get_db()

    conn.execute("""
        INSERT INTO orders(
            customer_id,
            courier_id,
            address,
            comment
        )
        VALUES (?, ?, ?, ?)
    """, (
        customer_id,
        courier_id,
        address,
        comment
    ))

    conn.commit()
    conn.close()

    await broadcast({
        "type": "new_order"
    })

    return {
        "ok": True
    }


# =========================================================
# START BOT + SERVER
# =========================================================

async def run_bot():

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
            courier_start
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
            filters.LOCATION,
            location_handler
        )
    )

    bot_app.add_handler(
        TypeHandler(
            Update,
            edited_location_handler
        )
    )

    bot_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    try:

        while True:
            await asyncio.sleep(3600)

    finally:

        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


async def run_server():

    import uvicorn

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "10000")
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