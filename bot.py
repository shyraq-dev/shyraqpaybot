# bot_pay_products_admin_fixed.py
import os
import asyncio
import logging
from datetime import datetime, UTC
from typing import Optional
import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    LabeledPrice,
    PreCheckoutQuery,
    SuccessfulPayment,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.filters import CommandStart, Command, CommandObject

# ------------------ Бағдарламалық баптаулар (ORTA / ENV арқылы беріледі) ------------------
# Ешқашан тікелей кодқа токен жазбаңыз — орта айнымалы арқылы орнатыңыз.
load_dotenv()

MIN_AMOUNT_XTR = 1
MAX_AMOUNT_XTR = 10000
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN")  # Telegram payment provider token (Stars үшін бос болуы мүмкін)
ADMIN_ID = int(os.getenv("ADMIN_ID"))  # әкімшінің Telegram ID (оқшауланған ортада орнатыңыз)
CURRENCY = os.getenv("CURRENCY", "XTR")  # Валюта (Stars = XTR)
DB_PATH = os.getenv("DB_PATH")

# ------------------ Aiogram init ------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------ FSM күйі (донейт хабарламасын сұрағанда) ------------------
class Donate(StatesGroup):
    waiting_for_message = State()
    waiting_for_amount = State()
    waiting_for_custom_amount = State()

