"""Telegram-бот учёта расходов и доходов.  Polling + health-check."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import data_store as db

# ── config ──────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
CURRENCY = os.environ.get("CURRENCY", "₽")
PORT = int(os.environ.get("PORT", "10000") or 10000)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("expense-bot")

router = Router()


class AdminOnly(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.from_user) and message.from_user.id == ADMIN_ID


class Form(StatesGroup):
    pick_category = State()
    pick_income_category = State()
    add_category = State()
    rename_category = State()
    delete_category = State()
    report_from = State()
    report_to = State()
    edit_income_pick = State()
    edit_income_field = State()
    edit_income_value = State()


# ── utils ───────────────────────────────────────────────

AMOUNT_RE = re.compile(r"^\d+(?:[.,]\d{1,2})?$")
INCOME_RE = re.compile(r"^\+\s*(\d+(?:[.,]\d{1,2})?)\s*(.*)$")


def fmt_money(v: float) -> str:
    s = f"{v:,.2f}".replace(",", "\u2009").replace(".", ",")
    return f"{s} {CURRENCY}"


def parse_date(text: str) -> date | None:
    t = text.strip().lower()
    today = date.today()
    if t in ("сегодня", "today"):
        return today
    if t in ("вчера", "yesterday"):
        return today - timedelta(days=1)
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    return None


def parse_text(text: str, categories: list[str]):
    """'250 продукты хлеб' -> (amount, category|None, comment)."""
    text = text.strip()
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if not parts or not AMOUNT_RE.match(parts[0]):
        return None
    amount = float(parts[0].replace(",", "."))
    if amount <= 0:
        return None

    rest = parts[1].strip() if len(parts) > 1 else ""
    words = rest.split()

    for cat in sorted(categories, key=lambda c: len(c.split()), reverse=True):
        n = len(cat.split())
        if len(words) >= n and " ".join(words[:n]).lower() == cat.lower():
            return amount, cat, " ".join(words[n:]).strip()

    return amount, None, rest


def parse_income_text(text: str, categories: list[str]):
    """'+50000 зарплата июнь' -> (amount, category|None, comment)."""
    m = INCOME_RE.match(text.strip())
    if not m:
        return None
    amount = float(m.group(1).replace(",", "."))
    if amount <= 0:
        return None

    rest = m.group(2).strip()
    words = rest.split()

    for cat in sorted(categories, key=lambda c: len(c.split()), reverse=True):
        n = len(cat.split())
        if len(words) >= n and " ".join(words[:n]).lower() == cat.lower():
            return amount, cat, " ".join(words[n:]).strip()

    return amount, None, rest


def category_emoji(cat: str) -> str:
    table = {
        "продукты": "🛒", "коммуналка": "🏠", "транспорт": "🚗",
        "здоровье": "💊", "развлечения": "🎬", "прочее": "📦",
        "зарплата": "💰", "аванс": "💵", "премия": "🎁",
    }
    return table.get(cat.lower(), "🏷")


# ── keyboards ───────────────────────────────────────────

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Отчёт", callback_data="report"),
         InlineKeyboardButton(text="💰 Доходы", callback_data="income")],
        [InlineKeyboardButton(text="➕ Добавить трату", callback_data="add_hint"),
         InlineKeyboardButton(text="🗑 Удалить трату", callback_data="del")],
        [InlineKeyboardButton(text="📁 Категории", callback_data="cat"),
         InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
    ])


def income_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Отчёт", callback_data="ireport"),
         InlineKeyboardButton(text="✏️ Корректировка", callback_data="iedit")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="idel"),
         InlineKeyboardButton(text="📁 Категории", callback_data="icat")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])


def report_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="rep:today"),
         InlineKeyboardButton(text="Вчера", callback_data="rep:yesterday")],
        [InlineKeyboardButton(text="7 дней", callback_data="rep:week"),
         InlineKeyboardButton(text="Месяц", callback_data="rep:month")],
        [InlineKeyboardButton(text="📅 Свой период", callback_data="rep:custom")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])


def income_report_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="irep:today"),
         InlineKeyboardButton(text="Вчера", callback_data="irep:yesterday")],
        [InlineKeyboardButton(text="7 дней", callback_data="irep:week"),
         InlineKeyboardButton(text="Месяц", callback_data="irep:month")],
        [InlineKeyboardButton(text="📅 Свой период", callback_data="irep:custom")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="income")],
    ])


def cat_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Список", callback_data="cat:list"),
         InlineKeyboardButton(text="➕ Добавить", callback_data="cat:add")],
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data="cat:ren"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data="cat:del")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])


def income_cat_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Список", callback_data="icat:list"),
         InlineKeyboardButton(text="➕ Добавить", callback_data="icat:add")],
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data="icat:ren"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data="icat:del")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="income")],
    ])


def back_menu(cb: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=cb)],
    ])


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")],
    ])


def categories_keyboard(prefix: str,
                        categories: list[str] | None = None) -> InlineKeyboardMarkup:
    if categories is None:
        categories = db.load_categories()
    rows = [[InlineKeyboardButton(
        text=f"{category_emoji(c)} {c}", callback_data=f"{prefix}:{i}",
    )] for i, c in enumerate(categories)]
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── report builder ──────────────────────────────────────

def build_report(title: str, start: date, end: date,
                 records: list[dict], is_income: bool = False) -> str:
    total_label = "Общий доход" if is_income else "Общая сумма"

    lines = [f"📊 <b>{title}</b>"]
    lines.append(
        f"📅 {start.strftime('%d.%m.%Y')}"
        if start == end
        else f"📅 {start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"
    )
    lines.append("")

    if not records:
        lines.append("Записей нет.")
        return "\n".join(lines)

    by_cat: dict[str, dict] = {}
    total = 0.0
    for e in records:
        cell = by_cat.setdefault(e["category"], {"sum": 0.0, "count": 0})
        cell["sum"] += e["amount"]
        cell["count"] += 1
        total += e["amount"]

    for cat, cell in sorted(by_cat.items(), key=lambda kv: kv[1]["sum"], reverse=True):
        pct = cell["sum"] / total * 100 if total else 0
        lines.append(f"{category_emoji(cat)} <b>{cat}</b>")
        lines.append(
            f"    {fmt_money(cell['sum'])}  ·  {pct:.1f}%  ·  {cell['count']} оп."
        )
        lines.append("")

    lines.append(f"💰 <b>{total_label}:</b> {fmt_money(total)}")
    lines.append(f"🧾 Всего операций: {len(records)}")

    # среднее за день (только если период > 1 дня)
    days = (end - start).days + 1
    if days > 1:
        avg = total / days
        word = "доход" if is_income else "расход"
        lines.append(f"📈 Средний {word} за день: <b>{fmt_money(avg)}</b>")

    return "\n".join(lines)


# ── старт / помощь ──────────────────────────────────────

@router.message(Command("start"), AdminOnly())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>Привет! Я бот учёта расходов и доходов.</b>\n\n"
        "<b>Расход</b> — просто напиши:\n"
        "<code>250 продукты хлеб</code>\n"
        "<code>1200 коммуналка электричество</code>\n\n"
        "<b>Доход</b> — начни с <code>+</code>:\n"
        "<code>+50000 зарплата июнь</code>\n"
        "<code>+15000 аванс</code>\n\n"
        "Я пойму сумму, категорию и комментарий.",
        reply_markup=main_menu(),
    )


@router.message(Command("help"), AdminOnly())
@router.callback_query(F.data == "help")
async def show_help(event: Message | CallbackQuery):
    text = (
        "ℹ️ <b>Как пользоваться</b>\n\n"
        "<b>Расход</b> — просто сообщение:\n"
        "<code>сумма категория комментарий</code>\n"
        "Например: <code>350 продукты молоко и сыр</code>\n\n"
        "<b>Доход</b> — начни с <code>+</code>:\n"
        "<code>+50000 зарплата июнь</code>\n\n"
        "Если категорию не распознал — покажу кнопки.\n\n"
        "<b>Команды</b>\n"
        "/report — отчёт по расходам\n"
        "/income — доходы\n"
        "/del — удалить трату\n"
        "/cat — категории расходов\n"
        "/cancel — отменить действие\n"
        "/help — справка\n"
    )
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=main_menu())
        await event.answer()
    else:
        await event.answer(text, reply_markup=main_menu())


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("📋 <b>Главное меню</b>", reply_markup=main_menu())
    await cb.answer()


@router.callback_query(F.data == "add_hint")
async def cb_add_hint(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "➕ Напиши <b>трату</b>:\n"
        "<code>сумма категория комментарий</code>\n\n"
        "или <b>доход</b> (с <code>+</code> в начале):\n"
        "<code>+50000 зарплата июнь</code>\n\n"
        "Примеры трат:\n"
        "<code>250 продукты хлеб</code>\n"
        "<code>90 транспорт метро</code>"
    )


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("✖️ Отменено.", reply_markup=main_menu())
    await cb.answer()


# ── добавление расхода / дохода ────────────────────────

@router.message(F.text, StateFilter(None), AdminOnly())
async def handle_text(message: Message, state: FSMContext):
    text = message.text or ""

    # ── доход ──
    if text.strip().startswith("+"):
        parsed = parse_income_text(text, db.load_income_categories())
        if parsed is None:
            await message.answer(
                "🤔 Не понял формат дохода.\n"
                "Нужно: <code>+сумма категория комментарий</code>\n"
                "Например: <code>+50000 зарплата июнь</code>",
                reply_markup=main_menu(),
            )
            return

        amount, category, comment = parsed
        if category is None:
            cats = db.load_income_categories()
            if not cats:
                await message.answer("⚠️ Категории доходов пусты. Добавь: /income → Категории")
                return
            await state.set_state(Form.pick_income_category)
            await state.update_data(amount=amount, comment=comment)
            await message.answer(
                f"💰 Сумма: <b>{fmt_money(amount)}</b>\n"
                f"💬 {comment or '—'}\n\n"
                "Выбери категорию дохода:",
                reply_markup=categories_keyboard("ipick", cats),
            )
            return

        item = db.add_income(amount, category, comment)
        await message.answer(
            f"✅ <b>Доход сохранён</b>\n"
            f"{category_emoji(category)} {category} — {fmt_money(item['amount'])}\n"
            f"💬 {item['comment'] or '—'}\n"
            f"📅 {date.today().strftime('%d.%m.%Y')}",
            reply_markup=main_menu(),
        )
        return

    # ── расход ──
    parsed = parse_text(text, db.load_categories())
    if parsed is None:
        await message.answer(
            "🤔 Не понял формат.\n"
            "<b>Расход:</b> <code>250 продукты хлеб</code>\n"
            "<b>Доход:</b> <code>+50000 зарплата</code>",
            reply_markup=main_menu(),
        )
        return

    amount, category, comment = parsed

    if category is None:
        cats = db.load_categories()
        if not cats:
            await message.answer("⚠️ Список категорий пуст. Добавь: /cat")
            return
        await state.set_state(Form.pick_category)
        await state.update_data(amount=amount, comment=comment)
        await message.answer(
            f"Сумма: <b>{fmt_money(amount)}</b>\n"
            f"💬 {comment or '—'}\n\n"
            "Выбери категорию расхода:",
            reply_markup=categories_keyboard("pick", cats),
        )
        return

    item = db.add_expense(amount, category, comment)
    await message.answer(
        f"✅ <b>Сохранено</b>\n"
        f"{category_emoji(category)} {category} — {fmt_money(item['amount'])}\n"
        f"💬 {item['comment'] or '—'}\n"
        f"📅 {date.today().strftime('%d.%m.%Y')}",
        reply_markup=main_menu(),
    )


@router.callback_query(Form.pick_category, F.data.startswith("pick:"))
async def cb_pick_category(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":", 1)[1])
    cats = db.load_categories()
    if idx < 0 or idx >= len(cats):
        await cb.answer("Категория не найдена", show_alert=True)
        return
    data = await state.get_data()
    category = cats[idx]
    item = db.add_expense(data["amount"], category, data.get("comment", ""))
    await state.clear()
    await cb.message.edit_text(
        f"✅ <b>Сохранено</b>\n"
        f"{category_emoji(category)} {category} — {fmt_money(item['amount'])}\n"
        f"💬 {item['comment'] or '—'}\n"
        f"📅 {date.today().strftime('%d.%m.%Y')}",
        reply_markup=main_menu(),
    )
    await cb.answer("Добавлено")


@router.callback_query(Form.pick_income_category, F.data.startswith("ipick:"))
async def cb_pick_income_category(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":", 1)[1])
    cats = db.load_income_categories()
    if idx < 0 or idx >= len(cats):
        await cb.answer("Категория не найдена", show_alert=True)
        return
    data = await state.get_data()
    category = cats[idx]
    item = db.add_income(data["amount"], category, data.get("comment", ""))
    await state.clear()
    await cb.message.edit_text(
        f"✅ <b>Доход сохранён</b>\n"
        f"{category_emoji(category)} {category} — {fmt_money(item['amount'])}\n"
        f"💬 {item['comment'] or '—'}\n"
        f"📅 {date.today().strftime('%d.%m.%Y')}",
        reply_markup=main_menu(),
    )
    await cb.answer("Добавлено")


# ── отчёты по расходам ─────────────────────────────────

@router.message(Command("report"), AdminOnly())
@router.callback_query(F.data == "report")
async def show_report_menu(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    text = "📊 <b>Отчёт по расходам</b>\n\nВыбери период:"
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=report_menu())
        await event.answer()
    else:
        await event.answer(text, reply_markup=report_menu())


async def send_report(cb, title, start, end):
    expenses = db.expenses_in_range(start, end)
    await cb.message.edit_text(
        build_report(title, start, end, expenses),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К периодам", callback_data="report")],
            [InlineKeyboardButton(text="📋 Меню", callback_data="menu")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rep:"))
async def cb_report(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":", 1)[1]
    today = date.today()
    if kind == "today":
        await send_report(cb, "Расходы за сегодня", today, today)
    elif kind == "yesterday":
        d = today - timedelta(days=1)
        await send_report(cb, "Расходы за вчера", d, d)
    elif kind == "week":
        await send_report(cb, "Расходы за 7 дней", today - timedelta(days=6), today)
    elif kind == "month":
        await send_report(cb, "Расходы за месяц", today.replace(day=1), today)
    elif kind == "custom":
        await state.set_state(Form.report_from)
        await state.update_data(mode="expense")
        await cb.message.edit_text(
            "📅 Введи <b>дату начала</b>:\n<code>01.06.2025</code>",
            reply_markup=cancel_menu(),
        )
        await cb.answer()


@router.message(Form.report_from, AdminOnly())
async def report_from(message: Message, state: FSMContext):
    d = parse_date(message.text or "")
    if d is None:
        await message.answer("❌ Не понял дату. Пример: <code>01.06.2025</code>",
                             reply_markup=cancel_menu())
        return
    await state.update_data(start=d.isoformat())
    await state.set_state(Form.report_to)
    await message.answer(
        f"Начало: <b>{d.strftime('%d.%m.%Y')}</b>\n\nТеперь <b>дату конца</b>:",
        reply_markup=cancel_menu(),
    )


@router.message(Form.report_to, AdminOnly())
async def report_to(message: Message, state: FSMContext):
    end = parse_date(message.text or "")
    if end is None:
        await message.answer("❌ Не понял дату. Пример: <code>30.06.2025</code>",
                             reply_markup=cancel_menu())
        return

    data = await state.get_data()
    start = date.fromisoformat(data["start"])
    if end < start:
        start, end = end, start

    mode = data.get("mode", "expense")
    await state.clear()

    if mode == "income":
        records = db.income_in_range(start, end)
        text = build_report("Доходы за период", start, end, records, is_income=True)
    else:
        records = db.expenses_in_range(start, end)
        text = build_report("Расходы за период", start, end, records)

    await message.answer(text, reply_markup=main_menu())


# ── удаление трат ───────────────────────────────────────

def delete_list_kb(items, prefix="d"):
    rows = []
    for e in items:
        d = date.fromisoformat(e["date"])
        label = f"{d.strftime('%d.%m')} · {e['category']} · {e['amount']:,.2f} {CURRENCY}"
        if e.get("comment"):
            label += f" · {e['comment']}"
        rows.append([InlineKeyboardButton(
            text=f"🗑 {label[:55]}", callback_data=f"{prefix}:{e['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("del"), AdminOnly())
@router.callback_query(F.data == "del")
async def show_delete_list(event: Message | CallbackQuery):
    items = db.list_expenses(15)
    if not items:
        text, kb = "🗑 Список трат пуст.", back_menu()
    else:
        text = "🗑 <b>Выбери трату для удаления</b> (последние 15):"
        kb = delete_list_kb(items, "d")

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("d:"))
async def cb_delete(cb: CallbackQuery):
    eid = int(cb.data.split(":", 1)[1])
    item = db.get_expense(eid)
    if item is None:
        await cb.answer("Уже удалено", show_alert=True)
    else:
        db.delete_expense(eid)
        await cb.answer(f"Удалено: {item['category']} {item['amount']}")

    items = db.list_expenses(15)
    if not items:
        await cb.message.edit_text("🗑 Список трат пуст.", reply_markup=back_menu())
    else:
        await cb.message.edit_text(
            "🗑 <b>Выбери трату для удаления</b> (последние 15):",
            reply_markup=delete_list_kb(items, "d"),
        )


# ── доходы: главное меню ────────────────────────────────

@router.callback_query(F.data == "income")
async def cb_income_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "💰 <b>Доходы</b>\n\n"
        "Чтобы добавить доход — напиши:\n"
        "<code>+50000 зарплата июнь</code>",
        reply_markup=income_menu(),
    )
    await cb.answer()


# ── доходы: отчёты ──────────────────────────────────────

@router.callback_query(F.data == "ireport")
async def cb_ireport(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("📊 <b>Отчёт по доходам</b>\n\nВыбери период:",
                               reply_markup=income_report_menu())
    await cb.answer()


async def send_ireport(cb, title, start, end):
    records = db.income_in_range(start, end)
    await cb.message.edit_text(
        build_report(title, start, end, records, is_income=True),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К периодам", callback_data="ireport")],
            [InlineKeyboardButton(text="💰 Меню доходов", callback_data="income")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("irep:"))
async def cb_irep(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":", 1)[1]
    today = date.today()
    if kind == "today":
        await send_ireport(cb, "Доходы за сегодня", today, today)
    elif kind == "yesterday":
        d = today - timedelta(days=1)
        await send_ireport(cb, "Доходы за вчера", d, d)
    elif kind == "week":
        await send_ireport(cb, "Доходы за 7 дней", today - timedelta(days=6), today)
    elif kind == "month":
        await send_ireport(cb, "Доходы за месяц", today.replace(day=1), today)
    elif kind == "custom":
        await state.set_state(Form.report_from)
        await state.update_data(mode="income")
        await cb.message.edit_text(
            "📅 Введи <b>дату начала</b>:\n<code>01.06.2025</code>",
            reply_markup=cancel_menu(),
        )
        await cb.answer()


# ── доходы: удаление ────────────────────────────────────

@router.callback_query(F.data == "idel")
async def cb_idel(cb: CallbackQuery):
    items = db.list_income(15)
    if not items:
        await cb.message.edit_text("🗑 Список доходов пуст.", reply_markup=back_menu("income"))
        await cb.answer()
        return

    rows = []
    for e in items:
        d = date.fromisoformat(e["date"])
        label = f"{d.strftime('%d.%m')} · {e['category']} · {e['amount']:,.2f} {CURRENCY}"
        if e.get("comment"):
            label += f" · {e['comment']}"
        rows.append([InlineKeyboardButton(
            text=f"🗑 {label[:55]}", callback_data=f"id:{e['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="income")])
    await cb.message.edit_text("🗑 <b>Удалить доход</b> (последние 15):",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("id:"))
async def cb_delete_income(cb: CallbackQuery):
    iid = int(cb.data.split(":", 1)[1])
    item = db.get_income(iid)
    if item is None:
        await cb.answer("Уже удалено", show_alert=True)
    else:
        db.delete_income(iid)
        await cb.answer(f"Удалено: {item['category']} {item['amount']}")

    items = db.list_income(15)
    if not items:
        await cb.message.edit_text("🗑 Список доходов пуст.",
                                   reply_markup=back_menu("income"))
        return

    rows = []
    for e in items:
        d = date.fromisoformat(e["date"])
        label = f"{d.strftime('%d.%m')} · {e['category']} · {e['amount']:,.2f} {CURRENCY}"
        if e.get("comment"):
            label += f" · {e['comment']}"
        rows.append([InlineKeyboardButton(
            text=f"🗑 {label[:55]}", callback_data=f"id:{e['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="income")])
    await cb.message.edit_text("🗑 <b>Удалить доход</b> (последние 15):",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ── доходы: корректировка ──────────────────────────────

@router.callback_query(F.data == "iedit")
async def cb_iedit(cb: CallbackQuery, state: FSMContext):
    items = db.list_income(15)
    if not items:
        await cb.message.edit_text("Нет доходов для корректировки.",
                                   reply_markup=back_menu("income"))
        await cb.answer()
        return

    rows = []
    for e in items:
        d = date.fromisoformat(e["date"])
        label = f"{d.strftime('%d.%m')} · {e['category']} · {e['amount']:,.2f} {CURRENCY}"
        if e.get("comment"):
            label += f" · {e['comment']}"
        rows.append([InlineKeyboardButton(
            text=f"✏️ {label[:55]}", callback_data=f"ie:{e['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="income")])

    await state.set_state(Form.edit_income_pick)
    await cb.message.edit_text("✏️ <b>Выбери доход для корректировки</b> (последние 15):",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(Form.edit_income_pick, F.data.startswith("ie:"))
async def cb_iedit_pick(cb: CallbackQuery, state: FSMContext):
    iid = int(cb.data.split(":", 1)[1])
    item = db.get_income(iid)
    if item is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return

    await state.set_state(Form.edit_income_field)
    await state.update_data(edit_id=iid)

    d = date.fromisoformat(item["date"])
    text = (
        f"✏️ <b>Корректировка дохода #{iid}</b>\n\n"
        f"📅 {d.strftime('%d.%m.%Y')}\n"
        f"{category_emoji(item['category'])} {item['category']} — {fmt_money(item['amount'])}\n"
        f"💬 {item['comment'] or '—'}\n\n"
        "Что изменить?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Сумма", callback_data="ief:amount"),
         InlineKeyboardButton(text="🏷 Категория", callback_data="ief:category")],
        [InlineKeyboardButton(text="💬 Комментарий", callback_data="ief:comment")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="income")],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(Form.edit_income_field, F.data.startswith("ief:"))
async def cb_iedit_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":", 1)[1]

    if field == "category":
        cats = db.load_income_categories()
        await state.set_state(Form.edit_income_value)
        await state.update_data(edit_field="category")
        await cb.message.edit_text("🏷 Выбери новую категорию:",
                                   reply_markup=categories_keyboard("iec", cats))
        await cb.answer()
        return

    if field == "amount":
        await state.set_state(Form.edit_income_value)
        await state.update_data(edit_field="amount")
        await cb.message.edit_text("💰 Введи новую сумму:",
                                   reply_markup=cancel_menu())
        await cb.answer()
        return

    if field == "comment":
        await state.set_state(Form.edit_income_value)
        await state.update_data(edit_field="comment")
        await cb.message.edit_text("💬 Введи новый комментарий:",
                                   reply_markup=cancel_menu())
        await cb.answer()
        return


@router.callback_query(Form.edit_income_value, F.data.startswith("iec:"))
async def cb_iedit_cat_value(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":", 1)[1])
    cats = db.load_income_categories()
    if idx < 0 or idx >= len(cats):
        await cb.answer("Не найдено", show_alert=True)
        return

    data = await state.get_data()
    iid = data["edit_id"]
    new_cat = cats[idx]
    db.update_income(iid, category=new_cat)
    await state.clear()
    await cb.message.edit_text(
        f"✅ Категория изменена на: {category_emoji(new_cat)} <b>{new_cat}</b>",
        reply_markup=income_menu(),
    )
    await cb.answer("Обновлено")


@router.message(Form.edit_income_value, AdminOnly())
async def process_iedit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    iid = data["edit_id"]
    field = data["edit_field"]
    text = (message.text or "").strip()

    if field == "amount":
        if not AMOUNT_RE.match(text):
            await message.answer("❌ Введи число. Например: <code>55000</code>",
                                 reply_markup=cancel_menu())
            return
        new_val = round(float(text.replace(",", ".")), 2)
        db.update_income(iid, amount=new_val)
        await state.clear()
        await message.answer(f"✅ Сумма изменена на <b>{fmt_money(new_val)}</b>",
                             reply_markup=income_menu())

    elif field == "comment":
        db.update_income(iid, comment=text)
        await state.clear()
        await message.answer(f"✅ Комментарий изменён: <b>{text or '—'}</b>",
                             reply_markup=income_menu())


# ── категории расходов ─────────────────────────────────

@router.message(Command("cat"), AdminOnly())
@router.callback_query(F.data == "cat")
async def show_cat_menu(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    text = "📁 <b>Категории расходов</b>"
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=cat_menu())
        await event.answer()
    else:
        await event.answer(text, reply_markup=cat_menu())


@router.callback_query(F.data == "cat:list")
async def cb_cat_list(cb: CallbackQuery):
    cats = db.load_categories()
    if not cats:
        text = "📁 Список пуст."
    else:
        counts = {}
        for e in db.load_expenses():
            counts[e["category"]] = counts.get(e["category"], 0) + 1
        text = "📁 <b>Категории расходов</b>\n\n"
        for i, c in enumerate(cats, 1):
            text += f"{i}. {category_emoji(c)} {c} — {counts.get(c, 0)} записей\n"
    await cb.message.edit_text(text, reply_markup=back_menu("cat"))
    await cb.answer()


@router.callback_query(F.data == "cat:add")
async def cb_cat_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.add_category)
    await state.update_data(cat_type="expense")
    await cb.message.edit_text("➕ Введи название категории:",
                               reply_markup=cancel_menu())
    await cb.answer()


@router.message(Form.add_category, AdminOnly())
async def process_add_category(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or len(name) > 32:
        await message.answer("❌ От 1 до 32 символов.", reply_markup=cancel_menu())
        return

    data = await state.get_data()
    cat_type = data.get("cat_type", "expense")

    if cat_type == "income":
        ok = db.add_income_category(name)
        back = income_cat_menu()
    else:
        ok = db.add_category(name)
        back = cat_menu()

    if ok:
        await state.clear()
        await message.answer(f"✅ Добавлено: {category_emoji(name)} <b>{name}</b>",
                             reply_markup=back)
    else:
        await message.answer("❌ Уже существует.", reply_markup=cancel_menu())


@router.callback_query(F.data == "cat:del")
async def cb_cat_del(cb: CallbackQuery, state: FSMContext):
    cats = db.load_categories()
    if not cats:
        await cb.answer("Список пуст", show_alert=True)
        return
    await state.set_state(Form.delete_category)
    await state.update_data(cat_type="expense")
    await cb.message.edit_text("🗑 Выбери категорию для удаления:",
                               reply_markup=categories_keyboard("cdel", cats))
    await cb.answer()


@router.callback_query(Form.delete_category, F.data.startswith("cdel:"))
async def cb_cat_del_pick(cb: CallbackQuery, state: FSMContext):
    cats = db.load_categories()
    idx = int(cb.data.split(":", 1)[1])
    if idx < 0 or idx >= len(cats):
        await cb.answer("Не найдено", show_alert=True)
        return
    name = cats[idx]
    db.delete_category(name)
    await state.clear()
    await cb.message.edit_text(f"✅ Удалено: <b>{name}</b>", reply_markup=cat_menu())
    await cb.answer("Удалено")


@router.callback_query(F.data == "cat:ren")
async def cb_cat_ren(cb: CallbackQuery, state: FSMContext):
    cats = db.load_categories()
    if not cats:
        await cb.answer("Список пуст", show_alert=True)
        return
    await state.set_state(Form.rename_category)
    await state.update_data(cat_type="expense")
    await cb.message.edit_text("✏️ Выбери категорию:",
                               reply_markup=categories_keyboard("cren", cats))
    await cb.answer()


@router.callback_query(Form.rename_category, F.data.startswith("cren:"))
async def cb_cat_ren_pick(cb: CallbackQuery, state: FSMContext):
    cats = db.load_categories()
    idx = int(cb.data.split(":", 1)[1])
    if idx < 0 or idx >= len(cats):
        await cb.answer("Не найдено", show_alert=True)
        return
    await state.update_data(rename_idx=idx)
    await cb.message.edit_text(
        f"Текущее: <b>{cats[idx]}</b>\n\nВведи новое название:",
        reply_markup=cancel_menu(),
    )
    await cb.answer()


@router.message(Form.rename_category, AdminOnly())
async def process_rename_category(message: Message, state: FSMContext):
    new_name = (message.text or "").strip()
    if not new_name or len(new_name) > 32:
        await message.answer("❌ От 1 до 32 символов.", reply_markup=cancel_menu())
        return

    data = await state.get_data()
    cat_type = data.get("cat_type", "expense")
    idx = data.get("rename_idx", -1)

    if cat_type == "income":
        cats = db.load_income_categories()
        if idx < 0 or idx >= len(cats):
            await state.clear()
            await message.answer("❌ Ошибка.", reply_markup=income_cat_menu())
            return
        old_name = cats[idx]
        ok = db.rename_income_category(old_name, new_name)
        back = income_cat_menu()
    else:
        cats = db.load_categories()
        if idx < 0 or idx >= len(cats):
            await state.clear()
            await message.answer("❌ Ошибка.", reply_markup=cat_menu())
            return
        old_name = cats[idx]
        ok = db.rename_category(old_name, new_name)
        back = cat_menu()

    if ok:
        await state.clear()
        await message.answer(
            f"✅ <b>{old_name}</b> → <b>{new_name}</b>", reply_markup=back,
        )
    else:
        await message.answer("❌ Дубликат.", reply_markup=cancel_menu())


# ── категории доходов ──────────────────────────────────

@router.callback_query(F.data == "icat")
async def cb_icat(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("📁 <b>Категории доходов</b>",
                               reply_markup=income_cat_menu())
    await cb.answer()


@router.callback_query(F.data == "icat:list")
async def cb_icat_list(cb: CallbackQuery):
    cats = db.load_income_categories()
    if not cats:
        text = "📁 Список пуст."
    else:
        counts = {}
        for e in db.load_income():
            counts[e["category"]] = counts.get(e["category"], 0) + 1
        text = "📁 <b>Категории доходов</b>\n\n"
        for i, c in enumerate(cats, 1):
            text += f"{i}. {category_emoji(c)} {c} — {counts.get(c, 0)} записей\n"
    await cb.message.edit_text(text, reply_markup=back_menu("icat"))
    await cb.answer()


@router.callback_query(F.data == "icat:add")
async def cb_icat_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.add_category)
    await state.update_data(cat_type="income")
    await cb.message.edit_text("➕ Введи название категории дохода:",
                               reply_markup=cancel_menu())
    await cb.answer()


@router.callback_query(F.data == "icat:del")
async def cb_icat_del(cb: CallbackQuery, state: FSMContext):
    cats = db.load_income_categories()
    if not cats:
        await cb.answer("Список пуст", show_alert=True)
        return
    await state.set_state(Form.delete_category)
    await state.update_data(cat_type="income")
    rows = [[InlineKeyboardButton(
        text=f"{category_emoji(c)} {c}", callback_data=f"icdel:{i}",
    )] for i, c in enumerate(cats)]
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")])
    await cb.message.edit_text("🗑 Выбери категорию дохода для удаления:",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(Form.delete_category, F.data.startswith("icdel:"))
async def cb_icat_del_pick(cb: CallbackQuery, state: FSMContext):
    cats = db.load_income_categories()
    idx = int(cb.data.split(":", 1)[1])
    if idx < 0 or idx >= len(cats):
        await cb.answer("Не найдено", show_alert=True)
        return
    name = cats[idx]
    db.delete_income_category(name)
    await state.clear()
    await cb.message.edit_text(f"✅ Удалено: <b>{name}</b>",
                               reply_markup=income_cat_menu())
    await cb.answer("Удалено")


@router.callback_query(F.data == "icat:ren")
async def cb_icat_ren(cb: CallbackQuery, state: FSMContext):
    cats = db.load_income_categories()
    if not cats:
        await cb.answer("Список пуст", show_alert=True)
        return
    await state.set_state(Form.rename_category)
    await state.update_data(cat_type="income")
    rows = [[InlineKeyboardButton(
        text=f"{category_emoji(c)} {c}", callback_data=f"icren:{i}",
    )] for i, c in enumerate(cats)]
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")])
    await cb.message.edit_text("✏️ Выбери категорию дохода:",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(Form.rename_category, F.data.startswith("icren:"))
async def cb_icat_ren_pick(cb: CallbackQuery, state: FSMContext):
    cats = db.load_income_categories()
    idx = int(cb.data.split(":", 1)[1])
    if idx < 0 or idx >= len(cats):
        await cb.answer("Не найдено", show_alert=True)
        return
    await state.update_data(rename_idx=idx)
    await cb.message.edit_text(
        f"Текущее: <b>{cats[idx]}</b>\n\nВведи новое название:",
        reply_markup=cancel_menu(),
    )
    await cb.answer()


# ── отмена ──────────────────────────────────────────────

@router.message(Command("cancel"), AdminOnly())
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✖️ Отменено.", reply_markup=main_menu())


# ── HTTP health-check ──────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "expense-bot",
        "time": datetime.now().isoformat(timespec="seconds"),
    })


async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("HTTP health-check сервер запущен на порту %s", PORT)
    return runner


# ── main ────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        sys.exit("❌ Не задан BOT_TOKEN")
    if not ADMIN_ID:
        sys.exit("❌ Не задан ADMIN_ID")

    db.load_categories()
    db.load_income_categories()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    runner = await start_http_server()
    log.info("Запуск бота в режиме POLLING...")
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
