"""
🐻 Grizz Shop Bot  ·  aiogram 3  ·  Supabase async  ·  CryptoBot
v2 — Smart TopUp, 30min deadline, TON auto-check, receipt validation
"""
import asyncio, io, logging, os, re, sys
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "env")

# ── ENV ───────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
ADMIN_IDS        = {int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()}
CRYPTO_TOKEN     = os.getenv("CRYPTO_API_TOKEN", "")
SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")

UAH_PER_STAR     = float(os.getenv("UAH_PER_STAR", "4.5"))
UAH_PER_PREMIUM  = float(os.getenv("UAH_PER_MONTH_PREMIUM", "280"))
UAH_PER_TON      = float(os.getenv("UAH_PER_TON", "200"))
UAH_PER_GRAM     = float(os.getenv("UAH_PER_GRAM", "140"))

_E = {
    "welcome":"5222108309795908493","flash":"5219943216781995020",
    "gift_m":"6032644646587338669","user_p":"6032608126480421344",
    "money":"5769126056262898415","star_bal":"5985389642575254458",
    "id_ico":"5884366771913233289","calendar":"5776233299424843260",
    "trophy":"5805553606635559688","star":"5954135079662916434",
    "up":"5963103826075456248","info":"6028435952299413210",
    "orders":"6037475557082403885","users":"5879905000972358125",
    "gift_s":"5453969572354878595","user_s":"6035084557378654059",
    "magic":"5258396243666681152","diamond":"5769406891289481208",
}
E = {k: f'<tg-emoji emoji-id="{v}">⭐</tg-emoji>' for k, v in _E.items()}

import httpx
import pypdf
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, Message
)
from supabase import acreate_client, AsyncClient as SupabaseClient

sb: SupabaseClient = None
# ══════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════════════════
async def db_get_user(user_id: int) -> Optional[dict]:
    r = await sb.table("users").select("*").eq("user_id", user_id).execute()
    return r.data[0] if r.data else None

async def db_ensure_user(user_id: int, username: str, first_name: str) -> dict:
    u = await db_get_user(user_id)
    if not u:
        await sb.table("users").insert({
            "user_id": user_id, "username": username or "",
            "first_name": first_name or "",
            "balance_uah": 0.0, "total_bought_uah": 0.0,
        }).execute()
        u = await db_get_user(user_id)
    return u

async def db_add_order(user_id:int, product:str, amount_uah:float,
                       payload:str, status:str="pending",
                       deadline_minutes:int=30) -> int:
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=deadline_minutes)).isoformat()
    r = await sb.table("orders").insert({
        "user_id": user_id, "product": product,
        "amount_uah": amount_uah, "payload": payload,
        "status": status, "deadline": deadline,
    }).execute()
    return r.data[0]["id"]

async def db_complete_order(order_id: int):
    await sb.table("orders").update({"status":"completed"}).eq("id",order_id).execute()

async def db_get_setting(key:str, default:str="") -> str:
    r = await sb.table("settings").select("value").eq("key",key).execute()
    return r.data[0]["value"] if r.data else default

async def db_set_setting(key:str, value:str):
    r = await sb.table("settings").select("id").eq("key",key).execute()
    if r.data:
        await sb.table("settings").update({"value":value}).eq("key",key).execute()
    else:
        await sb.table("settings").insert({"key":key,"value":value}).execute()

async def db_bump_total(user_id:int, amount:float):
    u = await db_get_user(user_id)
    if u:
        await sb.table("users").update({
            "total_bought_uah": u["total_bought_uah"] + amount
        }).eq("user_id", user_id).execute()

async def db_add_balance(user_id:int, amount:float):
    u = await db_get_user(user_id)
    if u:
        await sb.table("users").update({
            "balance_uah": u["balance_uah"] + amount
        }).eq("user_id", user_id).execute()

async def db_deduct_balance(user_id:int, amount:float) -> bool:
    u = await db_get_user(user_id)
    if not u or u["balance_uah"] < amount:
        return False
    await sb.table("users").update({
        "balance_uah": u["balance_uah"] - amount
    }).eq("user_id", user_id).execute()
    return True

async def db_rank(user_id:int) -> int:
    u = await db_get_user(user_id)
    if not u:
        return 1
    r = await sb.table("users").select("user_id",count="exact").gt(
        "total_bought_uah", u["total_bought_uah"]).execute()
    return (r.count or 0) + 1

async def db_receipt_used(code:str) -> bool:
    r = await sb.table("used_receipts").select("id").eq("receipt_code",code).execute()
    return bool(r.data)

async def db_mark_receipt(code:str, bank:str, amount:float, user_id:int):
    with suppress(Exception):
        await sb.table("used_receipts").insert({
            "receipt_code":code,"bank":bank,"amount":amount,"user_id":user_id
        }).execute()

async def db_add_pending_send(user_id:int, coin:str, to_address:str,
                               amount:float, amount_uah:float) -> int:
    r = await sb.table("pending_sends").insert({
        "user_id":user_id,"coin":coin,"to_address":to_address,
        "amount":amount,"amount_uah":amount_uah,"status":"pending"
    }).execute()
    return r.data[0]["id"]

# ── Pending CryptoBot topups ──────────────────────────────────────────────
async def db_create_crypto_topup(user_id:int, invoice_id:int,
                                  amount_usdt:float, amount_uah:float) -> int:
    r = await sb.table("pending_crypto_topups").select("*")\
        .eq("invoice_id",invoice_id).execute()
    if r.data:
        return r.data[0]["id"]
    res = await sb.table("pending_crypto_topups").insert({
        "user_id":user_id,"invoice_id":invoice_id,
        "amount_usdt":amount_usdt,"amount_uah":amount_uah,"status":"pending"
    }).execute()
    return res.data[0]["id"]

async def db_get_crypto_topup(invoice_id:int) -> Optional[dict]:
    r = await sb.table("pending_crypto_topups").select("*")\
        .eq("invoice_id",invoice_id).eq("status","pending").execute()
    return r.data[0] if r.data else None

async def db_complete_crypto_topup(topup_id:int):
    await sb.table("pending_crypto_topups").update({
        "status":"credited",
        "credited_at": datetime.now(timezone.utc).isoformat()
    }).eq("id",topup_id).execute()

# ── Pending TON topups ────────────────────────────────────────────────────
async def db_create_ton_topup(user_id:int, comment:str,
                               amount_ton:float, amount_uah:float) -> int:
    r = await sb.table("pending_ton_topups").select("*")\
        .eq("user_id", user_id).eq("status","pending").execute()
    if r.data:
        return r.data[0]["id"]
    res = await sb.table("pending_ton_topups").insert({
        "user_id":user_id,"comment":comment,
        "amount_ton":amount_ton,"amount_uah":amount_uah,"status":"pending"
    }).execute()
    return res.data[0]["id"]

async def db_get_ton_topup_by_comment(comment:str) -> Optional[dict]:
    r = await sb.table("pending_ton_topups").select("*")\
        .eq("comment",comment).eq("status","pending").execute()
    return r.data[0] if r.data else None

async def db_complete_ton_topup(topup_id:int):
    await sb.table("pending_ton_topups").update({
        "status":"credited",
        "credited_at": datetime.now(timezone.utc).isoformat()
    }).eq("id",topup_id).execute()
# ══════════════════════════════════════════════════════════════════════════
# CHECK.GOV.UA / TON / CRYPTOBOT
# ══════════════════════════════════════════════════════════════════════════
BANK_OPTIONS = {
    "monobank":   "🟢 Monobank",
    "privatbank": "🔵 PrivatBank",
    "oshchadbank":"🟡 Ощадбанк",
    "pumb":       "🟠 ПУМБ",
    "ukrsibbank": "🔷 UKRSIB",
    "sensebank":  "🔴 Sense Bank",
}

async def check_gov_receipt(bank_id:str, receipt_code:str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://check.gov.ua/api/cabinet/receipts/check",
                params={"rc": receipt_code, "bid": bank_id},
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logging.error(f"check.gov.ua error: {e}")
    return None

def extract_receipt_code(pdf_bytes: bytes) -> Optional[str]:
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages:
            text += (page.extract_text() or "")
        pattern = r'[A-ZА-ЯІЇЄ0-9]{4}-[A-ZА-ЯІЇЄ0-9]{4}-[A-ZА-ЯІЇЄ0-9]{4}-[A-ZА-ЯІЇЄ0-9]{4}'
        matches = re.findall(pattern, text.upper())
        return matches[0] if matches else None
    except Exception as e:
        logging.error(f"PDF parse error: {e}")
        return None

CRYPTO_API = "https://pay.crypt.bot/api"

async def get_usd_rate() -> float:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://api.monobank.ua/bank/currency")
            for item in r.json():
                if item.get("currencyCodeA")==840 and item.get("currencyCodeB")==980:
                    return float(item["rateSell"])
    except Exception:
        pass
    return float(await db_get_setting("usdt_to_uah","40"))

async def cryptobot_create(amount:float, asset:str, payload:str, desc:str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{CRYPTO_API}/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN},
            json={"asset":asset,"amount":str(round(amount,6)),
                  "payload":payload,"description":desc,
                  "allow_comments":False,"allow_anonymous":False})
    d = r.json()
    if d.get("ok"):
        return d["result"]
    raise ValueError(d.get("error",{}).get("name","CryptoBot error"))

async def cryptobot_status(invoice_id:int) -> str:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{CRYPTO_API}/getInvoices",
            headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN},
            params={"invoice_ids":str(invoice_id)})
    d = r.json()
    if d.get("ok") and d["result"]["items"]:
        return d["result"]["items"][0]["status"]
    return "unknown"