# ------------------ DB инициализация ------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # products: ұсыныстар/жазылымдар/пакеттер
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                duration_days INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1
            )
            """
        )
        # payments: нақты төлем жазбасы (қолдау хабарламасы үшін message бағаны қосылды)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER,
                amount INTEGER NOT NULL,
                currency TEXT,
                charge_id TEXT UNIQUE,
                date TEXT,
                refunded INTEGER DEFAULT 0,
                message TEXT
            )
            """
        )
        # subscriptions: пайдаланушы жазылымдары
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER,
                start_date TEXT,
                expiry_date TEXT
            )
            """
        )
        # refunds: локал журнал
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS refunds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                charge_id TEXT,
                admin_id INTEGER,
                reason TEXT,
                date TEXT
            )
            """
        )
        # pending_donations: төлемге дейінгі донейт хабарламаларын сақтау (payload-қа сілтеме жасаймыз)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_donations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                message TEXT,
                created_at TEXT
            )
            """
        )
        await db.commit()
    logger.info("DB initialized.")

# ------------------ Helper: fetch products ------------------
async def get_active_products(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, title, description, amount, currency, duration_days FROM products WHERE active=1 ORDER BY id ASC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return rows

# ------------------ Командалар: START / HELP ------------------
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Сәлем! Бұл бот арқылы өнімдерді (жазылым/пакет) сатып алуға болады.\n"
        "Пайдалану:\n"
        "/pay — өнімдер тізімі\n"
        "/premium — өз жазылымың туралы\n\n"
        "Әкімшілер: /admin"
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>📘 Қолжетімді пәрмендер:</b>\n"
        "/start — ботты бастау және мәзірге өту\n"
        "/pay — өнімдер тізімі және сатып алу\n"
        "/premium — Premium / жазылым күйін көру\n"
        "/donate — ботты жұлдыз (Stars) арқылы қолдау\n"
        "/help — көмек пен пәрмендер тізімі\n\n"
        "<b>👑 Әкімші пәрмендері:</b>\n"
        "/stats — жалпы статистика\n"
        "/refund [ID] — төлемді қайтару\n"
        "/add_product [атауы]|[бағасы]|[күндер]|[сипаттамасы] — жаңа өнім қосу\n"
        "/edit_product [id]|[атауы]|[бағасы]|[күндер]|[сипаттамасы] — өнімді өзгерту\n"
        "/set_product_status [id] [0|1] — өнімді қосу/өшіру\n"
        "/delete_product [id] — өнімді жою\n"
        "/mark_refund [charge_id] — төлемді қайтарылған деп белгілеу"
    )

# ------------------ PAY: өнімдер тізімі және сатып алу ------------------
@router.message(Command("pay"))
async def cmd_pay(message: Message):
    products = await get_active_products()
    if not products:
        return await message.answer("Қазір ұсыныстар жоқ. Кейінірек қайта көріңіз.")

    # әр өнімге жеке хабарлама және Сатып алу батырмасы
    for p in products:
        pid, title, desc, amount, currency, duration_days = p
        # amount — raw smallest unit (Stars жағдайда 1 = 1 XTR)
        text = f"<b>{title}</b>\n{desc or ''}\n\nСома: <code>{amount}</code> {currency}"
        if duration_days and duration_days > 0:
            text += f"\nМерзімі: {duration_days} күн"

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Сатып алу", callback_data=f"buy:{pid}")]
            ]
        )
        await message.answer(text, reply_markup=kb)

@router.callback_query(F.data.startswith("buy:"))
async def buy_callback(callback: CallbackQuery):
    await callback.answer()
    try:
        pid = int(callback.data.split(":", 1)[1])
    except Exception:
        return await callback.message.answer("Өнім идентификаторы қате.")

    # өнімді DB-дан жүктеу
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT title, description, amount, currency FROM products WHERE id = ? AND active = 1", (pid,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return await callback.message.answer("Өнім табылмады немесе белсенді емес.")

    title, description, amount, currency = row
    prices = [LabeledPrice(label=title, amount=amount)]
    payload = f"product:{pid}"  # кейінгі өңдеуде қолданамыз

    # provider_token — ENV арқылы беріледі (бағдарламалық қауіпсіздік)
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description=description or "-",
        payload=payload,
        provider_token=PROVIDER_TOKEN,
        currency=currency,
        prices=prices,
    )


# ------------------ DONATE пәрмені ------------------
@router.message(Command("donate"))
async def cmd_donate(message: Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Өткізу ➡️", callback_data="skip_message")]
    ])
    await message.answer(
        "💬 Донатпен бірге қандай хабарлама қалдырғың келеді?\n"
        "(Мысалы: «Бот ұнады!»)\n\n"
        "Қаламасаң, төмендегі «Өткізу» батырмасын бас.",
        reply_markup=keyboard
    )
    await state.set_state(Donate.waiting_for_message)


# ------------------ Өткізу батырмасы ------------------
@router.callback_query(F.data == "skip_message")
async def skip_donate_message(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(user_message=None)
    await state.set_state(Donate.waiting_for_amount)
    await _show_amount_buttons(callback.message)


# ------------------ Хабарлама жазылған жағдайда ------------------
@router.message(Donate.waiting_for_message)
async def donate_message_received(message: Message, state: FSMContext):
    user_message = (message.text or "").strip()
    await state.update_data(user_message=user_message)
    await state.set_state(Donate.waiting_for_amount)
    await _show_amount_buttons(message)


# ------------------ Сома таңдау батырмалары (3 қатар) ------------------
async def _show_amount_buttons(target):
    amounts = [1, 2, 5, 10, 20, 50, 100, 500, 1000]
    buttons = []
    row = []
    for i, amt in enumerate(amounts, start=1):
        row.append(InlineKeyboardButton(text=f"{amt} ⭐", callback_data=f"donate:{amt}"))
        if i % 3 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="Басқа сома ✏️", callback_data="donate:custom")])

    await target.answer(
        "💰 Донат сомасын таңдаңыз:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# ------------------ Таңдалған сома ------------------
@router.callback_query(lambda c: c.data.startswith("donate:"))
async def donate_amount_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = callback.data.split(":")[1]

    if data == "custom":
        await callback.message.answer(f" 📥 Сомаңызды енгізіңіз ({MIN_AMOUNT_XTR}-{MAX_AMOUNT_XTR} ⭐):")
        await state.set_state(Donate.waiting_for_custom_amount)
        return

    amount = int(data)
    await _send_invoice(callback.message, amount, state)


# ------------------ Custom сома енгізу ------------------
@router.message(Donate.waiting_for_custom_amount)
async def donate_custom_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        return await message.answer("❌ Сома тек бүтін сан болуы керек. Қайта енгізіңіз:")

    amount = int(text)
    if amount < MIN_AMOUNT_XTR:
        return await message.answer(f"⚠️ Ең аз донат {MIN_AMOUNT_XTR} ⭐.")
    if amount > MAX_AMOUNT_XTR:
        return await message.answer(f"⚠️ Ең көп донат {MAX_AMOUNT_XTR} ⭐.\nКөбірек бергің келсе — бірнеше рет жібере аласың 😉")

    await _send_invoice(message, amount, state)


# ------------------ Invoice жіберу ------------------
async def _send_invoice(message_or_callback, amount: int, state: FSMContext):
    user_id = message_or_callback.from_user.id
    state_data = await state.get_data()
    user_message = state_data.get("user_message", None)
    await state.clear()

    created_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO pending_donations (user_id, amount, message, created_at) VALUES (?, ?, ?, ?)",
            (user_id, amount, user_message, created_at)
        )
        await db.commit()
        pending_id = cur.lastrowid

    prices = [LabeledPrice(label="Ботты қолдау ⭐", amount=amount)]
    payload = f"donation:{pending_id}"

    desc = f"💌 Хабарлама: {user_message}" if user_message else "Қолдау үшін рақмет ❤️"

    try:
        await message_or_callback.answer_invoice(
            title="Ботты қолдау 🌠",
            description=desc,
            payload=payload,
            provider_token=PROVIDER_TOKEN,
            currency=CURRENCY,
            prices=prices,
            start_parameter="donate_support"
        )
    except Exception as e:
        logging.exception("Invoice жіберу сәтсіз")
        await message_or_callback.answer(f"❌ Төлем бастау мүмкін болмады: {e}")


# ------------------ Төлем сәтті болған соң ------------------
@router.message(F.successful_payment)
async def successful_payment(message: Message):
    sp = message.successful_payment
    user = message.from_user

    payload = getattr(sp, "invoice_payload", None)
    pending_id = None
    if payload and payload.startswith("donation:"):
        try:
            pending_id = int(payload.split(":")[1])
        except Exception:
            pending_id = None

    amount = sp.total_amount
    currency = sp.currency
    charge_id = sp.telegram_payment_charge_id

    async with aiosqlite.connect(DB_PATH) as db:
        user_message = None
        if pending_id:
            async with db.execute("SELECT message FROM pending_donations WHERE id=?", (pending_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    user_message = row[0]
        await db.execute(
            "INSERT INTO payments (user_id, amount, currency, charge_id, message, date) VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, amount, currency, charge_id, user_message, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        )
        await db.commit()

    # ✅ Пайдаланушыға жауап
    msg_to_user = f"✅ Төлем сәтті өтті!\n💰 Сома: {amount} ⭐"
    if user_message:
        msg_to_user += f"\n💌 Хабарлама: {user_message}"
    await message.answer(msg_to_user)

    # 👑 Әкімшіге хабар
    msg_to_admin = (
        f"🌟 Жаңа донат!\n"
        f"👤 @{user.username or user.full_name} ({user.id})\n"
        f"💰 {amount} ⭐\n"
    )
    if user_message:
        msg_to_admin += f"💌 {user_message}\n"
    msg_to_admin += f"Transaction ID: {charge_id}"

    try:
        await message.bot.send_message(ADMIN_ID, msg_to_admin)
    except Exception:
        logging.exception("Admin хабарламасын жіберу сәтсіз")

# ------------------ Pre-checkout ------------------
@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    # Қарапайым рәсім, қажетті валидацияны осында қосуға болады
    await pre_checkout_query.answer(ok=True)

# -----------------------------------------
# Бір ғана сәтті төлем хэндлері (барлық successful payments осы жерде өңделеді)
# -----------------------------------------
@router.message(F.successful_payment)
async def handle_successful_payment(message: Message):
    sp: SuccessfulPayment = message.successful_payment
    user = message.from_user

    # payload алу (тәжірибелерде әртүрлі жерде болуы мүмкін)
    payload = None
    try:
        payload = getattr(sp, "invoice_payload", None) or getattr(message, "invoice_payload", None)
    except Exception:
        payload = None

    amount = sp.total_amount  # raw integer
    currency = sp.currency
    charge_id = sp.telegram_payment_charge_id
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    product_id: Optional[int] = None
    user_message: Optional[str] = None

    # Егер payload өнімге жатса:
    if payload and isinstance(payload, str) and payload.startswith("product:"):
        try:
            product_id = int(payload.split(":", 1)[1])
        except Exception:
            product_id = None

    # Егер бұл донейт болса:
    if payload and isinstance(payload, str) and payload.startswith("donation:"):
        try:
            pending_id = int(payload.split(":", 1)[1])
        except Exception:
            pending_id = None
        if pending_id:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT user_id, message FROM pending_donations WHERE id = ?", (pending_id,)
                ) as cur:
                    prow = await cur.fetchone()
                if prow:
                    # біз жай ғана сақтаулы хабарламаны пайдаланамыз
                    user_message = prow[1]
                # очистка pending (необязательно, бірақ ұқыпты)
                await db.execute("DELETE FROM pending_donations WHERE id = ?", (pending_id,))
                await db.commit()

    # DB: payments енгізу
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments (user_id, product_id, amount, currency, charge_id, date, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user.id, product_id, amount, currency, charge_id, now_str, user_message),
        )
        # Егер өнім болса және оның duration_days > 0 болса — жазылым кестесіне жазу
        if product_id:
            async with db.execute("SELECT duration_days FROM products WHERE id = ? AND active = 1", (product_id,)) as cur:
                prow = await cur.fetchone()
            if prow:
                duration_days = prow[0] or 0
                if duration_days > 0:
                    start = datetime.utcnow()
                    expiry = start + timedelta(days=duration_days)
                    await db.execute(
                        "INSERT INTO subscriptions (user_id, product_id, start_date, expiry_date) VALUES (?, ?, ?, ?)",
                        (user.id, product_id, start.strftime("%Y-%m-%d %H:%M:%S"), expiry.strftime("%Y-%m-%d %H:%M:%S")),
                    )
        await db.commit()

    # Хабарлама сатып алушыға
    msg = (
        f"✅ Төлем сәтті өтті!\n"
        f"Transaction ID: <code>{charge_id}</code>\n"
        f"Сомасы (raw): <code>{amount}</code> {currency}\n"
    )
    if product_id:
        msg += f"Сатып алынған өнім ID: <code>{product_id}</code>\n"
    if user_message:
        msg += f"Сіздің хабарламаңыз: {user_message}\n"

    await message.answer(msg)

    # Әкімшіге хабарлау
    try:
        uname = f"@{user.username}" if user.username else f"{user.full_name} ({user.id})"
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Жаңа төлем</b>\n"
            f"Пайдаланушы: {uname}\n"
            f"product_id: {product_id} | amount: {amount} {currency}\n"
            f"charge_id: {charge_id}\n"
            + (f"message: {user_message}\n" if user_message else ""),
        )
    except Exception:
        logger.exception("Admin notify failed")

# ------------------ PREMIUM: пайдаланушы өз жазылымын тексеру ------------------
@router.message(Command("premium"))
async def cmd_premium(message: Message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT expiry_date, product_id FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return await message.answer("Сізде белсенді жазылым жоқ. /pay арқылы жазылыңыз.")

    expiry_str, product_id = row
    expiry = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
    now = datetime.utcnow()
    if expiry > now:
        remaining = expiry - now
        days = remaining.days
        await message.answer(f"🎖️ Сізде белсенді жазылым бар (өнім ID:{product_id}). Қалған күндер: {days} күн.")
    else:
        await message.answer("Сіздің жазылым мерзімі аяқталған. Қайта жазылыңыз /pay арқылы.")

# ------------------ ӘКІМШІ: өнімдерді басқару (инлайн + командалар) ------------------
def admin_only(user_id: int) -> bool:
    return user_id == ADMIN_ID

@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not admin_only(message.from_user.id):
        return await message.answer("🚫 Құқыңыз жоқ.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Өнімдер тізімі", callback_data="admin:products")],
        [InlineKeyboardButton(text="➕ Жаңа өнім қосу (пәрмен арқылы)", callback_data="admin:add_product_help")],
        [InlineKeyboardButton(text="📜 Қайтарулар (журнал)", callback_data="admin:refunds")],
    ])
    await message.answer("⚙️ Әкімші тақтасы:", reply_markup=kb)

@router.callback_query(F.data == "admin:home")
async def admin_home(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        return await callback.answer("Құқың жоқ", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Өнімдер тізімі", callback_data="admin:products")],
        [InlineKeyboardButton(text="➕ Жаңа өнім қосу (пәрмен арқылы)", callback_data="admin:add_product_help")],
        [InlineKeyboardButton(text="📜 Қайтарулар (журнал)", callback_data="admin:refunds")],
    ])
    await callback.message.edit_text("⚙️ Әкімші тақтасы:", reply_markup=kb)

@router.callback_query(F.data == "admin:products")
async def admin_products_list(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        return await callback.answer("Құқың жоқ", show_alert=True)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, title, amount, currency, duration_days, active FROM products ORDER BY id DESC") as cur:
            rows = await cur.fetchall()

    if not rows:
        return await callback.message.edit_text("Өнімдер жоқ. /add_product арқылы қосыңыз.")

    text = "<b>📦 Барлық өнімдер:</b>\n\n"
    kb_rows = []
    for r in rows:
        pid, title, amount, currency, duration, active = r
        status = "✅" if active else "⛔"
        text += f"ID:{pid} | {status} {title} — {amount} {currency} | dur:{duration}d\n"
        kb_rows.append([
            InlineKeyboardButton(text=f"✏️ {pid}", callback_data=f"admin:product:edit:{pid}"),
            InlineKeyboardButton(text=f"🗑️ {pid}", callback_data=f"admin:product:del:{pid}")
        ])
    kb_rows.append([InlineKeyboardButton(text="🏠 Басты", callback_data="admin:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data.startswith("admin:product:edit:"))
async def admin_product_edit_cb(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        return await callback.answer("Құқы жоқ", show_alert=True)
    try:
        pid = int(callback.data.split(":", 3)[-1])
    except:
        return await callback.answer("Қате ID", show_alert=True)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, title, description, amount, currency, duration_days, active FROM products WHERE id = ?", (pid,)) as cur:
            row = await cur.fetchone()
    if not row:
        return await callback.answer("Өнім табылмады.", show_alert=True)

    pid, title, desc, amount, currency, duration, active = row
    status_text = "Белсенді" if active else "Өшірілген"
    txt = f"ID:{pid}\n{title}\n{desc or ''}\nСома(raw):{amount} {currency}\nМерзім(days):{duration}\nСтатус: {status_text}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Тоқтату/Қосу", callback_data=f"admin:product:toggle:{pid}")],
        [InlineKeyboardButton(text="Өңдеу (команда)", callback_data=f"admin:product:editcmd:{pid}")],
        [InlineKeyboardButton(text="🗑️ Жою", callback_data=f"admin:product:del:{pid}")],
        [InlineKeyboardButton(text="📦 Тізімге оралу", callback_data="admin:products")]
    ])
    await callback.message.edit_text(txt, reply_markup=kb)

@router.callback_query(F.data.startswith("admin:product:toggle:"))
async def admin_product_toggle(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        return await callback.answer("Құқы жоқ", show_alert=True)
    pid = int(callback.data.split(":", 3)[-1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT active FROM products WHERE id = ?", (pid,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await callback.answer("Өнім табылмады.", show_alert=True)
        new = 0 if row[0] else 1
        await db.execute("UPDATE products SET active = ? WHERE id = ?", (new, pid))
        await db.commit()
    await callback.answer("Өнім статусы жаңартылды.")
    await callback.message.edit_text("Өнім статусы өзгертілді. /admin қайта ашыңыз немесе 'Тізімге оралу' басыңыз.")

@router.callback_query(F.data.startswith("admin:product:del:"))
async def admin_product_delete(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        return await callback.answer("Құқы жоқ", show_alert=True)
    pid = int(callback.data.split(":", 3)[-1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (pid,))
        await db.commit()
    await callback.answer("Өнім жойылды.")
    await callback.message.edit_text("Өнім жойылды. /admin арқылы тізімді қайта ашыңыз.")

@router.callback_query(F.data.startswith("admin:product:editcmd:"))
async def admin_product_editcmd(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        return await callback.answer("Құқың жоқ", show_alert=True)

    pid = int(callback.data.split(":", 3)[-1])
    await callback.answer()
    await callback.message.edit_text(
        f"Бұл өнімді пәрмен арқылы өңдеу үшін:\n"
        f"<code>/edit_product {pid}|Title|amount|duration_days|Description</code>\n\n"
        "Мысалы:\n"
        f"<code>/edit_product {pid}|Premium|100|30|Premium жазылым</code>",
        parse_mode="HTML"
    )

@router.callback_query(F.data == "admin:add_product_help")
async def admin_add_help(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        return await callback.answer("Құқың жоқ", show_alert=True)
    await callback.answer()
    await callback.message.edit_text(
        "Жаңа өнім қосу форматымен команда:\n"
        "<code>/add_product Title|amount|duration_days|Description</code>\n\n"
        "Мысал:\n"
        "<code>/add_product Premium 1⭐|1|30|Premium жазылым 30 күн</code>\n\n"
        "Ескерту: amount — raw integer (Stars smallest unit)."
    )

# Командалық қосу
@router.message(Command("add_product"))
async def cmd_add_product(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return await message.answer("Құқың жоқ")
    if not command.args:
        return await message.answer("Баптаулар қажет. Пішім: /add_product Title|amount|duration_days|Description")

    try:
        parts = command.args.split("|", 3)
        title = parts[0].strip()
        amount = int(parts[1].strip())
        duration = int(parts[2].strip()) if parts[2].strip() else 0
        description = parts[3].strip() if len(parts) > 3 else ""
    except Exception as e:
        return await message.answer(f"Баптау қате: {e}\nПішім: /add_product Title|amount|duration_days|Description")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO products (title, description, amount, currency, duration_days, active) VALUES (?, ?, ?, ?, ?, 1)",
            (title, description, amount, CURRENCY, duration),
        )
        await db.commit()
    await message.answer("✅ Өнім қосылды.")

@router.message(Command("edit_product"))
async def cmd_edit_product(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return await message.answer("Құқың жоқ")
    if not command.args:
        return await message.answer("Пішім: /edit_product id|Title|amount|duration_days|Description")

    try:
        parts = command.args.split("|", 4)
        pid = int(parts[0].strip())
        title = parts[1].strip()
        amount = int(parts[2].strip())
        duration = int(parts[3].strip())
        description = parts[4].strip() if len(parts) > 4 else ""
    except Exception as e:
        return await message.answer(f"Баптау қате: {e}\nПішім: /edit_product id|Title|amount|duration_days|Description")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET title = ?, description = ?, amount = ?, duration_days = ? WHERE id = ?",
            (title, description, amount, duration, pid),
        )
        await db.commit()
    await message.answer("✅ Өнім жаңартылды.")

@router.message(Command("set_product_status"))
async def cmd_set_prod_status(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return await message.answer("Құқың жоқ")
    if not command.args:
        return await message.answer("Пішім: /set_product_status <id> <0|1>", parse_mode=None)

    try:
        parts = command.args.split()
        pid = int(parts[0])
        status = 1 if parts[1] == "1" else 0
    except Exception:
        return await message.answer("Қате баптау. /set_product_status <id> <0|1>", parse_mode=None)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET active = ? WHERE id = ?", (status, pid))
        await db.commit()
    await message.answer("Статус өзгертілді.")

@router.message(Command("delete_product"))
async def cmd_delete_product(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return await message.answer("Құқың жоқ")
    if not command.args:
        return await message.answer("Пішім: /delete_product <id>", parse_mode=None)
    try:
        pid = int(command.args.strip())
    except:
        return await message.answer("ID сан болуы тиіс.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (pid,))
        await db.commit()
    await message.answer("Өнім жойылды.")

# -----------------------------------------
# /stats пәрмені (тек әкімшіге)
# -----------------------------------------
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not admin_only(message.from_user.id):
        await message.answer("Бұл пәрмен тек әкімшіге арналған 🚫")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*), SUM(amount) FROM payments") as cur:
            row = await cur.fetchone()
    total_payments, total_amount = (row or (0, 0))
    total_payments = total_payments or 0
    total_amount = total_amount or 0

    await message.answer(
        f"📊 <b>Статистика</b>\n"
        f"Төлем саны: {total_payments}\n"
        f"Жалпы жиналған (raw): {total_amount} {CURRENCY}"
    )

# ------------------ Refund маркерлеу (admin only) ------------------
@router.message(Command("mark_refund"))
async def cmd_mark_refund(message: Message, command: CommandObject):
    if not admin_only(message.from_user.id):
        return await message.answer("Құқың жоқ")
    if not command.args:
        return await message.answer("Пішім: /mark_refund <charge_id>", parse_mode=None)
    cid = command.args.strip()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE payments SET refunded = 1 WHERE charge_id = ?", (cid,))
        await db.execute(
            "INSERT INTO refunds (charge_id, admin_id, reason, date) VALUES (?, ?, ?, ?)",
            (cid, message.from_user.id, "Manual refund marked", now),
        )
        await db.commit()
        async with db.execute("SELECT user_id FROM payments WHERE charge_id = ?", (cid,)) as cur:
            row = await cur.fetchone()
    await message.answer(f"✅ {cid} жергілікті түрде қайтарылды (маркерленді).")
    if row:
        user_id = row[0]
        try:
            await bot.send_message(user_id, f"Сіздің төлеміңіз (ID: <code>{cid}</code>) әкімші тарапынан қайтарылған.")
        except Exception:
            logger.exception("Notify user refund failed")

# ------------------ Admin: refunds list ------------------
@router.callback_query(F.data == "admin:refunds")
async def admin_refunds_list(callback: CallbackQuery):
    if not admin_only(callback.from_user.id):
        return await callback.answer("Құқың жоқ", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT charge_id, admin_id, reason, date FROM refunds ORDER BY id DESC LIMIT 20") as cur:
            rows = await cur.fetchall()
    if not rows:
        return await callback.message.edit_text("Қайтарулар жоқ.")
    text = "<b>📜 Қайтарулар (журнал):</b>\n\n"
    for cid, aid, reason, date in rows:
        text += f"{cid} — admin:{aid} — {reason} — {date}\n"
    await callback.message.edit_text(text)

# ------------------ Catch-all echo (сақтықпен) ------------------
@router.message()
async def echo_catch_all(message: Message):
    # командаларға кедергі жасамау үшін тек командалар емес хабарламаларды қайталаймыз
    if message.text and message.text.startswith("/"):
        await message.answer("Белгісіз пәрмен. /help қараңыз.")
    else:
        try:
            # message.send_copy мүмкіндігі арқылы контент-ті қайталаймыз
            await message.send_copy(chat_id=message.chat.id)
        except Exception:
            # кейбір типтер қайталанбайды — жай тыныштық сақтау
            pass

# ------------------ Негізгі іске қосу ------------------
async def main():
    logger.info("ДҚ қалпына келтіріліп жатыр...")
    await init_db()
    logger.info("Бот іске қосылуға дайын.")
    # Long polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped by user")
