"""Хранение данных в JSON-файлах."""

from __future__ import annotations

import json
import os
import threading
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")

EXPENSES_FILE = os.path.join(DATA_DIR, "expenses.json")
CATEGORIES_FILE = os.path.join(DATA_DIR, "categories.json")
INCOME_FILE = os.path.join(DATA_DIR, "income.json")
INCOME_CATEGORIES_FILE = os.path.join(DATA_DIR, "income_categories.json")

_lock = threading.RLock()

DEFAULT_CATEGORIES = [
    "Продукты", "Коммуналка", "Транспорт",
    "Здоровье", "Развлечения", "Прочее",
]
DEFAULT_INCOME_CATEGORIES = ["Зарплата", "Аванс", "Премия"]


# ── helpers ──────────────────────────────────────────────

def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _read(path, default):
    _ensure_dir()
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write(path, data):
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── generic categories ───────────────────────────────────

def _load_cat(path, defaults):
    with _lock:
        data = _read(path, None)
        if not data or "categories" not in data:
            _write(path, {"categories": list(defaults)})
            return list(defaults)
        return list(data["categories"])


def _save_cat(path, cats):
    with _lock:
        _write(path, {"categories": cats})


def _add_cat(path, name):
    name = name.strip()
    cats = _load_cat(path, [])
    if not name or any(c.lower() == name.lower() for c in cats):
        return False
    cats.append(name)
    _save_cat(path, cats)
    return True


def _del_cat(path, name):
    cats = _load_cat(path, [])
    new = [c for c in cats if c.lower() != name.lower()]
    if len(new) == len(cats):
        return False
    _save_cat(path, new)
    return True


def _ren_cat(path, old, new):
    new = new.strip()
    if not new:
        return False
    cats = _load_cat(path, [])
    if any(c.lower() == new.lower() and c.lower() != old.lower() for c in cats):
        return False
    out = [new if c.lower() == old.lower() else c for c in cats]
    _save_cat(path, out)
    return True


# ── expense categories ──────────────────────────────────

def load_categories():
    return _load_cat(CATEGORIES_FILE, DEFAULT_CATEGORIES)

def add_category(name):
    return _add_cat(CATEGORIES_FILE, name)

def delete_category(name):
    return _del_cat(CATEGORIES_FILE, name)

def rename_category(old, new):
    return _ren_cat(CATEGORIES_FILE, old, new)


# ── income categories ───────────────────────────────────

def load_income_categories():
    return _load_cat(INCOME_CATEGORIES_FILE, DEFAULT_INCOME_CATEGORIES)

def add_income_category(name):
    return _add_cat(INCOME_CATEGORIES_FILE, name)

def delete_income_category(name):
    return _del_cat(INCOME_CATEGORIES_FILE, name)

def rename_income_category(old, new):
    return _ren_cat(INCOME_CATEGORIES_FILE, old, new)


# ── generic records ─────────────────────────────────────

def _next_id(records):
    return max((r["id"] for r in records), default=0) + 1


def _load_records(path, key):
    with _lock:
        data = _read(path, None)
        if not data or key not in data:
            _write(path, {key: []})
            return []
        return list(data[key])


def _save_records(path, key, records):
    with _lock:
        _write(path, {key: records})


def _add_record(path, key, amount, category, comment, day):
    with _lock:
        records = _load_records(path, key)
        item = {
            "id": _next_id(records),
            "date": (day or date.today()).isoformat(),
            "amount": round(float(amount), 2),
            "category": category,
            "comment": comment.strip(),
        }
        records.append(item)
        _save_records(path, key, records)
        return item


def _get_record(path, key, rec_id):
    for r in _load_records(path, key):
        if r["id"] == rec_id:
            return r
    return None


def _update_record(path, key, rec_id, **fields):
    with _lock:
        records = _load_records(path, key)
        for r in records:
            if r["id"] == rec_id:
                r.update(fields)
                _save_records(path, key, records)
                return True
        return False


def _delete_record(path, key, rec_id):
    with _lock:
        records = _load_records(path, key)
        new = [r for r in records if r["id"] != rec_id]
        if len(new) == len(records):
            return False
        _save_records(path, key, new)
        return True


def _list_records(path, key, limit=15):
    return sorted(
        _load_records(path, key),
        key=lambda r: (r["date"], r["id"]),
        reverse=True,
    )[:limit]


def _records_in_range(path, key, start, end):
    out = []
    for r in _load_records(path, key):
        try:
            d = date.fromisoformat(r["date"])
        except (ValueError, KeyError):
            continue
        if start <= d <= end:
            out.append(r)
    return sorted(out, key=lambda r: (r["date"], r["id"]))


# ── expense records ─────────────────────────────────────

def load_expenses():
    return _load_records(EXPENSES_FILE, "expenses")

def add_expense(amount, category, comment="", day=None):
    return _add_record(EXPENSES_FILE, "expenses", amount, category, comment, day)

def get_expense(expense_id):
    return _get_record(EXPENSES_FILE, "expenses", expense_id)

def update_expense(expense_id, **fields):
    return _update_record(EXPENSES_FILE, "expenses", expense_id, **fields)

def delete_expense(expense_id):
    return _delete_record(EXPENSES_FILE, "expenses", expense_id)

def list_expenses(limit=15):
    return _list_records(EXPENSES_FILE, "expenses", limit)

def expenses_in_range(start, end):
    return _records_in_range(EXPENSES_FILE, "expenses", start, end)


# ── income records ──────────────────────────────────────

def load_income():
    return _load_records(INCOME_FILE, "income")

def add_income(amount, category, comment="", day=None):
    return _add_record(INCOME_FILE, "income", amount, category, comment, day)

def get_income(income_id):
    return _get_record(INCOME_FILE, "income", income_id)

def update_income(income_id, **fields):
    return _update_record(INCOME_FILE, "income", income_id, **fields)

def delete_income(income_id):
    return _delete_record(INCOME_FILE, "income", income_id)

def list_income(limit=15):
    return _list_records(INCOME_FILE, "income", limit)

def income_in_range(start, end):
    return _records_in_range(INCOME_FILE, "income", start, end)