async def check_ton_transaction_by_comment(wallet_address:str, comment:str,
                                            expected_amount:float=0) -> Optional[dict]:
    """Перевіряє TON транзакцію з вказаним коментарем через toncenter API."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://toncenter.com/api/v2/getTransactions",
                params={"address": wallet_address, "limit": 50, "archival": "false"},
                headers={"User-Agent": "GrizzShopBot/2.0"}
            )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("ok"):
            return None
        for tx in data.get("result", []):
            in_msg = tx.get("in_msg", {})
            if not in_msg:
                continue
            msg_body = in_msg.get("message", "") or ""
            value_ton = int(in_msg.get("value", 0)) / 1_000_000_000
            if comment.strip() in msg_body.strip():
                if expected_amount == 0 or abs(value_ton - expected_amount) / max(expected_amount, 0.001) < 0.1:
                    return {
                        "amount_ton": round(value_ton, 4),
                        "hash": tx.get("transaction_id", {}).get("hash", ""),
                    }
    except Exception as e:
        logging.error(f"TON check error: {e}")
    return None

# ══════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ══════════════════════════════════════════════════════════════════════════
MEDIA = Path(__file__).parent / "media"

def img(name:str) -> Optional[FSInputFile]:
    for ext in ("jpg","jpeg","webp","png"):
        p = MEDIA / f"{name}.{ext}"
        if p.exists():
            return FSInputFile(str(p))
    return None

def btn(text:str, callback_data:str=None, *, url:str=None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data, url=url)

def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))

def is_admin(uid:int) -> bool:
    return uid in ADMIN_IDS

async def get_price(key:str, default:float) -> float:
    return float(await db_get_setting(key) or str(default))

def home_markup(uid:int=0) -> InlineKeyboardMarkup:
    rows = [
        [btn("⭐  Купити зірки", "buy_stars")],
        [btn("🎁  Telegram Premium","buy_premium"),
         btn("◎  TON", "ton_choose:TON")],
        [btn("💎  GRAM", "ton_choose:GRAM"),
         btn("🏪  NFT Маркет", "nft_market")],
        [btn("👤  Профіль","profile"),
         btn("💬  Підтримка","support")],
    ]
    if is_admin(uid):
        rows.append([btn("⚙️  Адмін-панель","admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _topup_markup() -> InlineKeyboardMarkup:
    return kb(
        [btn("💳  Українська картка (UAH)", "topup_uah")],
        [btn("🤖  CryptoBot (USDT)", "topup_crypto")],
        [btn("◎  TON — гаманець", "topup_ton")],
        [btn("🔙  На головну", "home")],
    )

async def send_insufficient_balance(target, need:float, have:float):
    lack = round(need - have, 2)
    text = (
        f"❌ <b>Недостатньо коштів на балансі!</b>\n\n"
        f"<blockquote>"
        f"💳  Ваш баланс:   <b>{round(have,2)}₴</b>\n"
        f"💰  Потрібно:     <b>{round(need,2)}₴</b>\n"
        f"⬆️  Не вистачає: <b>{lack}₴</b>"
        f"</blockquote>\n\n"
        f"Поповніть баланс і спробуйте знову:"
    )
    markup = kb(
        [btn(f"💰  Поповнити баланс (+{lack}₴)", f"topup_need:{need}")],
        [btn("🔙  На головну","home")],
    )
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)

# ══════════════════════════════════════════════════════════════════════════
# FSM STATES
# ══════════════════════════════════════════════════════════════════════════
class BuyStars(StatesGroup):
    recipient=State(); amount=State(); custom_amount=State(); payment=State()

class BuyPremium(StatesGroup):
    recipient=State(); months=State(); payment=State()

class BuyTon(StatesGroup):
    coin=State(); amount=State(); wallet=State()
    bank_select=State(); receipt_upload=State()

class TopUp(StatesGroup):
    amount_input=State()
    bank_select=State()
    receipt_upload=State()
    ton_wait=State()

class Support(StatesGroup):
    ticket=State()

class Admin(StatesGroup):
    set_price=State(); manual_send=State(); broadcast=State()
    set_req_card=State(); set_req_name=State(); set_req_initials=State()
    give_uid=State(); give_amount=State()
    set_ton_addr=State(); set_gram_addr=State()
    set_ton_rate=State(); set_gram_rate=State()

router = Router()
# ══════════════════════════════════════════════════════════════════════════
# START / HOME
# ══════════════════════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(msg:Message, state:FSMContext):
    await state.clear()
    await db_ensure_user(msg.from_user.id,
        msg.from_user.username or "", msg.from_user.first_name or "")
    photo = img("welcome")
    text = (
        f"{E['welcome']} <b>Ласкаво просимо до Grizz Shop!</b>\n\n"
        f"<blockquote>{E['flash']}  Купуйте Зірки, Premium та крипту "
        f"за пару кліків.</blockquote>\n\n"
        f"{E['gift_m']}  <b>Відправка товарів протягом 30 хвилин!</b>"
    )
    if photo:
        await msg.answer_photo(photo, caption=text,
                               reply_markup=home_markup(msg.from_user.id))
    else:
        await msg.answer(text, reply_markup=home_markup(msg.from_user.id))

@router.callback_query(F.data == "home")
async def home_cb(cb:CallbackQuery, state:FSMContext):
    await state.clear()
    with suppress(TelegramBadRequest):
        await cb.message.delete()
    photo = img("welcome")
    text = (
        f"{E['welcome']} <b>Головне меню — Grizz Shop</b>\n\n"
        f"{E['gift_m']}  <b>Відправка товарів протягом 30 хвилин!</b>"
    )
    if photo:
        await cb.message.answer_photo(photo, caption=text,
                                      reply_markup=home_markup(cb.from_user.id))
    else:
        await cb.message.answer(text, reply_markup=home_markup(cb.from_user.id))
    await cb.answer()

@router.callback_query(F.data == "nft_market")
async def nft_market(cb:CallbackQuery):
    await cb.message.answer(
        f"🏪 <b>NFT Маркет</b>\n\n"
        f"<blockquote>Скоро відкриємо унікальний маркет\n"
        f"цифрових активів і NFT-колекцій!</blockquote>\n\n"
        f"🔔 Слідкуйте за оновленнями.",
        reply_markup=kb([btn("🔙  На головну","home")])
    )
    await cb.answer()

# ══════════════════════════════════════════════════════════════════════════
# STARS FLOW
# ══════════════════════════════════════════════════════════════════════════
STARS_PKGS = [50,100,150,250,350,500,750,1000,1500,2500,5000,10000]

@router.callback_query(F.data == "buy_stars")
async def stars_start(cb:CallbackQuery, state:FSMContext):
    await state.set_state(BuyStars.recipient)
    photo = img("stars")
    text = (
        f"{E['star']} <b>Купівля Telegram Stars</b>\n\n"
        f"<blockquote>Введіть <b>@username</b> отримувача\n"
        f"або натисніть «Собі»</blockquote>"
    )
    markup = kb([btn("🎁  Собі","stars_self")],[btn("🔙  На головну","home")])
    with suppress(TelegramBadRequest): await cb.message.delete()
    if photo: await cb.message.answer_photo(photo,caption=text,reply_markup=markup)
    else:     await cb.message.answer(text,reply_markup=markup)
    await cb.answer()

@router.callback_query(F.data=="stars_self", BuyStars.recipient)
async def stars_self(cb:CallbackQuery, state:FSMContext):
    uname = cb.from_user.username or str(cb.from_user.id)
    await state.update_data(recipient=f"@{uname}")
    await _stars_show_pkgs(cb, state)

@router.message(BuyStars.recipient)
async def stars_recv_input(msg:Message, state:FSMContext):
    t = msg.text.strip()
    if not t.startswith("@"): t = f"@{t}"
    await state.update_data(recipient=t)
    await _stars_show_pkgs_msg(msg, state)

async def _stars_show_pkgs(cb:CallbackQuery, state:FSMContext):
    data = await state.get_data()
    recipient = data.get("recipient","")
    await state.set_state(BuyStars.amount)
    price = await get_price("uah_per_star", UAH_PER_STAR)
    rows=[]
    for i in range(0,len(STARS_PKGS),2):
        row=[]
        for p in STARS_PKGS[i:i+2]:
            row.append(btn(f"⭐  {p}  ({round(p*price)}₴)", f"stars_buy:{p}"))
        rows.append(row)
    rows.append([btn("🪄  Своя кількість","stars_custom")])
    rows.append([btn("🔙  На головну","home")])
    text = (
        f"{E['user_s']} <b>Отримувач:</b> <code>{recipient}</code>\n\n"
        f"{E['magic']}  Виберіть пакет або вкажіть свою кількість:"
    )
    photo = img("stars")
    with suppress(TelegramBadRequest): await cb.message.delete()
    mkp = InlineKeyboardMarkup(inline_keyboard=rows)
    if photo: await cb.message.answer_photo(photo,caption=text,reply_markup=mkp)
    else:     await cb.message.answer(text,reply_markup=mkp)
    await cb.answer()

async def _stars_show_pkgs_msg(msg:Message, state:FSMContext):
    data = await state.get_data()
    recipient = data.get("recipient","")
    await state.set_state(BuyStars.amount)
    price = await get_price("uah_per_star", UAH_PER_STAR)
    rows=[]
    for i in range(0,len(STARS_PKGS),2):
        row=[]
        for p in STARS_PKGS[i:i+2]:
            row.append(btn(f"⭐  {p}  ({round(p*price)}₴)",f"stars_buy:{p}"))
        rows.append(row)
    rows.append([btn("🪄  Своя кількість","stars_custom")])
    rows.append([btn("🔙  На головну","home")])
    text = (
        f"{E['user_s']} <b>Отримувач:</b> <code>{recipient}</code>\n\n"
        f"{E['magic']}  Виберіть пакет або вкажіть свою кількість:"
    )
    photo = img("stars")
    mkp = InlineKeyboardMarkup(inline_keyboard=rows)
    if photo: await msg.answer_photo(photo,caption=text,reply_markup=mkp)
    else:     await msg.answer(text,reply_markup=mkp)

@router.callback_query(F.data=="stars_custom")
async def stars_custom(cb:CallbackQuery, state:FSMContext):
    await state.set_state(BuyStars.custom_amount)
    await cb.message.answer(
        f"{E['magic']} <b>Введіть кількість зірок</b>\n\n<i>Мінімум — 50 зірок</i>",
        reply_markup=kb([btn("🔙  На головну","home")])
    )
    await cb.answer()

@router.message(BuyStars.custom_amount)
async def stars_custom_input(msg:Message, state:FSMContext):
    try:
        n = int(msg.text.strip())
        if n < 50: raise ValueError
    except ValueError:
        await msg.answer("❌ Введіть ціле число ≥ 50!"); return
    await state.update_data(stars_amount=n)
    await _stars_confirm_msg(msg, state)

@router.callback_query(F.data.startswith("stars_buy:"))
async def stars_buy_cb(cb:CallbackQuery, state:FSMContext):
    n = int(cb.data.split(":")[1])
    await state.update_data(stars_amount=n)
    await _stars_confirm_cb(cb, state)

async def _build_payment_rows(user_id:int, total:float) -> tuple[list, str]:
    """Повертає (rows, extra_text) для вибору оплати."""
    u = await db_get_user(user_id)
    bal = u["balance_uah"] if u else 0
    rows = []
    extra = f"\n💳 Ваш баланс: <b>{round(bal,2)}₴</b>"
    if bal >= total:
        rows.append([btn(f"💳  Оплатити з балансу ({round(bal,2)}₴)","pay_balance")])
    else:
        lack = round(total - bal, 2)
        extra += f"\n❌ Не вистачає: <b>{lack}₴</b>"
        rows.append([btn(f"💰  Поповнити баланс (+{lack}₴)", f"topup_need:{total}")])
    rows.append([btn("🤖  CryptoBot (USDT)","pay_crypto")])
    rows.append([btn("🔙  На головну","home")])
    return rows, extra

async def _stars_confirm_cb(cb:CallbackQuery, state:FSMContext):
    data = await state.get_data()
    n    = data["stars_amount"]
    recv = data["recipient"]
    price= await get_price("uah_per_star", UAH_PER_STAR)
    total= round(n*price)
    await state.set_state(BuyStars.payment)
    u = await db_get_user(cb.from_user.id)
    bal = u["balance_uah"] if u else 0
    lack = round(total - bal, 2) if bal < total else 0
    text = (
        f"{E['star']} <b>Підтвердження замовлення</b>\n\n"
        f"<blockquote>"
        f"⭐  Товар:       <b>{n} Telegram Stars</b>\n"
        f"{E['user_s']}  Отримувач:  <code>{recv}</code>\n"
        f"💰  До сплати: <b>{total}₴</b>"
        f"</blockquote>\n\n"
        f"💳 Ваш баланс: <b>{round(bal,2)}₴</b>"
    )
    rows = []
    if bal >= total:
        rows.append([btn(f"💳  Оплатити з балансу ({round(bal,2)}₴)","stars_pay_balance")])
    else:
        text += f"\n❌ Не вистачає: <b>{lack}₴</b>"
        rows.append([btn(f"💰  Поповнити баланс (+{lack}₴)", f"topup_need:{total}")])
    rows.append([btn("🤖  CryptoBot (USDT)","stars_pay_crypto")])
    rows.append([btn("🔙  На головну","home")])
    with suppress(TelegramBadRequest): await cb.message.delete()
    photo = img("stars")
    mkp = InlineKeyboardMarkup(inline_keyboard=rows)
    if photo: await cb.message.answer_photo(photo,caption=text,reply_markup=mkp)
    else:     await cb.message.answer(text,reply_markup=mkp)
    await cb.answer()

async def _stars_confirm_msg(msg:Message, state:FSMContext):
    data = await state.get_data()
    n    = data["stars_amount"]
    recv = data["recipient"]
    price= await get_price("uah_per_star", UAH_PER_STAR)
    total= round(n*price)
    await state.set_state(BuyStars.payment)
    u = await db_get_user(msg.from_user.id)
    bal = u["balance_uah"] if u else 0
    lack = round(total - bal, 2) if bal < total else 0
    text = (
        f"{E['star']} <b>Підтвердження замовлення</b>\n\n"
        f"<blockquote>"
        f"⭐  Товар:       <b>{n} Telegram Stars</b>\n"
        f"{E['user_s']}  Отримувач:  <code>{recv}</code>\n"
        f"💰  До сплати: <b>{total}₴</b>"
        f"</blockquote>\n\n"
        f"💳 Ваш баланс: <b>{round(bal,2)}₴</b>"
    )
    rows = []
    if bal >= total:
        rows.append([btn(f"💳  Оплатити з балансу ({round(bal,2)}₴)","stars_pay_balance")])
    else:
        text += f"\n❌ Не вистачає: <b>{lack}₴</b>"
        rows.append([btn(f"💰  Поповнити баланс (+{lack}₴)", f"topup_need:{total}")])
    rows.append([btn("🤖  CryptoBot (USDT)","stars_pay_crypto")])
    rows.append([btn("🔙  На головну","home")])
    photo = img("stars")
    mkp = InlineKeyboardMarkup(inline_keyboard=rows)
    if photo: await msg.answer_photo(photo,caption=text,reply_markup=mkp)
    else:     await msg.answer(text,reply_markup=mkp)

@router.callback_query(F.data=="stars_pay_balance", BuyStars.payment)
async def stars_pay_balance(cb:CallbackQuery, state:FSMContext, bot:Bot):
    data = await state.get_data()
    n    = data["stars_amount"]
    recv = data["recipient"]
    price= await get_price("uah_per_star", UAH_PER_STAR)
    total= round(n*price)
    ok   = await db_deduct_balance(cb.from_user.id, total)
    if not ok:
        u = await db_get_user(cb.from_user.id)
        bal = u["balance_uah"] if u else 0
        await send_insufficient_balance(cb, total, bal)
        return
    order_id = await db_add_order(cb.from_user.id, f"stars:{n}", total,
                                   f"stars:{n}:{recv}:{cb.from_user.id}","completed")
    await db_bump_total(cb.from_user.id, total)
    dl = (datetime.now(timezone.utc)+timedelta(minutes=30)).strftime('%H:%M UTC')
    for aid in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(aid,
                f"💰 <b>Stars (баланс)</b>\n"
                f"👤 {cb.from_user.mention_html()} (<code>{cb.from_user.id}</code>)\n"
                f"⭐ {n} Stars → <code>{recv}</code>\n"
                f"💵 <b>{total}₴</b>  |  #{order_id}\n"
                f"⏰ Відправити до: <b>{dl}</b>")
    await state.clear()
    await cb.message.answer(
        f"✅ <b>Замовлення прийнято!</b>\n\n"
        f"<blockquote>⭐ <b>{n} Stars</b> для <code>{recv}</code>\n"
        f"будуть відправлені протягом <b>30 хвилин</b>.</blockquote>\n\nДякуємо! 🙏",
        reply_markup=kb([btn("🏠  Головне меню","home")]))
    await cb.answer()

@router.callback_query(F.data=="stars_pay_crypto", BuyStars.payment)
async def stars_pay_crypto(cb:CallbackQuery, state:FSMContext):
    data = await state.get_data()
    n    = data["stars_amount"]
    recv = data["recipient"]
    price= await get_price("uah_per_star", UAH_PER_STAR)
    total= round(n*price)
    rate = await get_usd_rate()
    usd  = round(total/rate, 2)
    payload = f"stars:{n}:{recv}:{cb.from_user.id}"
    try:
        inv  = await cryptobot_create(usd,"USDT",payload,f"{n} Stars -> {recv}")
        oid  = await db_add_order(cb.from_user.id,f"stars:{n}",total,payload)
        markup = kb(
            [btn("💳  Перейти до оплати",url=inv["pay_url"])],
            [btn("✅  Я оплатив",f"check_stars:{oid}:{inv['invoice_id']}")],
            [btn("🔙  На головну","home")],
        )
        await cb.message.answer(
            f"🤖 <b>Рахунок CryptoBot створено!</b>\n\n"
            f"<blockquote>⭐ {n} Stars → <code>{recv}</code>\n"
            f"💵 <b>{usd} USDT</b>  (~{total}₴)</blockquote>\n\n"
            f"1️⃣ Натисніть «Перейти до оплати»\n2️⃣ Після оплати — «Я оплатив»",
            reply_markup=markup)
    except Exception as e:
        await cb.message.answer(f"❌ <b>Помилка:</b> <code>{e}</code>",
                                reply_markup=kb([btn("🔙  На головну","home")]))
    await cb.answer()

@router.callback_query(F.data.startswith("check_stars:"))
async def check_stars(cb:CallbackQuery, state:FSMContext, bot:Bot):
    _,oid,iid = cb.data.split(":")
    status = await cryptobot_status(int(iid))
    if status=="paid":
        await db_complete_order(int(oid))
        row = (await sb.table("orders").select("*").eq("id",oid).execute()).data[0]
        parts = row["payload"].split(":")
        qty,recv = parts[1],parts[2]
        await db_bump_total(cb.from_user.id, row["amount_uah"])
        dl = (datetime.now(timezone.utc)+timedelta(minutes=30)).strftime('%H:%M UTC')
        for aid in ADMIN_IDS:
            with suppress(Exception):
                await bot.send_message(aid,
                    f"💰 <b>Stars (CryptoBot)</b>\n"
                    f"👤 {cb.from_user.mention_html()} (<code>{cb.from_user.id}</code>)\n"
                    f"⭐ {qty} Stars → <code>{recv}</code>\n"
                    f"💵 <b>{row['amount_uah']}₴</b>  |  #{row['id']}\n"
                    f"⏰ Відправити до: <b>{dl}</b>")
        await state.clear()
        await cb.message.answer(
            f"✅ <b>Оплата підтверджена! 🎉</b>\n\n"
            f"<blockquote>⭐ <b>{qty} Stars</b> на <code>{recv}</code>\n"
            f"протягом <b>30 хвилин</b>.</blockquote>\n\nДякуємо! 🙏",
            reply_markup=kb([btn("🏠  Головне меню","home")]))
    elif status=="active":
        await cb.answer("⏳ Оплата ще не надійшла. Зачекайте трохи.",show_alert=True)
    else:
        await cb.answer("❌ Рахунок прострочений або помилка.",show_alert=True)
# ══════════════════════════════════════════════════════════════════════════
# PREMIUM FLOW
# ══════════════════════════════════════════════════════════════════════════
PREM_MONTHS=[1,3,6,12]

@router.callback_query(F.data=="buy_premium")
async def premium_start(cb:CallbackQuery, state:FSMContext):
    await state.set_state(BuyPremium.recipient)
    photo = img("premium")
    price = await get_price("uah_per_month_premium", UAH_PER_PREMIUM)
    text = (
        f"🎁 <b>Telegram Premium</b>\n\n"
        f"<blockquote>✨ Безліміт, ексклюзивні стікери та реакції\n"
        f"📱 Від <b>{round(price)}₴/місяць</b></blockquote>\n\n"
        f"Введіть <b>@username</b> отримувача або натисніть «Собі»:"
    )
    markup = kb([btn("🎁  Собі","premium_self")],[btn("🔙  На головну","home")])
    with suppress(TelegramBadRequest): await cb.message.delete()
    if photo: await cb.message.answer_photo(photo,caption=text,reply_markup=markup)
    else:     await cb.message.answer(text,reply_markup=markup)
    await cb.answer()

@router.callback_query(F.data=="premium_self", BuyPremium.recipient)
async def premium_self(cb:CallbackQuery, state:FSMContext):
    uname = cb.from_user.username or str(cb.from_user.id)
    await state.update_data(recipient=f"@{uname}")
    await _prem_months(cb, state)

@router.message(BuyPremium.recipient)
async def prem_recv(msg:Message, state:FSMContext):
    t = msg.text.strip()
    if not t.startswith("@"): t=f"@{t}"
    await state.update_data(recipient=t)
    await state.set_state(BuyPremium.months)
    price = await get_price("uah_per_month_premium", UAH_PER_PREMIUM)
    rows=_prem_rows(price)
    await msg.answer(
        f"🎁 <b>Telegram Premium</b>\n\n"
        f"<blockquote>{E['user_s']}  Отримувач: <code>{t}</code></blockquote>\n\n"
        f"Оберіть термін підписки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

def _prem_rows(price:float)->list:
    rows=[]
    for i in range(0,len(PREM_MONTHS),2):
        row=[]
        for m in PREM_MONTHS[i:i+2]:
            row.append(btn(f"🎁  {m} міс. — {round(m*price)}₴",f"premium_buy:{m}"))
        rows.append(row)
    rows.append([btn("🔙  На головну","home")])
    return rows

async def _prem_months(cb:CallbackQuery, state:FSMContext):
    data=await state.get_data()
    recv=data["recipient"]
    await state.set_state(BuyPremium.months)
    price=await get_price("uah_per_month_premium", UAH_PER_PREMIUM)
    rows=_prem_rows(price)
    text=(f"🎁 <b>Telegram Premium</b>\n\n"
          f"<blockquote>{E['user_s']}  Отримувач: <code>{recv}</code></blockquote>\n\n"
          f"Оберіть термін підписки:")
    with suppress(TelegramBadRequest): await cb.message.delete()
    photo=img("premium")
    mkp=InlineKeyboardMarkup(inline_keyboard=rows)
    if photo: await cb.message.answer_photo(photo,caption=text,reply_markup=mkp)
    else:     await cb.message.answer(text,reply_markup=mkp)
    await cb.answer()

@router.callback_query(F.data.startswith("premium_buy:"))
async def premium_buy(cb:CallbackQuery, state:FSMContext):
    months=int(cb.data.split(":")[1])
    data=await state.get_data()
    recv=data["recipient"]
    price=await get_price("uah_per_month_premium", UAH_PER_PREMIUM)
    total=round(months*price)
    await state.update_data(months=months,total_uah=total)
    await state.set_state(BuyPremium.payment)
    u=await db_get_user(cb.from_user.id)
    bal=u["balance_uah"] if u else 0
    lack=round(total-bal,2) if bal<total else 0
    text=(f"🎁 <b>Підтвердження замовлення</b>\n\n"
          f"<blockquote>✨ Premium <b>{months} міс.</b>\n"
          f"{E['user_s']}  Отримувач: <code>{recv}</code>\n"
          f"💰  До сплати: <b>{total}₴</b></blockquote>\n\n"
          f"💳 Ваш баланс: <b>{round(bal,2)}₴</b>")
    rows=[]
    if bal>=total:
        rows.append([btn(f"💳  Оплатити з балансу ({round(bal,2)}₴)","prem_pay_balance")])
    else:
        text+=f"\n❌ Не вистачає: <b>{lack}₴</b>"
        rows.append([btn(f"💰  Поповнити баланс (+{lack}₴)", f"topup_need:{total}")])
    rows.append([btn("🤖  CryptoBot (USDT)","prem_pay_crypto")])
    rows.append([btn("🔙  На головну","home")])
    with suppress(TelegramBadRequest): await cb.message.delete()
    await cb.message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data=="prem_pay_balance", BuyPremium.payment)
async def prem_pay_balance(cb:CallbackQuery, state:FSMContext, bot:Bot):
    data=await state.get_data()
    months=data["months"]; recv=data["recipient"]; total=data["total_uah"]
    ok=await db_deduct_balance(cb.from_user.id,total)
    if not ok:
        u=await db_get_user(cb.from_user.id)
        bal=u["balance_uah"] if u else 0
        await send_insufficient_balance(cb, total, bal)
        return
    payload=f"premium:{months}:{recv}:{cb.from_user.id}"
    oid=await db_add_order(cb.from_user.id,f"premium:{months}",total,payload,"completed")
    await db_bump_total(cb.from_user.id,total)
    dl=(datetime.now(timezone.utc)+timedelta(minutes=30)).strftime('%H:%M UTC')
    for aid in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(aid,
                f"💰 <b>Premium (баланс)</b>\n"
                f"👤 {cb.from_user.mention_html()}\n"
                f"✨ {months}міс. → <code>{recv}</code>\n💵 <b>{total}₴</b>  |  #{oid}\n"
                f"⏰ Активувати до: <b>{dl}</b>")
    await state.clear()
    await cb.message.answer(
        f"✅ <b>Замовлення прийнято!</b>\n\n"
        f"<blockquote>✨ <b>Telegram Premium {months} міс.</b>\n"
        f"для <code>{recv}</code> — буде активовано протягом <b>30 хвилин</b>.</blockquote>",
        reply_markup=kb([btn("🏠  Головне меню","home")]))
    await cb.answer()

@router.callback_query(F.data=="prem_pay_crypto", BuyPremium.payment)
async def prem_pay_crypto(cb:CallbackQuery, state:FSMContext):
    data=await state.get_data()
    months=data["months"]; recv=data["recipient"]; total=data["total_uah"]
    rate=await get_usd_rate(); usd=round(total/rate,2)
    payload=f"premium:{months}:{recv}:{cb.from_user.id}"
    try:
        inv=await cryptobot_create(usd,"USDT",payload,f"Premium {months}m -> {recv}")
        oid=await db_add_order(cb.from_user.id,f"premium:{months}",total,payload)
        markup=kb(
            [btn("💳  Перейти до оплати",url=inv["pay_url"])],
            [btn("✅  Я оплатив",f"check_prem:{oid}:{inv['invoice_id']}")],
            [btn("🔙  На головну","home")],)
        await cb.message.answer(
            f"🤖 <b>Рахунок CryptoBot</b>\n\n"
            f"<blockquote>✨ Premium {months}міс. → <code>{recv}</code>\n"
            f"💵 <b>{usd} USDT</b>  (~{total}₴)</blockquote>",
            reply_markup=markup)
    except Exception as e:
        await cb.message.answer(f"❌ <code>{e}</code>",
                                reply_markup=kb([btn("🔙  На головну","home")]))
    await cb.answer()

@router.callback_query(F.data.startswith("check_prem:"))
async def check_prem(cb:CallbackQuery, state:FSMContext, bot:Bot):
    _,oid,iid=cb.data.split(":")
    status=await cryptobot_status(int(iid))
    if status=="paid":
        await db_complete_order(int(oid))
        row=(await sb.table("orders").select("*").eq("id",oid).execute()).data[0]
        await db_bump_total(cb.from_user.id,row["amount_uah"])
        dl=(datetime.now(timezone.utc)+timedelta(minutes=30)).strftime('%H:%M UTC')
        for aid in ADMIN_IDS:
            with suppress(Exception):
                await bot.send_message(aid,
                    f"💰 <b>Premium (CryptoBot)</b>\n"
                    f"👤 {cb.from_user.mention_html()}\n"
                    f"📦 <code>{row['payload']}</code>\n"
                    f"💵 <b>{row['amount_uah']}₴</b>  |  #{row['id']}\n"
                    f"⏰ Активувати до: <b>{dl}</b>")
        await state.clear()
        await cb.message.answer(
            f"✅ <b>Оплата підтверджена! 🎉</b>\n\n"
            f"<blockquote>✨ <b>Telegram Premium</b> буде активовано\nпротягом <b>30 хвилин</b>.</blockquote>",
            reply_markup=kb([btn("🏠  Головне меню","home")]))
    elif status=="active":
        await cb.answer("⏳ Оплата ще не надійшла.",show_alert=True)
    else:
        await cb.answer("❌ Рахунок прострочений.",show_alert=True)

# ══════════════════════════════════════════════════════════════════════════
# TON/GRAM BUY — з перевіркою квитанції
# ══════════════════════════════════════════════════════════════════════════
@router.callback_query(F.data.startswith("ton_choose:"))
async def ton_choose(cb:CallbackQuery, state:FSMContext):
    coin = cb.data.split(":")[1]
    await state.update_data(coin=coin)
    rate_key = "ton_to_uah" if coin=="TON" else "gram_to_uah"
    default  = UAH_PER_TON if coin=="TON" else UAH_PER_GRAM
    rate     = await get_price(rate_key, default)
    icon     = "◎" if coin=="TON" else "💎"
    photo    = img("ton")
    text = (
        f"{icon} <b>Купівля {coin}</b>\n\n"
        f"<blockquote>💱  Курс: <b>1 {coin} = {rate}₴</b></blockquote>\n\n"
        f"Введіть кількість {coin}, яку хочете купити:"
    )
    await state.set_state(BuyTon.amount)
    with suppress(TelegramBadRequest): await cb.message.delete()
    mkp=kb([btn("🔙  На головну","home")])
    if photo: await cb.message.answer_photo(photo,caption=text,reply_markup=mkp)
    else:     await cb.message.answer(text,reply_markup=mkp)
    await cb.answer()

@router.message(BuyTon.amount)
async def ton_amount(msg:Message, state:FSMContext):
    try:
        n=float(msg.text.strip().replace(",",".")); 
        if n<=0: raise ValueError
    except ValueError:
        await msg.answer("❌ Введіть число більше 0!"); return
    data=await state.get_data(); coin=data["coin"]
    rate_key="ton_to_uah" if coin=="TON" else "gram_to_uah"
    default=UAH_PER_TON if coin=="TON" else UAH_PER_GRAM
    rate=await get_price(rate_key,default)
    total=round(n*rate,2)
    await state.update_data(amount=n,total_uah=total)
    await state.set_state(BuyTon.wallet)
    icon="◎" if coin=="TON" else "💎"
    await msg.answer(
        f"{icon} <b>Купівля {coin}</b>\n\n"
        f"<blockquote>{icon}  Кількість: <b>{n} {coin}</b>\n"
        f"💰  Сума: <b>{total}₴</b></blockquote>\n\n"
        f"Введіть ваш <b>{coin} гаманець</b> для отримання:",
        reply_markup=kb([btn("🔙  На головну","home")]))

@router.message(BuyTon.wallet)
async def ton_wallet(msg:Message, state:FSMContext):
    wallet=msg.text.strip()
    data=await state.get_data()
    coin=data["coin"]; n=data["amount"]; total=data["total_uah"]
    await state.update_data(wallet=wallet)
    card   = await db_get_setting("ua_card_number","—")
    holder = await db_get_setting("ua_card_holder","—")
    icon="◎" if coin=="TON" else "💎"
    text = (
        f"{icon} <b>Оплата за {n} {coin}</b>\n\n"
        f"<blockquote>💳  Переведіть <b>{total}₴</b> на картку:\n\n"
        f"<code>{card}</code>\n👤  {holder}</blockquote>\n\n"
        f"⚠️ <i>Сума має точно збігатися!</i>\n\nПісля оплати натисніть «Перевірити оплату»"
    )
    await state.set_state(BuyTon.bank_select)
    await msg.answer(text, reply_markup=kb(
        [btn("✅  Перевірити оплату","ton_check_start")],
        [btn("🔙  На головну","home")]))

@router.callback_query(F.data=="ton_check_start")
async def ton_check_start(cb:CallbackQuery, state:FSMContext):
    await state.set_state(BuyTon.bank_select)
    rows=[]
    for bid,bname in BANK_OPTIONS.items():
        rows.append([btn(bname, f"ton_bank:{bid}")])
    rows.append([btn("🔙  На головну","home")])
    await cb.message.answer(
        f"🏦 <b>Оберіть банк</b>, через який здійснили оплату:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data.startswith("ton_bank:"))
async def ton_bank_select(cb:CallbackQuery, state:FSMContext):
    bank_id=cb.data.split(":")[1]
    await state.update_data(bank_id=bank_id)
    await state.set_state(BuyTon.receipt_upload)
    data=await state.get_data()
    coin=data["coin"]; total=data["total_uah"]
    with suppress(TelegramBadRequest): await cb.message.delete()
    await cb.message.answer(
        f"📄 <b>Завантажте PDF-квитанцію</b>\n\n"
        f"<blockquote>Відкрийте додаток банку → знайдіть транзакцію "
        f"на <b>{total}₴</b> → збережіть квитанцію як PDF → надішліть сюди.</blockquote>",
        reply_markup=kb([btn("🔙  На головну","home")]))
    await cb.answer()

@router.message(BuyTon.receipt_upload, F.document)
async def ton_receipt_upload(msg:Message, state:FSMContext, bot:Bot):
    if not msg.document.file_name.lower().endswith(".pdf"):
        await msg.answer("❌ Потрібен PDF файл!"); return
    data=await state.get_data()
    coin=data["coin"]; amount=data["amount"]; total=data["total_uah"]
    wallet=data["wallet"]; bank_id=data.get("bank_id","monobank")
    proc = await msg.answer("🔍 Перевіряю квитанцію...")
    file=await bot.get_file(msg.document.file_id)
    bio=io.BytesIO(); await bot.download_file(file.file_path, bio)
    pdf_bytes=bio.getvalue()
    receipt_code=extract_receipt_code(pdf_bytes)
    if not receipt_code:
        with suppress(TelegramBadRequest): await proc.delete()
        await msg.answer("❌ <b>Не вдалося знайти код квитанції в PDF</b>\n\nПереконайтеся що завантажуєте офіційну квитанцію з банку.",
            reply_markup=kb([btn("🔄  Спробувати знову","ton_check_start"),btn("🔙  На головну","home")])); return
    if await db_receipt_used(receipt_code):
        with suppress(TelegramBadRequest): await proc.delete()
        await msg.answer("❌ <b>Ця квитанція вже використана!</b>",
            reply_markup=kb([btn("🔙  На головну","home")])); return
    result=await check_gov_receipt(bank_id, receipt_code)
    paid=False; rec_amount=0.0; payer=""
    if result:
        status_val=(result.get("status") or result.get("state") or "").lower()
        paid = status_val in ("paid","success","оплачена","completed")
        rec_amount=float(result.get("amount") or result.get("total") or result.get("sum") or 0)
        payer=(result.get("payer") or result.get("sender") or result.get("payerName") or result.get("clientName") or "")
    if not paid:
        with suppress(TelegramBadRequest): await proc.delete()
        await msg.answer(
            f"❌ <b>Квитанція не підтверджена</b>\n\n<blockquote>Код: <code>{receipt_code}</code></blockquote>\nЗверніться до підтримки.",
            reply_markup=kb([btn("💬  Підтримка","support"),btn("🔙  На головну","home")])); return
    if rec_amount > 0 and abs(rec_amount - total) / total > 0.05:
        with suppress(TelegramBadRequest): await proc.delete()
        await msg.answer(
            f"❌ <b>Сума не збігається!</b>\n\n<blockquote>Очікувалось: <b>{total}₴</b>\nЗнайдено: <b>{rec_amount}₴</b></blockquote>",
            reply_markup=kb([btn("🔙  На головну","home")])); return
    expected_initials=await db_get_setting("ua_card_initials","")
    if expected_initials and payer:
        first_letter=expected_initials.split()[0][0].upper() if expected_initials else ""
        if first_letter and first_letter not in payer.upper():
            with suppress(TelegramBadRequest): await proc.delete()
            await msg.answer("❌ <b>ПІБ платника не збігається!</b>",
                reply_markup=kb([btn("🔙  На головну","home")])); return
    await db_mark_receipt(receipt_code, bank_id, rec_amount or total, msg.from_user.id)
    send_id=await db_add_pending_send(msg.from_user.id, coin, wallet, amount, total)
    icon="◎" if coin=="TON" else "💎"
    dl=(datetime.now(timezone.utc)+timedelta(minutes=30)).strftime('%H:%M UTC')
    for aid in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(aid,
                f"🚀 <b>Запит на відправку {coin}!</b>\n\n"
                f"👤 {msg.from_user.mention_html()} (<code>{msg.from_user.id}</code>)\n"
                f"{icon}  <b>{amount} {coin}</b>\n📬  Гаманець: <code>{wallet}</code>\n"
                f"💵  Сплачено: <b>{total}₴</b>\n🧾  Квитанція: <code>{receipt_code}</code>\n"
                f"🏦  Банк: {BANK_OPTIONS.get(bank_id,bank_id)}\n"
                f"⏰  Відправити до: <b>{dl}</b>\n📋  ID: #{send_id}",
                reply_markup=kb([btn(f"✅ Підтвердити відправку #{send_id}",f"admin_confirm_send:{send_id}")]))
    with suppress(TelegramBadRequest): await proc.delete()
    await state.clear()
    await msg.answer(
        f"✅ <b>Оплату підтверджено!</b>\n\n"
        f"<blockquote>{icon}  <b>{amount} {coin}</b> буде відправлено\n"
        f"на гаманець <code>{wallet}</code>\nпротягом <b>30 хвилин</b>.</blockquote>\n\nДякуємо! 🙏",
        reply_markup=kb([btn("🏠  Головне меню","home")]))
# ══════════════════════════════════════════════════════════════════════════
# ПОПОВНЕННЯ БАЛАНСУ — РОЗУМНИЙ ФЛОУ
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "topup")
async def topup_start(cb:CallbackQuery, state:FSMContext):
    await state.clear()
    with suppress(TelegramBadRequest): await cb.message.delete()
    await cb.message.answer(
        f"{E['money']} <b>Поповнення балансу</b>\n\n"
        f"<blockquote>Оберіть зручний спосіб поповнення:</blockquote>",
        reply_markup=_topup_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("topup_need:"))
async def topup_need(cb:CallbackQuery, state:FSMContext):
    """Кнопка «Поповнити баланс (+X₴)» з екрану підтвердження замовлення."""
    need = float(cb.data.split(":")[1])
    await state.update_data(topup_return_need=need)
    with suppress(TelegramBadRequest): await cb.message.delete()
    await cb.message.answer(
        f"{E['money']} <b>Поповнення балансу</b>\n\n"
        f"<blockquote>Потрібно поповнити мінімум на <b>{round(need,2)}₴</b>\n\n"
        f"Оберіть зручний спосіб:</blockquote>",
        reply_markup=_topup_markup())
    await cb.answer()

# ── UAH через картку + PDF квитанція ─────────────────────────────────────

@router.callback_query(F.data == "topup_uah")
async def topup_uah_start(cb:CallbackQuery, state:FSMContext):
    card   = await db_get_setting("ua_card_number","—")
    holder = await db_get_setting("ua_card_holder","—")
    data   = await state.get_data()
    need   = data.get("topup_return_need", 0)
    hint   = f"\n💡 Рекомендована сума: <b>{round(need,2)}₴</b>" if need else ""
    await cb.message.answer(
        f"💳 <b>Поповнення через UAH</b>\n\n"
        f"<blockquote>Переведіть будь-яку суму на картку:\n\n"
        f"<code>{card}</code>\n👤  {holder}\n\n"
        f"⚠️ Мінімум — <b>50₴</b>{hint}</blockquote>\n\n"
        f"Після переказу натисніть «Я переказав»:",
        reply_markup=kb(
            [btn("✅  Я переказав, перевірити","topup_uah_select_bank")],
            [btn("🔙  Назад","topup")]))
    await cb.answer()

@router.callback_query(F.data == "topup_uah_select_bank")
async def topup_uah_select_bank(cb:CallbackQuery, state:FSMContext):
    await state.set_state(TopUp.bank_select)
    rows=[]
    for bid, bname in BANK_OPTIONS.items():
        rows.append([btn(bname, f"topup_bank:{bid}")])
    rows.append([btn("🔙  Назад","topup_uah")])
    await cb.message.answer(
        "🏦 <b>Оберіть банк</b>, через який здійснили переказ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data.startswith("topup_bank:"))
async def topup_bank(cb:CallbackQuery, state:FSMContext):
    bank_id   = cb.data.split(":")[1]
    bank_name = BANK_OPTIONS.get(bank_id, bank_id)
    await state.update_data(bank_id=bank_id)
    await state.set_state(TopUp.receipt_upload)
    # Видаляємо повідомлення з вибором банку
    with suppress(TelegramBadRequest): await cb.message.delete()
    card   = await db_get_setting("ua_card_number","—")
    holder = await db_get_setting("ua_card_holder","—")
    data   = await state.get_data()
    need   = data.get("topup_return_need", 0)
    hint   = f"<b>{round(need,2)}₴</b>" if need else "будь-яку суму ≥ 50₴"
    await cb.message.answer(
        f"📄 <b>Надішліть PDF-квитанцію</b>\n\n"
        f"<blockquote>🏦 Банк: {bank_name}\n"
        f"💳 Карта: <code>{card}</code>\n"
        f"👤 Одержувач: {holder}\n"
        f"💰 Сума: {hint}</blockquote>\n\n"
        f"Відкрийте додаток банку → знайдіть транзакцію → "
        f"збережіть квитанцію як PDF → надішліть сюди.",
        reply_markup=kb([btn("🔙  Назад","topup_uah_select_bank")]))
    await cb.answer()

@router.message(TopUp.receipt_upload, F.document)
async def topup_receipt(msg:Message, state:FSMContext, bot:Bot):
    if not msg.document.file_name.lower().endswith(".pdf"):
        await msg.answer("❌ Потрібен PDF файл!"); return
    data    = await state.get_data()
    bank_id = data.get("bank_id","monobank")
    proc    = await msg.answer("🔍 Перевіряю квитанцію...")
    file    = await bot.get_file(msg.document.file_id)
    bio     = io.BytesIO()
    await bot.download_file(file.file_path, bio)
    pdf_bytes = bio.getvalue()
    receipt_code = extract_receipt_code(pdf_bytes)
    if not receipt_code:
        with suppress(TelegramBadRequest): await proc.delete()
        await msg.answer(
            "❌ <b>Код квитанції не знайдено в PDF</b>\n\nПереконайтеся що це офіційна квитанція банку.",
            reply_markup=kb([btn("🔄  Спробувати","topup_uah_select_bank"),btn("🔙  На головну","home")])); return
    if await db_receipt_used(receipt_code):
        with suppress(TelegramBadRequest): await proc.delete()
        await msg.answer("❌ <b>Ця квитанція вже використана!</b>",
            reply_markup=kb([btn("🔙  На головну","home")])); return
    result = await check_gov_receipt(bank_id, receipt_code)
    paid = False; rec_amount = 0.0
    if result:
        status_val = (result.get("status") or result.get("state") or "").lower()
        paid = status_val in ("paid","success","оплачена","completed")
        rec_amount = float(result.get("amount") or result.get("total") or result.get("sum") or 0)
    if not paid:
        with suppress(TelegramBadRequest): await proc.delete()
        await msg.answer(
            f"❌ <b>Квитанція не підтверджена</b>\n\n<blockquote>Код: <code>{receipt_code}</code></blockquote>\nЗверніться до підтримки.",
            reply_markup=kb([btn("💬  Підтримка","support"),btn("🔙  На головну","home")])); return
    if rec_amount < 50:
        with suppress(TelegramBadRequest): await proc.delete()
        await msg.answer(
            f"❌ Мінімальна сума — <b>50₴</b>\nЗнайдено: <b>{rec_amount}₴</b>",
            reply_markup=kb([btn("🔙  На головну","home")])); return
    # ✅ Зараховуємо
    await db_mark_receipt(receipt_code, bank_id, rec_amount, msg.from_user.id)
    await db_add_balance(msg.from_user.id, rec_amount)
    u = await db_get_user(msg.from_user.id)
    new_bal = u["balance_uah"] if u else rec_amount
    for aid in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(aid,
                f"💰 <b>Поповнення (UAH картка)</b>\n"
                f"👤 {msg.from_user.mention_html()} (<code>{msg.from_user.id}</code>)\n"
                f"💵 +<b>{rec_amount}₴</b>\n"
                f"🧾 <code>{receipt_code}</code>  |  {BANK_OPTIONS.get(bank_id,bank_id)}")
    with suppress(TelegramBadRequest): await proc.delete()
    await state.clear()
    await msg.answer(
        f"✅ <b>Баланс поповнено!</b>\n\n"
        f"<blockquote>💰 Зараховано: <b>+{rec_amount}₴</b>\n"
        f"💳 Поточний баланс: <b>{round(new_bal,2)}₴</b></blockquote>\n\n"
        f"Тепер можете оплатити замовлення з балансу! 🎉",
        reply_markup=kb([btn("🏠  Головне меню","home")]))

# ── CryptoBot поповнення ──────────────────────────────────────────────────

@router.callback_query(F.data == "topup_crypto")
async def topup_crypto_start(cb:CallbackQuery, state:FSMContext):
    data = await state.get_data()
    need = data.get("topup_return_need", 0)
    hint = f"\n\n💡 Рекомендована сума: <b>{round(need,2)}₴</b>" if need else ""
    await cb.message.answer(
        f"🤖 <b>Поповнення через CryptoBot (USDT)</b>{hint}\n\n"
        f"Введіть суму в <b>гривнях (₴)</b>:",
        reply_markup=kb([btn("🔙  Назад","topup")]))
    await state.set_state(TopUp.amount_input)
    await cb.answer()

@router.message(TopUp.amount_input)
async def topup_crypto_amount(msg:Message, state:FSMContext):
    try:
        amount_uah = float(msg.text.strip().replace(",","."))
        if amount_uah < 50: raise ValueError
    except ValueError:
        await msg.answer("❌ Введіть суму в гривнях (мінімум 50₴)!"); return
    rate = await get_usd_rate()
    amount_usdt = round(amount_uah / rate, 2)
    payload = f"topup:{msg.from_user.id}:{amount_uah}"
    try:
        inv = await cryptobot_create(
            amount_usdt, "USDT", payload,
            f"Grizz Shop поповнення — {amount_uah}грн")
        await db_create_crypto_topup(msg.from_user.id, inv["invoice_id"], amount_usdt, amount_uah)
        markup = kb(
            [btn("💳  Перейти до оплати", url=inv["pay_url"])],
            [btn("✅  Я оплатив", f"topup_crypto_check:{inv['invoice_id']}")],
            [btn("🔙  На головну","home")],
        )
        await msg.answer(
            f"🤖 <b>Рахунок CryptoBot створено!</b>\n\n"
            f"<blockquote>💰 Сума: <b>{amount_uah}₴</b>\n"
            f"💵 До оплати: <b>{amount_usdt} USDT</b>\n"
            f"💱 Курс: <b>{rate:.1f}₴/USDT</b></blockquote>\n\n"
            f"1️⃣ Перейдіть до оплати\n2️⃣ Оплатіть у CryptoBot\n3️⃣ Поверніться і натисніть «Я оплатив»",
            reply_markup=markup)
        await state.clear()
    except Exception as e:
        await msg.answer(f"❌ <b>Помилка CryptoBot:</b> <code>{e}</code>",
                         reply_markup=kb([btn("🔙  На головну","home")]))

@router.callback_query(F.data.startswith("topup_crypto_check:"))
async def topup_crypto_check(cb:CallbackQuery, bot:Bot):
    invoice_id = int(cb.data.split(":")[1])
    # Захист від подвійного нарахування
    topup = await db_get_crypto_topup(invoice_id)
    if not topup:
        await cb.answer("⚠️ Цей рахунок вже зарахований або не знайдений.", show_alert=True)
        return
    status = await cryptobot_status(invoice_id)
    if status == "paid":
        await db_complete_crypto_topup(topup["id"])
        await db_add_balance(topup["user_id"], topup["amount_uah"])
        u = await db_get_user(topup["user_id"])
        new_bal = u["balance_uah"] if u else topup["amount_uah"]
        for aid in ADMIN_IDS:
            with suppress(Exception):
                await bot.send_message(aid,
                    f"💰 <b>Поповнення (CryptoBot)</b>\n"
                    f"👤 {cb.from_user.mention_html()} (<code>{cb.from_user.id}</code>)\n"
                    f"💵 +<b>{topup['amount_uah']}₴</b>  ({topup['amount_usdt']} USDT)")
        await cb.message.answer(
            f"✅ <b>Баланс поповнено!</b>\n\n"
            f"<blockquote>💰 Зараховано: <b>+{topup['amount_uah']}₴</b>\n"
            f"💳 Поточний баланс: <b>{round(new_bal,2)}₴</b></blockquote>\n\n"
            f"Тепер можете оплатити замовлення з балансу! 🎉",
            reply_markup=kb([btn("🏠  Головне меню","home")]))
        await cb.answer()
    elif status == "active":
        await cb.answer("⏳ Оплата ще не надійшла. Зачекайте та спробуйте знову.", show_alert=True)
    else:
        await cb.answer("❌ Рахунок прострочений. Створіть новий.", show_alert=True)

# ── TON поповнення через blockchain ──────────────────────────────────────

@router.callback_query(F.data == "topup_ton")
async def topup_ton_start(cb:CallbackQuery, state:FSMContext):
    ton_addr = await db_get_setting("admin_ton_address","—")
    ton_rate = await get_price("ton_to_uah", UAH_PER_TON)
    uid      = cb.from_user.id
    data     = await state.get_data()
    need     = data.get("topup_return_need", 0)
    comment  = str(uid)  # коментар = Telegram ID (унікальний)
    min_ton  = round(max(0.1, need / ton_rate), 4) if need else 0.1
    hint     = f"\n💡 Рекомендована сума: <b>{min_ton} TON</b> (~{round(min_ton*ton_rate)}₴)" if need else ""
    # Зберігаємо pending запис (або повертаємо існуючий)
    await db_create_ton_topup(uid, comment, min_ton, round(min_ton*ton_rate, 2))
    await state.set_state(TopUp.ton_wait)
    await cb.message.answer(
        f"◎ <b>Поповнення через TON</b>{hint}\n\n"
        f"<blockquote>Адреса гаманця:\n<code>{ton_addr}</code>\n\n"
        f"⚠️ <b>ОБОВ'ЯЗКОВО</b> вкажіть у коментарі:\n<code>{comment}</code>\n\n"
        f"💱 Курс: 1 TON = {ton_rate}₴\n📊 Мінімум: 0.1 TON</blockquote>\n\n"
        f"Після відправки натисніть «Я відправив» — бот перевірить транзакцію автоматично.",
        reply_markup=kb(
            [btn("✅  Я відправив TON", f"topup_ton_check:{comment}")],
            [btn("🔙  Назад","topup")]))
    await cb.answer()

@router.callback_query(F.data.startswith("topup_ton_check:"))
async def topup_ton_check(cb:CallbackQuery, bot:Bot):
    comment = cb.data.split(":",1)[1]
    # Захист від подвійного нарахування
    topup = await db_get_ton_topup_by_comment(comment)
    if not topup:
        await cb.answer("⚠️ Цей запит вже зарахований або не знайдений.", show_alert=True)
        return
    checking = await cb.message.answer("🔍 Перевіряю транзакцію в TON blockchain...")
    await cb.answer()
    ton_addr = await db_get_setting("admin_ton_address","")
    ton_rate = await get_price("ton_to_uah", UAH_PER_TON)
    tx = await check_ton_transaction_by_comment(ton_addr, comment)
    with suppress(TelegramBadRequest): await checking.delete()
    if not tx:
        await cb.message.answer(
            f"❌ <b>Транзакцію не знайдено</b>\n\n"
            f"<blockquote>Переконайтеся що:\n"
            f"• Адреса гаманця правильна\n"
            f"• В коментарі вказано: <code>{comment}</code>\n"
            f"• Пройшло достатньо часу (1-3 хвилини)</blockquote>",
            reply_markup=kb(
                [btn("🔄  Перевірити знову", f"topup_ton_check:{comment}")],
                [btn("💬  Підтримка","support")],
                [btn("🔙  На головну","home")]))
        return
    # Знайдено!
    amount_ton = tx["amount_ton"]
    amount_uah = round(amount_ton * ton_rate, 2)
    await db_complete_ton_topup(topup["id"])
    await db_add_balance(topup["user_id"], amount_uah)
    u = await db_get_user(topup["user_id"])
    new_bal = u["balance_uah"] if u else amount_uah
    for aid in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(aid,
                f"💰 <b>Поповнення (TON)</b>\n"
                f"👤 {cb.from_user.mention_html()} (<code>{cb.from_user.id}</code>)\n"
                f"◎ {amount_ton} TON  →  +<b>{amount_uah}₴</b>\n"
                f"🔖 Коментар: <code>{comment}</code>")
    await cb.message.answer(
        f"✅ <b>Баланс поповнено!</b>\n\n"
        f"<blockquote>◎ Отримано: <b>{amount_ton} TON</b>\n"
        f"💰 Зараховано: <b>+{amount_uah}₴</b>\n"
        f"💳 Поточний баланс: <b>{round(new_bal,2)}₴</b></blockquote>\n\n"
        f"Тепер можете оплатити замовлення з балансу! 🎉",
        reply_markup=kb([btn("🏠  Головне меню","home")]))
# ══════════════════════════════════════════════════════════════════════════
# ПРОФІЛЬ / ПІДТРИМКА
# ══════════════════════════════════════════════════════════════════════════
@router.callback_query(F.data=="profile")
async def profile_cb(cb:CallbackQuery, state:FSMContext):
    u=await db_get_user(cb.from_user.id)
    if not u:
        u=await db_ensure_user(cb.from_user.id, cb.from_user.username or "", cb.from_user.first_name or "")
    rank=await db_rank(cb.from_user.id)
    total=u["total_bought_uah"]; created=u.get("created_at","")[:10]
    rank_str={1:"🥇 1 місце",2:"🥈 2 місце",3:"🥉 3 місце"}.get(rank,f"#{rank} місце")
    text=(
        f"{E['user_p']} <b>Особистий кабінет</b>\n\n"
        f"{E['id_ico']}  Твій ID: <code>{cb.from_user.id}</code>\n"
        f"{E['calendar']}  В боті з: <b>{created}</b>\n\n"
        f"<blockquote>"
        f"{E['money']}  Ваш баланс: <b>{round(u['balance_uah'],2)}₴</b>\n"
        f"{E['star_bal']}  Витрачено: <b>{round(total,2)}₴</b>\n"
        f"{E['trophy']}  Рейтинг: <b>{rank_str}</b>"
        f"</blockquote>")
    markup=kb(
        [btn("⬆️  Вивести","withdraw"), btn("ℹ️  Ціни","info_prices")],
        [btn("📁  Мої замовлення","my_orders")],
        [btn("💰  Поповнити баланс","topup")],
        [btn("🔙  На головну","home")])
    photo=img("profile")
    with suppress(TelegramBadRequest): await cb.message.delete()
    if photo: await cb.message.answer_photo(photo,caption=text,reply_markup=markup)
    else:     await cb.message.answer(text,reply_markup=markup)
    await cb.answer()

@router.callback_query(F.data=="my_orders")
async def my_orders(cb:CallbackQuery):
    orders=(await sb.table("orders").select("*").eq("user_id",cb.from_user.id)
            .order("created_at",desc=True).limit(10).execute()).data
    if not orders:
        await cb.answer("📁 Замовлень ще немає.",show_alert=True); return
    lines=[]
    for o in orders:
        ic="✅" if o["status"]=="completed" else "⏳"
        lines.append(f"{ic}  {o['product']}  —  <b>{o['amount_uah']}₴</b>\n     <i>{o['created_at'][:10]}</i>")
    await cb.message.answer(
        f"📁 <b>Ваші замовлення:</b>\n\n" + "\n\n".join(lines),
        reply_markup=kb([btn("🔙  Назад","profile")]))
    await cb.answer()

@router.callback_query(F.data=="withdraw")
async def withdraw(cb:CallbackQuery):
    await cb.message.answer(
        f"{E['up']} <b>Виведення коштів</b>\n\n"
        f"<blockquote>Функція в розробці.\nЗверніться до підтримки для виведення.</blockquote>",
        reply_markup=kb([btn("💬  Підтримка","support"),btn("🔙  Назад","profile")]))
    await cb.answer()

@router.callback_query(F.data=="info_prices")
async def info_prices(cb:CallbackQuery):
    star=await get_price("uah_per_star",UAH_PER_STAR)
    prem=await get_price("uah_per_month_premium",UAH_PER_PREMIUM)
    ton =await get_price("ton_to_uah",UAH_PER_TON)
    gram=await get_price("gram_to_uah",UAH_PER_GRAM)
    rate=await get_usd_rate()
    await cb.message.answer(
        f"ℹ️ <b>Актуальні ціни</b>\n\n"
        f"<blockquote>"
        f"⭐  1 Star           —  <b>{star}₴</b>\n"
        f"🎁  Premium 1 міс.  —  <b>{prem}₴</b>\n"
        f"◎  1 TON            —  <b>{ton}₴</b>\n"
        f"💎  1 GRAM           —  <b>{gram}₴</b>\n\n"
        f"💱  Курс USDT/UAH:   <b>{rate:.1f}₴</b>"
        f"</blockquote>",
        reply_markup=kb([btn("🔙  Назад","profile")]))
    await cb.answer()

@router.callback_query(F.data=="support")
async def support_start(cb:CallbackQuery, state:FSMContext):
    await state.set_state(Support.ticket)
    with suppress(TelegramBadRequest): await cb.message.delete()
    await cb.message.answer(
        f"💬 <b>Підтримка Grizz Shop</b>\n\n"
        f"<blockquote>Опишіть проблему одним повідомленням.\n\n"
        f"Для швидкого вирішення вкажіть:\n"
        f"1️⃣  Скріншот або переслане повідомлення\n"
        f"2️⃣  Час коли здійснювали оплату\n"
        f"3️⃣  Суму оплати</blockquote>",
        reply_markup=kb([btn("🔙  На головну","home")]))
    await cb.answer()

@router.message(Support.ticket)
async def support_ticket(msg:Message, state:FSMContext, bot:Bot):
    text=msg.text or msg.caption or "(медіа без тексту)"
    for aid in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(aid,
                f"🎫 <b>Тікет підтримки</b>\n"
                f"👤 {msg.from_user.mention_html()} (<code>{msg.from_user.id}</code>)\n\n"
                f"<blockquote>{text}</blockquote>",
                reply_markup=kb([btn(f"↩️  Відповісти","admin_reply_uid:"+str(msg.from_user.id))]))
    await state.clear()
    await msg.answer(
        f"✅ <b>Тікет відправлено!</b>\n\n<blockquote>Відповімо найближчим часом. Дякуємо!</blockquote>",
        reply_markup=kb([btn("🏠  Головне меню","home")]))

# ══════════════════════════════════════════════════════════════════════════
# АДМІН ПАНЕЛЬ
# ══════════════════════════════════════════════════════════════════════════
def _admin_home_mkp()->InlineKeyboardMarkup:
    return kb(
        [btn("💰  Ціни","adm_prices"), btn("💳  Реквізити","adm_requisites")],
        [btn("👥  Статистика","adm_stats"), btn("📋  Замовлення","adm_orders")],
        [btn("💸  Видати баланс","adm_give_bal"), btn("📤  Розсилка","adm_broadcast")],
        [btn("✉️  Написати юзеру","adm_manual"), btn("⏳  Очікують відправки","adm_pending")],
        [btn("◎  TON Гаманець","adm_ton_wallet"), btn("💎  GRAM Гаманець","adm_gram_wallet")],
        [btn("🔙  На головну","home")])

@router.message(Command("admin"))
async def admin_cmd(msg:Message, state:FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.clear()
    await msg.answer(f"⚙️ <b>Адмін-панель</b>",reply_markup=_admin_home_mkp())

@router.callback_query(F.data=="admin_home")
async def admin_home_cb(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Немає доступу",show_alert=True); return
    await state.clear()
    with suppress(TelegramBadRequest):
        await cb.message.edit_text("⚙️ <b>Адмін-панель</b>",reply_markup=_admin_home_mkp())
    await cb.answer()

@router.callback_query(F.data=="admin_back")
async def admin_back(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.clear()
    with suppress(TelegramBadRequest):
        await cb.message.edit_text("⚙️ <b>Адмін-панель</b>",reply_markup=_admin_home_mkp())
    await cb.answer()

@router.callback_query(F.data=="adm_prices")
async def adm_prices(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return
    star=await db_get_setting("uah_per_star") or str(UAH_PER_STAR)
    prem=await db_get_setting("uah_per_month_premium") or str(UAH_PER_PREMIUM)
    ton =await db_get_setting("ton_to_uah") or str(UAH_PER_TON)
    gram=await db_get_setting("gram_to_uah") or str(UAH_PER_GRAM)
    markup=kb(
        [btn(f"⭐ Star {star}₴ → змінити","set_price:star")],
        [btn(f"🎁 Prem {prem}₴ → змінити","set_price:premium")],
        [btn(f"◎ TON {ton}₴ → змінити","set_price:ton")],
        [btn(f"💎 GRAM {gram}₴ → змінити","set_price:gram")],
        [btn("🔙  Назад","admin_back")])
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(
            f"💰 <b>Поточні ціни:</b>\n\n⭐ Star: <b>{star}₴</b>\n🎁 Prem/міс: <b>{prem}₴</b>\n◎ TON: <b>{ton}₴</b>\n💎 GRAM: <b>{gram}₴</b>",
            reply_markup=markup)
    await cb.answer()

@router.callback_query(F.data.startswith("set_price:"))
async def set_price_start(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    prod=cb.data.split(":")[1]
    await state.set_state(Admin.set_price); await state.update_data(price_prod=prod)
    names={"star":"1 Star","premium":"Premium/міс","ton":"1 TON","gram":"1 GRAM"}
    await cb.message.answer(f"✏️ Введіть нову ціну в ₴ для <b>{names.get(prod,prod)}</b>:",
        reply_markup=kb([btn("❌  Скасувати","adm_prices")]))
    await cb.answer()

@router.message(Admin.set_price)
async def set_price_input(msg:Message, state:FSMContext):
    if not is_admin(msg.from_user.id): return
    try: val=float(msg.text.strip().replace(",","."))
    except ValueError:
        await msg.answer("❌ Введіть число!"); return
    data=await state.get_data()
    keys={"star":"uah_per_star","premium":"uah_per_month_premium","ton":"ton_to_uah","gram":"gram_to_uah"}
    await db_set_setting(keys[data["price_prod"]],str(val))
    await state.clear()
    await msg.answer(f"✅ Ціну оновлено: <b>{val}₴</b>",reply_markup=kb([btn("🔙  До цін","adm_prices")]))

@router.callback_query(F.data=="adm_requisites")
async def adm_requisites(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return
    card=await db_get_setting("ua_card_number","—")
    holder=await db_get_setting("ua_card_holder","—")
    initials=await db_get_setting("ua_card_initials","—")
    markup=kb(
        [btn("💳  Змінити номер картки","set_req:card")],
        [btn("👤  Змінити ПІБ (латиниця)","set_req:name")],
        [btn("✍️  Змінити ініціали (кирилиця)","set_req:initials")],
        [btn("🔙  Назад","admin_back")])
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(
            f"💳 <b>Реквізити для UAH оплат</b>\n\n"
            f"<blockquote>Номер картки: <code>{card}</code>\nПІБ: <b>{holder}</b>\nІніціали: <b>{initials}</b></blockquote>",
            reply_markup=markup)
    await cb.answer()

@router.callback_query(F.data.startswith("set_req:"))
async def set_req_start(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    field=cb.data.split(":")[1]
    state_map={"card":Admin.set_req_card,"name":Admin.set_req_name,"initials":Admin.set_req_initials}
    prompts={"card":"Введіть новий номер картки (напр. 4441 1111 2222 3333):","name":"Введіть ПІБ латиницею:","initials":"Введіть ініціали кирилицею (напр. Микита П.):"}
    await state.set_state(state_map[field]); await state.update_data(req_field=field)
    await cb.message.answer(prompts[field],reply_markup=kb([btn("❌  Скасувати","adm_requisites")]))
    await cb.answer()

@router.message(Admin.set_req_card)
async def set_req_card(msg:Message, state:FSMContext):
    if not is_admin(msg.from_user.id): return
    await db_set_setting("ua_card_number",msg.text.strip())
    await state.clear()
    await msg.answer("✅ Номер картки оновлено!",reply_markup=kb([btn("🔙  До реквізитів","adm_requisites")]))

@router.message(Admin.set_req_name)
async def set_req_name(msg:Message, state:FSMContext):
    if not is_admin(msg.from_user.id): return
    await db_set_setting("ua_card_holder",msg.text.strip().upper())
    await state.clear()
    await msg.answer("✅ ПІБ оновлено!",reply_markup=kb([btn("🔙  До реквізитів","adm_requisites")]))

@router.message(Admin.set_req_initials)
async def set_req_initials(msg:Message, state:FSMContext):
    if not is_admin(msg.from_user.id): return
    await db_set_setting("ua_card_initials",msg.text.strip())
    await state.clear()
    await msg.answer("✅ Ініціали оновлено!",reply_markup=kb([btn("🔙  До реквізитів","adm_requisites")]))

@router.callback_query(F.data=="adm_ton_wallet")
async def adm_ton_wallet(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    addr=await db_get_setting("admin_ton_address","—")
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(
            f"◎ <b>TON Гаманець</b>\n\n<code>{addr}</code>\n\nНа цей гаманець покупці відправляють TON для поповнення балансу.",
            reply_markup=kb([btn("✏️  Змінити адресу","set_ton_addr")],[btn("🔙  Назад","admin_back")]))
    await cb.answer()

@router.callback_query(F.data=="set_ton_addr")
async def set_ton_addr_start(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(Admin.set_ton_addr)
    await cb.message.answer("Введіть нову TON-адресу гаманця:",reply_markup=kb([btn("❌  Скасувати","adm_ton_wallet")]))
    await cb.answer()

@router.message(Admin.set_ton_addr)
async def set_ton_addr(msg:Message, state:FSMContext):
    if not is_admin(msg.from_user.id): return
    await db_set_setting("admin_ton_address",msg.text.strip())
    await state.clear()
    await msg.answer("✅ TON-адреса оновлена!",reply_markup=kb([btn("🔙  До гаманця","adm_ton_wallet")]))

@router.callback_query(F.data=="adm_gram_wallet")
async def adm_gram_wallet(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    addr=await db_get_setting("admin_gram_address","—")
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(f"💎 <b>GRAM Гаманець</b>\n\n<code>{addr}</code>",
            reply_markup=kb([btn("✏️  Змінити адресу","set_gram_addr")],[btn("🔙  Назад","admin_back")]))
    await cb.answer()

@router.callback_query(F.data=="set_gram_addr")
async def set_gram_addr_start(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(Admin.set_gram_addr)
    await cb.message.answer("Введіть нову GRAM-адресу:",reply_markup=kb([btn("❌  Скасувати","adm_gram_wallet")]))
    await cb.answer()

@router.message(Admin.set_gram_addr)
async def set_gram_addr(msg:Message, state:FSMContext):
    if not is_admin(msg.from_user.id): return
    await db_set_setting("admin_gram_address",msg.text.strip())
    await state.clear()
    await msg.answer("✅ GRAM-адреса оновлена!",reply_markup=kb([btn("🔙  До гаманця","adm_gram_wallet")]))
# ══════════════════════════════════════════════════════════════════════════
# АДМІН — STATS / ORDERS / PENDING / BROADCAST / MANUAL
# ══════════════════════════════════════════════════════════════════════════
@router.callback_query(F.data=="adm_stats")
async def adm_stats(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return
    cnt=(await sb.table("users").select("user_id",count="exact").execute()).count or 0
    orders_r=(await sb.table("orders").select("amount_uah").eq("status","completed").execute()).data
    total_sum=sum(o["amount_uah"] for o in orders_r) if orders_r else 0
    pending_orders=(await sb.table("orders").select("id",count="exact").eq("status","pending").execute()).count or 0
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(
            f"👥 <b>Статистика</b>\n\n"
            f"<blockquote>"
            f"👥  Користувачів: <b>{cnt}</b>\n"
            f"💰  Загальний оборот: <b>{round(total_sum,2)}₴</b>\n"
            f"⏳  Замовлень в очікуванні: <b>{pending_orders}</b>"
            f"</blockquote>",
            reply_markup=kb([btn("🔙  Назад","admin_back")]))
    await cb.answer()

@router.callback_query(F.data=="adm_orders")
async def adm_orders(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return
    orders=(await sb.table("orders").select("*").order("created_at",desc=True).limit(15).execute()).data
    lines=[]
    for o in orders:
        ic="✅" if o["status"]=="completed" else "⏳"
        dl=""
        if o.get("deadline") and o["status"]=="pending":
            dl=f" ⏰{o['deadline'][11:16]}"
        lines.append(f"{ic} <b>#{o['id']}</b> {o['product']} <b>{o['amount_uah']}₴</b> {o['created_at'][:10]}{dl}")
    text="\n".join(lines) if lines else "Замовлень немає."
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(f"📋 <b>Останні замовлення:</b>\n\n{text}",
            reply_markup=kb([btn("🔙  Назад","admin_back")]))
    await cb.answer()

@router.callback_query(F.data=="adm_give_bal")
async def adm_give_bal(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(Admin.give_uid)
    await cb.message.answer("Введіть user_id користувача:",reply_markup=kb([btn("❌  Скасувати","admin_back")]))
    await cb.answer()

@router.callback_query(F.data.startswith("admin_give_uid:"))
async def admin_give_uid_cb(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    uid=int(cb.data.split(":")[1])
    await state.set_state(Admin.give_amount); await state.update_data(give_uid=uid)
    u=await db_get_user(uid); bal=u["balance_uah"] if u else 0
    await cb.message.answer(
        f"Користувач <code>{uid}</code>\nПоточний баланс: <b>{round(bal,2)}₴</b>\n\nВведіть суму (₴):",
        reply_markup=kb([btn("❌  Скасувати","admin_back")]))
    await cb.answer()

@router.message(Admin.give_uid)
async def adm_give_uid_input(msg:Message, state:FSMContext):
    if not is_admin(msg.from_user.id): return
    try: uid=int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ Введіть числовий user_id!"); return
    await state.update_data(give_uid=uid); await state.set_state(Admin.give_amount)
    u=await db_get_user(uid); bal=u["balance_uah"] if u else 0
    await msg.answer(f"Користувач <code>{uid}</code>\nПоточний баланс: <b>{round(bal,2)}₴</b>\n\nВведіть суму (₴):",
        reply_markup=kb([btn("❌  Скасувати","admin_back")]))

@router.message(Admin.give_amount)
async def adm_give_amount(msg:Message, state:FSMContext, bot:Bot):
    if not is_admin(msg.from_user.id): return
    try: amount=float(msg.text.strip().replace(",","."))
    except ValueError:
        await msg.answer("❌ Введіть число!"); return
    data=await state.get_data(); uid=data["give_uid"]
    await db_add_balance(uid, amount)
    u=await db_get_user(uid); new_bal=u["balance_uah"] if u else amount
    with suppress(Exception):
        await bot.send_message(uid,
            f"💰 <b>Баланс поповнено!</b>\n\n"
            f"<blockquote>+<b>{amount}₴</b> від адміністрації\nПоточний баланс: <b>{round(new_bal,2)}₴</b></blockquote>")
    await state.clear()
    await msg.answer(f"✅ Видано <b>{amount}₴</b> користувачу <code>{uid}</code>",
        reply_markup=kb([btn("🔙  До панелі","admin_back")]))

@router.callback_query(F.data=="adm_pending")
async def adm_pending(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return
    rows=(await sb.table("pending_sends").select("*").eq("status","pending").order("created_at").execute()).data
    if not rows:
        await cb.answer("✅ Немає очікуючих відправок.",show_alert=True); return
    for r in rows[:5]:
        icon="◎" if r["coin"]=="TON" else "💎"
        is_overdue=""
        try:
            created_dt=datetime.fromisoformat(r["created_at"].replace("Z","+00:00"))
            if datetime.now(timezone.utc)-created_dt>timedelta(minutes=30):
                is_overdue="\n⚠️ <b>ПРОСТРОЧЕНО!</b>"
        except Exception: pass
        await cb.message.answer(
            f"{icon} <b>Відправка #{r['id']}</b>\n👤 <code>{r['user_id']}</code>\n"
            f"📬 <code>{r['to_address']}</code>\n💵 <b>{r['amount']} {r['coin']}</b>  (~{r['amount_uah']}₴)\n"
            f"🕐 {r['created_at'][:16]}{is_overdue}",
            reply_markup=kb(
                [btn(f"✅ Підтвердити відправку","admin_confirm_send:"+str(r["id"]))],
                [btn(f"❌ Скасувати","admin_cancel_send:"+str(r["id"]))]))
    await cb.answer()

@router.callback_query(F.data.startswith("admin_confirm_send:"))
async def admin_confirm_send(cb:CallbackQuery, bot:Bot):
    if not is_admin(cb.from_user.id): return
    sid=int(cb.data.split(":")[1])
    row=(await sb.table("pending_sends").select("*").eq("id",sid).execute()).data
    if not row:
        await cb.answer("❌ Відправка не знайдена.",show_alert=True); return
    row=row[0]
    if row["status"]!="pending":
        await cb.answer("⚠️ Вже оброблено.",show_alert=True); return
    await sb.table("pending_sends").update({"status":"confirmed"}).eq("id",sid).execute()
    with suppress(Exception):
        await bot.send_message(row["user_id"],
            f"✅ <b>Відправку підтверджено!</b>\n\n"
            f"<blockquote>{'◎' if row['coin']=='TON' else '💎'}  <b>{row['amount']} {row['coin']}</b>\n"
            f"буде відправлено на <code>{row['to_address']}</code></blockquote>")
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(cb.message.text+"\n\n✅ <b>ПІДТВЕРДЖЕНО</b>")
    await cb.answer("✅ Підтверджено!")

@router.callback_query(F.data.startswith("admin_cancel_send:"))
async def admin_cancel_send(cb:CallbackQuery, bot:Bot):
    if not is_admin(cb.from_user.id): return
    sid=int(cb.data.split(":")[1])
    row=(await sb.table("pending_sends").select("*").eq("id",sid).execute()).data
    if not row: await cb.answer("❌ Не знайдено.",show_alert=True); return
    row=row[0]
    await sb.table("pending_sends").update({"status":"cancelled"}).eq("id",sid).execute()
    with suppress(Exception):
        await bot.send_message(row["user_id"],
            "❌ <b>Відправку скасовано адміном.</b>\n\nЗверніться до підтримки.",
            reply_markup=kb([btn("💬  Підтримка","support")]))
    await cb.answer("❌ Скасовано.")

@router.callback_query(F.data=="adm_broadcast")
async def adm_broadcast_start(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(Admin.broadcast)
    await cb.message.answer("📤 Введіть текст для розсилки (HTML підтримується):",
        reply_markup=kb([btn("❌  Скасувати","admin_back")]))
    await cb.answer()

@router.message(Admin.broadcast)
async def adm_broadcast_send(msg:Message, state:FSMContext, bot:Bot):
    if not is_admin(msg.from_user.id): return
    text=msg.text or msg.caption or ""
    await state.clear()
    users=(await sb.table("users").select("user_id").execute()).data
    ok,fail=0,0
    for u in users:
        try:
            await bot.send_message(u["user_id"],text,parse_mode=ParseMode.HTML)
            ok+=1
        except Exception: fail+=1
        await asyncio.sleep(0.05)
    await msg.answer(f"✅ Розсилка: <b>{ok}</b> доставлено, <b>{fail}</b> помилок.")

@router.callback_query(F.data=="adm_manual")
async def adm_manual_start(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(Admin.manual_send)
    await cb.message.answer(
        "✉️ Формат: <code>user_id|повідомлення</code>\n\nПриклад:\n<code>123456789|Ваші зірки відправлені ✅</code>",
        reply_markup=kb([btn("❌  Скасувати","admin_back")]))
    await cb.answer()

@router.message(Admin.manual_send)
async def adm_manual_send(msg:Message, state:FSMContext, bot:Bot):
    if not is_admin(msg.from_user.id): return
    data=await state.get_data()
    reply_uid=data.get("reply_uid")
    if reply_uid:
        try:
            await bot.send_message(reply_uid, msg.text or "", parse_mode=ParseMode.HTML)
            await state.clear()
            await msg.answer(f"✅ Відповідь відправлена користувачу <code>{reply_uid}</code>")
        except Exception as e:
            await msg.answer(f"❌ Помилка: {e}")
        return
    try:
        parts=msg.text.split("|",1)
        uid=int(parts[0].strip()); text=parts[1].strip()
        await bot.send_message(uid,text,parse_mode=ParseMode.HTML)
        await state.clear()
        await msg.answer(f"✅ Відправлено користувачу <code>{uid}</code>")
    except Exception as e:
        await msg.answer(f"❌ Помилка: {e}")

@router.callback_query(F.data.startswith("admin_reply_uid:"))
async def admin_reply_start(cb:CallbackQuery, state:FSMContext):
    if not is_admin(cb.from_user.id): return
    uid=int(cb.data.split(":")[1])
    await state.set_state(Admin.manual_send); await state.update_data(reply_uid=uid)
    await cb.message.answer(f"Введіть відповідь для <code>{uid}</code>:",
        reply_markup=kb([btn("❌  Скасувати","admin_back")]))
    await cb.answer()

@router.callback_query(F.data.startswith("force_complete:"))
async def force_complete_order(cb:CallbackQuery):
    if not is_admin(cb.from_user.id): return
    oid=int(cb.data.split(":")[1])
    await db_complete_order(oid)
    with suppress(TelegramBadRequest):
        await cb.message.edit_text(cb.message.text+"\n\n✅ <b>ВИКОНАНО</b>")
    await cb.answer("✅ Замовлення позначено виконаним!")

# ══════════════════════════════════════════════════════════════════════════
# ФОНОВИЙ ВОРКЕР — 30-хвилинні нагадування
# ══════════════════════════════════════════════════════════════════════════
async def deadline_checker(bot: Bot):
    """Кожні 5 хвилин нагадує адмінам про прострочені замовлення."""
    notified = set()  # не спамимо одне й те саме замовлення
    await asyncio.sleep(60)
    while True:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            overdue = (await sb.table("orders").select("*")
                       .eq("status","pending")
                       .lt("created_at", cutoff)
                       .execute()).data
            for order in overdue:
                if order["id"] in notified:
                    continue
                notified.add(order["id"])
                for aid in ADMIN_IDS:
                    with suppress(Exception):
                        await bot.send_message(
                            aid,
                            f"🔴 <b>ПРОСТРОЧЕНЕ ЗАМОВЛЕННЯ!</b>\n\n"
                            f"<blockquote>📦 #{order['id']} — {order['product']}\n"
                            f"👤 user_id: <code>{order['user_id']}</code>\n"
                            f"💰 {order['amount_uah']}₴\n"
                            f"🕐 Створено: {order['created_at'][:16]}</blockquote>\n\n"
                            f"⚠️ Замовлення очікує відправки більше 30 хвилин!",
                            reply_markup=kb([btn("✅ Позначити виконаним",f"force_complete:{order['id']}")])
                        )
        except Exception as e:
            logging.error(f"deadline_checker error: {e}")
        await asyncio.sleep(300)

# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════
async def main():
    global sb
    missing=[k for k,v in {
        "BOT_TOKEN":BOT_TOKEN,"SUPABASE_URL":SUPABASE_URL,"SUPABASE_KEY":SUPABASE_KEY
    }.items() if not v]
    if missing:
        print(f"❌ Відсутні в env: {', '.join(missing)}"); sys.exit(1)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    logging.info("🔗 Підключення до Supabase...")
    sb=await acreate_client(SUPABASE_URL,SUPABASE_KEY)
    logging.info("✅ Supabase OK")
    bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp =Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(deadline_checker(bot))
    logging.info("🐻 Grizz Shop v2 запущено!")
    await dp.start_polling(bot,allowed_updates=["message","callback_query"])

if __name__=="__main__":
    asyncio.run(main())
