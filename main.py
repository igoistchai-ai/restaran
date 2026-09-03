# main.py
import os, re, json, time, hmac, hashlib, secrets, sqlite3, asyncio, threading
from urllib.parse import parse_qsl
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Header, Request, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import uvicorn

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEB_APP_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
ADMIN_IDS = {8357023784, 7003441441}

DB_PATH = "/data/restaran.db" if os.path.isdir("/data") else "restaran.db"

app = FastAPI(title="RESTARAN")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            pin_hash TEXT NOT NULL,
            pin_plain TEXT,
            role TEXT NOT NULL,
            telegram_id INTEGER,
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
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS orders (
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
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        """)

        def add_col(table, col, definition):
            cols = [x["name"] for x in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")

        add_col("users", "pin_plain", "TEXT")
        add_col("users", "telegram_id", "INTEGER")
        add_col("users", "active", "INTEGER NOT NULL DEFAULT 1")

        add_col("couriers", "approved", "INTEGER NOT NULL DEFAULT 0")
        add_col("couriers", "online", "INTEGER NOT NULL DEFAULT 0")
        add_col("couriers", "lat", "REAL")
        add_col("couriers", "lon", "REAL")
        add_col("couriers", "updated_at", "INTEGER")
        add_col("couriers", "active", "INTEGER NOT NULL DEFAULT 1")

        add_col("orders", "customer_confirmed", "INTEGER NOT NULL DEFAULT 0")
        add_col("orders", "closed_at", "INTEGER")


init_db()


def norm_phone(phone):
    return re.sub(r"\D", "", phone or "")


def hash_pin(pin):
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + pin).encode()).hexdigest()
    return salt + ":" + digest


def check_pin(pin, stored):
    try:
        salt, digest = stored.split(":", 1)
        return hmac.compare_digest(
            hashlib.sha256((salt + pin).encode()).hexdigest(),
            digest
        )
    except Exception:
        return False


def new_session(user_id, role):
    token = secrets.token_urlsafe(40)
    with db() as c:
        c.execute(
            "INSERT INTO sessions(token,user_id,role,created_at) VALUES(?,?,?,?)",
            (token, user_id, role, int(time.time()))
        )
    return token


def get_auth(authorization):
    if not authorization:
        raise HTTPException(401, "Не авторизован")

    token = authorization.replace("Bearer ", "").strip()

    with db() as c:
        row = c.execute("""
            SELECT s.*, u.name, u.phone, u.active
            FROM sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.token=?
        """, (token,)).fetchone()

    if not row or not row["active"]:
        raise HTTPException(401, "Сессия недействительна")

    return row


def admin_only(auth):
    if auth["role"] != "admin":
        raise HTTPException(403, "Только для администратора")


def validate_init_data(init_data):
    if not BOT_TOKEN or not init_data:
        raise ValueError("Telegram initData отсутствует")

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = data.pop("hash", None)

    if not received_hash:
        raise ValueError("Hash отсутствует")

    check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(data.items())
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

    if not hmac.compare_digest(calculated, received_hash):
        raise ValueError("Неверный Telegram initData")

    auth_date = int(data.get("auth_date", "0"))

    if not auth_date or time.time() - auth_date > 86400:
        raise ValueError("Telegram initData устарел")

    user = json.loads(data.get("user", "{}"))

    if not user.get("id"):
        raise ValueError("Telegram user отсутствует")

    return user


def find_customer(phone):
    target = norm_phone(phone)

    with db() as c:
        rows = c.execute("""
            SELECT * FROM users
            WHERE role='customer' AND active=1
        """).fetchall()

    for row in rows:
        if norm_phone(row["phone"]) == target:
            return row

    return None


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
    pin: str


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


@app.get("/")
async def index():
    return FileResponse("index.html")


@app.head("/")
async def index_head():
    return FileResponse("index.html")


@app.post("/api/login")
async def login(data: LoginIn):
    customer = find_customer(data.phone)

    if not customer:
        with db() as c:
            courier = c.execute("""
                SELECT u.*, c.approved, c.active AS courier_active
                FROM users u
                JOIN couriers c ON c.user_id=u.id
                WHERE u.role='courier' AND u.active=1
            """).fetchall()

        customer = next(
            (x for x in courier if norm_phone(x["phone"]) == norm_phone(data.phone)),
            None
        )

    if not customer or not check_pin(data.pin, customer["pin_hash"]):
        raise HTTPException(401, "Неверный телефон или PIN")

    if customer["role"] == "courier":
        with db() as c:
            courier = c.execute(
                "SELECT approved,active FROM couriers WHERE user_id=?",
                (customer["id"],)
            ).fetchone()

        if not courier or not courier["approved"] or not courier["active"]:
            raise HTTPException(403, "Курьер не активирован")

    token = new_session(customer["id"], customer["role"])

    return {
        "token": token,
        "role": customer["role"],
        "name": customer["name"]
    }


@app.post("/api/admin/web-login")
async def admin_web_login(data: AdminWebLogin):
    try:
        tg_user = validate_init_data(data.init_data)
    except Exception as e:
        raise HTTPException(401, str(e))

    tg_id = int(tg_user["id"])

    if tg_id not in ADMIN_IDS:
        raise HTTPException(403, "Вы не администратор")

    name = (
        (tg_user.get("first_name", "") + " " + tg_user.get("last_name", "")).strip()
        or tg_user.get("username", "")
        or "Администратор"
    )

    with db() as c:
        row = c.execute(
            "SELECT * FROM users WHERE telegram_id=? AND role='admin'",
            (tg_id,)
        ).fetchone()

        if not row:
            phone = f"tg:{tg_id}"
            c.execute("""
                INSERT INTO users
                (name,phone,pin_hash,pin_plain,role,telegram_id,active,created_at)
                VALUES(?,?,?,?,?,?,1,?)
            """, (
                name,
                phone,
                hash_pin(secrets.token_hex(8)),
                "",
                "admin",
                tg_id,
                int(time.time())
            ))
            user_id = c.lastrowid
        else:
            user_id = row["id"]
            c.execute(
                "UPDATE users SET name=?,active=1 WHERE id=?",
                (name, user_id)
            )

    token = new_session(user_id, "admin")

    return {
        "token": token,
        "role": "admin",
        "name": name
    }


@app.get("/api/me")
async def me(authorization: str = Header(None)):
    auth = get_auth(authorization)

    return {
        "id": auth["user_id"],
        "name": auth["name"],
        "phone": auth["phone"],
        "role": auth["role"]
    }


@app.get("/api/admin/stats")
async def admin_stats(authorization: str = Header(None)):
    auth = get_auth(authorization)
    admin_only(auth)

    cutoff = int(time.time()) - 300

    with db() as c:
        customers = c.execute(
            "SELECT COUNT(*) n FROM users WHERE role='customer' AND active=1"
        ).fetchone()["n"]

        couriers = c.execute(
            "SELECT COUNT(*) n FROM couriers WHERE active=1 AND approved=1"
        ).fetchone()["n"]

        orders = c.execute("""
            SELECT COUNT(*) n FROM orders
            WHERE status!='closed' OR closed_at IS NULL OR closed_at>?
        """, (cutoff,)).fetchone()["n"]

        revenue = c.execute("""
            SELECT COALESCE(SUM(price),0) n FROM orders
            WHERE status IN ('delivered','closed')
            AND (status!='closed' OR closed_at IS NULL OR closed_at>?)
        """, (cutoff,)).fetchone()["n"]

    return {
        "customers": customers,
        "couriers": couriers,
        "orders": orders,
        "revenue": revenue
    }


@app.get("/api/admin/customers")
async def admin_customers(authorization: str = Header(None)):
    auth = get_auth(authorization)
    admin_only(auth)

    with db() as c:
        rows = c.execute("""
            SELECT id,name,phone,pin_plain,active,created_at
            FROM users
            WHERE role='customer'
            ORDER BY id DESC
        """).fetchall()

    return [dict(x) for x in rows]


@app.post("/api/admin/customers")
async def create_customer(
    data: CustomerCreate,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)
    admin_only(auth)

    phone = norm_phone(data.phone)
    pin = data.pin.strip()

    if not phone:
        raise HTTPException(400, "Введите телефон")

    if not re.fullmatch(r"\d{4,12}", pin):
        raise HTTPException(400, "PIN должен содержать от 4 до 12 цифр")

    if find_customer(phone):
        raise HTTPException(409, "Клиент уже существует")

    with db() as c:
        c.execute("""
            INSERT INTO users
            (name,phone,pin_hash,pin_plain,role,active,created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (
            data.name.strip(),
            phone,
            hash_pin(pin),
            pin,
            "customer",
            1,
            int(time.time())
        ))

    return {"ok": True, "pin": pin}


@app.delete("/api/admin/customers/{customer_id}")
async def delete_customer(
    customer_id: int,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)
    admin_only(auth)

    with db() as c:
        customer = c.execute("""
            SELECT id FROM users
            WHERE id=? AND role='customer'
        """, (customer_id,)).fetchone()

        if not customer:
            raise HTTPException(404, "Клиент не найден")

        c.execute(
            "UPDATE users SET active=0 WHERE id=?",
            (customer_id,)
        )

    return {"ok": True}


@app.get("/api/admin/couriers")
async def admin_couriers(authorization: str = Header(None)):
    auth = get_auth(authorization)
    admin_only(auth)

    with db() as c:
        rows = c.execute("""
            SELECT
                c.id,
                c.user_id,
                c.approved,
                c.online,
                c.lat,
                c.lon,
                c.active,
                u.name,
                u.phone,
                u.pin_plain
            FROM couriers c
            JOIN users u ON u.id=c.user_id
            ORDER BY c.id DESC
        """).fetchall()

    return [dict(x) for x in rows]


@app.post("/api/admin/couriers")
async def create_courier(
    data: CourierCreate,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)
    admin_only(auth)

    phone = norm_phone(data.phone)
    pin = data.pin.strip()

    if not re.fullmatch(r"\d{4,12}", pin):
        raise HTTPException(400, "PIN должен содержать от 4 до 12 цифр")

    with db() as c:
        exists = c.execute(
            "SELECT id FROM users WHERE phone=?",
            (phone,)
        ).fetchone()

        if exists:
            raise HTTPException(409, "Пользователь с таким телефоном уже существует")

        c.execute("""
            INSERT INTO users
            (name,phone,pin_hash,pin_plain,role,active,created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (
            data.name.strip(),
            phone,
            hash_pin(pin),
            pin,
            "courier",
            1,
            int(time.time())
        ))

        user_id = c.lastrowid

        c.execute("""
            INSERT INTO couriers
            (user_id,approved,online,active)
            VALUES(?,?,?,?)
        """, (user_id, 0, 0, 1))

    return {"ok": True, "pin": pin}


@app.post("/api/admin/couriers/{courier_id}/approve")
async def approve_courier(
    courier_id: int,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)
    admin_only(auth)

    with db() as c:
        c.execute("""
            UPDATE couriers
            SET approved=1,active=1
            WHERE id=?
        """, (courier_id,))

        row = c.execute("""
            SELECT user_id FROM couriers WHERE id=?
        """, (courier_id,)).fetchone()

        if row:
            c.execute(
                "UPDATE users SET active=1 WHERE id=?",
                (row["user_id"],)
            )

    return {"ok": True}


@app.post("/api/admin/couriers/{courier_id}/fire")
async def fire_courier(
    courier_id: int,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)
    admin_only(auth)

    with db() as c:
        row = c.execute(
            "SELECT user_id FROM couriers WHERE id=?",
            (courier_id,)
        ).fetchone()

        if not row:
            raise HTTPException(404, "Курьер не найден")

        c.execute("""
            UPDATE couriers
            SET active=0,approved=0,online=0
            WHERE id=?
        """, (courier_id,))

        c.execute(
            "UPDATE users SET active=0 WHERE id=?",
            (row["user_id"],)
        )

        c.execute("""
            UPDATE orders
            SET courier_id=NULL,status='new'
            WHERE courier_id=?
            AND status IN ('assigned','accepted')
        """, (courier_id,))

    return {"ok": True}


@app.get("/api/admin/orders")
async def admin_orders(authorization: str = Header(None)):
    auth = get_auth(authorization)
    admin_only(auth)

    cutoff = int(time.time()) - 300

    with db() as c:
        rows = c.execute("""
            SELECT
                o.id,o.title,o.address,o.price,o.status,
                o.created_at,o.customer_confirmed,
                cu.name AS customer_name,
                cu.phone AS customer_phone,
                co.name AS courier_name,
                cr.lat AS courier_lat,
                cr.lon AS courier_lon
            FROM orders o
            JOIN users cu ON cu.id=o.customer_id
            LEFT JOIN couriers cr ON cr.id=o.courier_id
            LEFT JOIN users co ON co.id=cr.user_id
            WHERE o.status!='closed'
               OR o.closed_at IS NULL
               OR o.closed_at>?
            ORDER BY o.id DESC
        """, (cutoff,)).fetchall()

    return [dict(x) for x in rows]


@app.post("/api/admin/orders")
async def create_order(
    data: OrderCreate,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)
    admin_only(auth)

    customer = find_customer(data.phone)

    if not customer:
        raise HTTPException(404, "Клиент с таким телефоном не найден")

    with db() as c:
        c.execute("""
            INSERT INTO orders
            (customer_id,title,address,price,status,created_at)
            VALUES(?,?,?,?,?,?)
        """, (
            customer["id"],
            data.title.strip(),
            data.address.strip(),
            data.price,
            "new",
            int(time.time())
        ))

        order_id = c.lastrowid

    return {"ok": True, "id": order_id}


@app.post("/api/admin/orders/{order_id}/assign")
async def assign_order(
    order_id: int,
    data: AssignIn,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)
    admin_only(auth)

    with db() as c:
        courier = c.execute("""
            SELECT id FROM couriers
            WHERE id=? AND active=1 AND approved=1
        """, (data.courier_id,)).fetchone()

        if not courier:
            raise HTTPException(404, "Курьер недоступен")

        order = c.execute(
            "SELECT id,status FROM orders WHERE id=?",
            (order_id,)
        ).fetchone()

        if not order:
            raise HTTPException(404, "Заказ не найден")

        c.execute("""
            UPDATE orders
            SET courier_id=?,status='assigned'
            WHERE id=?
        """, (data.courier_id, order_id))

    return {"ok": True}


@app.post("/api/admin/orders/{order_id}/close")
async def close_order(
    order_id: int,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)
    admin_only(auth)

    now = int(time.time())

    with db() as c:
        c.execute("""
            UPDATE orders
            SET status='closed',closed_at=?
            WHERE id=?
        """, (now, order_id))

    return {"ok": True}


@app.get("/api/customer/orders")
async def customer_orders(authorization: str = Header(None)):
    auth = get_auth(authorization)

    if auth["role"] != "customer":
        raise HTTPException(403, "Только для клиентов")

    cutoff = int(time.time()) - 300

    with db() as c:
        rows = c.execute("""
            SELECT
                o.*,
                co.name AS courier_name,
                cr.lat AS courier_lat,
                cr.lon AS courier_lon
            FROM orders o
            LEFT JOIN couriers cr ON cr.id=o.courier_id
            LEFT JOIN users co ON co.id=cr.user_id
            WHERE o.customer_id=?
            AND (
                o.status!='closed'
                OR o.closed_at IS NULL
                OR o.closed_at>?
            )
            ORDER BY o.id DESC
        """, (auth["user_id"], cutoff)).fetchall()

    return [dict(x) for x in rows]


@app.post("/api/customer/orders/{order_id}/confirm")
async def customer_confirm(
    order_id: int,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)

    if auth["role"] != "customer":
        raise HTTPException(403, "Только для клиентов")

    now = int(time.time())

    with db() as c:
        order = c.execute("""
            SELECT id,status FROM orders
            WHERE id=? AND customer_id=?
        """, (order_id, auth["user_id"])).fetchone()

        if not order:
            raise HTTPException(404, "Заказ не найден")

        if order["status"] != "delivered":
            raise HTTPException(400, "Заказ ещё не доставлен")

        c.execute("""
            UPDATE orders
            SET customer_confirmed=1,status='closed',closed_at=?
            WHERE id=?
        """, (now, order_id))

    return {"ok": True}


@app.get("/api/courier/orders")
async def courier_orders(authorization: str = Header(None)):
    auth = get_auth(authorization)

    if auth["role"] != "courier":
        raise HTTPException(403, "Только для курьеров")

    with db() as c:
        courier = c.execute("""
            SELECT id FROM couriers
            WHERE user_id=? AND active=1
        """, (auth["user_id"],)).fetchone()

        if not courier:
            raise HTTPException(403, "Курьер не найден")

        rows = c.execute("""
            SELECT
                o.*,
                u.name AS customer_name,
                u.phone AS customer_phone
            FROM orders o
            JOIN users u ON u.id=o.customer_id
            WHERE o.courier_id=?
            AND o.status!='closed'
            ORDER BY o.id DESC
        """, (courier["id"],)).fetchall()

    return [dict(x) for x in rows]


@app.post("/api/courier/online")
async def courier_online(
    data: OnlineIn,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)

    if auth["role"] != "courier":
        raise HTTPException(403, "Только для курьеров")

    with db() as c:
        c.execute("""
            UPDATE couriers
            SET online=?
            WHERE user_id=? AND active=1
        """, (1 if data.online else 0, auth["user_id"]))

    return {"ok": True}


@app.post("/api/courier/orders/{order_id}/accept")
async def courier_accept(
    order_id: int,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)

    if auth["role"] != "courier":
        raise HTTPException(403, "Только для курьеров")

    with db() as c:
        courier = c.execute(
            "SELECT id FROM couriers WHERE user_id=? AND active=1",
            (auth["user_id"],)
        ).fetchone()

        if not courier:
            raise HTTPException(403, "Курьер не найден")

        result = c.execute("""
            UPDATE orders
            SET status='accepted'
            WHERE id=? AND courier_id=? AND status='assigned'
        """, (order_id, courier["id"]))

        if result.rowcount == 0:
            raise HTTPException(400, "Заказ нельзя принять")

    return {"ok": True}


@app.post("/api/courier/orders/{order_id}/start")
async def courier_start(
    order_id: int,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)

    if auth["role"] != "courier":
        raise HTTPException(403, "Только для курьеров")

    with db() as c:
        courier = c.execute(
            "SELECT id FROM couriers WHERE user_id=? AND active=1",
            (auth["user_id"],)
        ).fetchone()

        if not courier:
            raise HTTPException(403, "Курьер не найден")

        result = c.execute("""
            UPDATE orders
            SET status='delivering'
            WHERE id=? AND courier_id=? AND status='accepted'
        """, (order_id, courier["id"]))

        if result.rowcount == 0:
            raise HTTPException(400, "Заказ нельзя начать")

    return {"ok": True}


@app.post("/api/courier/orders/{order_id}/complete")
async def courier_complete(
    order_id: int,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)

    if auth["role"] != "courier":
        raise HTTPException(403, "Только для курьеров")

    with db() as c:
        courier = c.execute(
            "SELECT id FROM couriers WHERE user_id=? AND active=1",
            (auth["user_id"],)
        ).fetchone()

        if not courier:
            raise HTTPException(403, "Курьер не найден")

        result = c.execute("""
            UPDATE orders
            SET status='delivered'
            WHERE id=? AND courier_id=? AND status='delivering'
        """, (order_id, courier["id"]))

        if result.rowcount == 0:
            raise HTTPException(400, "Заказ нельзя завершить")

    return {"ok": True}


@app.post("/api/courier/location")
async def courier_location(
    data: LocationIn,
    authorization: str = Header(None)
):
    auth = get_auth(authorization)

    if auth["role"] != "courier":
        raise HTTPException(403, "Только для курьеров")

    with db() as c:
        c.execute("""
            UPDATE couriers
            SET lat=?,lon=?,updated_at=?
            WHERE user_id=? AND active=1
        """, (
            data.lat,
            data.lon,
            int(time.time()),
            auth["user_id"]
        ))

    return {"ok": True}


async def cleanup_loop():
    while True:
        try:
            cutoff = int(time.time()) - 300
            with db() as c:
                c.execute("""
                    DELETE FROM orders
                    WHERE status='closed'
                    AND closed_at IS NOT NULL
                    AND closed_at<=?
                """, (cutoff,))
        except Exception as e:
            print("cleanup:", e)

        await asyncio.sleep(30)


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await asyncio.sleep(30)
            await ws.send_json({"type": "ping"})
    except Exception:
        pass


async def telegram_bot():
    if not BOT_TOKEN:
        print("BOT_TOKEN is not set")
        return

    tg = Application.builder().token(BOT_TOKEN).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🍽 Открыть приложение",
                    web_app=WebAppInfo(url=WEB_APP_URL)
                )
            ]
        ])

        await update.message.reply_text(
            "🍽 RESTARAN",
            reply_markup=keyboard
        )

    async def random_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("📱 Отправить номер", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )

        await update.message.reply_text(
            "Отправьте ваш номер телефона:",
            reply_markup=keyboard
        )

    async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
        contact = update.message.contact
        phone = contact.phone_number

        customer = find_customer(phone)

        if not customer:
            await update.message.reply_text("❌ Клиент с таким номером не найден.")
            return

        with db() as c:
            c.execute(
                "UPDATE users SET telegram_id=? WHERE id=?",
                (update.effective_user.id, customer["id"])
            )

        await update.message.reply_text(
            f"🔐 Ваш PIN: <code>{customer['pin_plain']}</code>",
            parse_mode="HTML"
        )

    async def location(update: Update, context: ContextTypes.DEFAULT_TYPE):
        loc = update.message.location
        tg_id = update.effective_user.id

        with db() as c:
            user = c.execute(
                "SELECT id FROM users WHERE telegram_id=? AND role='courier' AND active=1",
                (tg_id,)
            ).fetchone()

            if user:
                c.execute("""
                    UPDATE couriers
                    SET lat=?,lon=?,updated_at=?
                    WHERE user_id=? AND active=1
                """, (
                    loc.latitude,
                    loc.longitude,
                    int(time.time()),
                    user["id"]
                ))

    tg.add_handler(CommandHandler("start", start))
    tg.add_handler(CommandHandler("random", random_pin))
    tg.add_handler(MessageHandler(filters.CONTACT, contact))
    tg.add_handler(MessageHandler(filters.LOCATION, location))

    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling()

    await asyncio.Event().wait()


def bot_thread():
    asyncio.run(telegram_bot())


@app.on_event("startup")
async def startup():
    asyncio.create_task(cleanup_loop())

    if BOT_TOKEN:
        threading.Thread(
            target=bot_thread,
            daemon=True
        ).start()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
