#!/usr/bin/env python3
"""
🍎 Трекер питания — красивое веб-приложение на Streamlit
Поиск продуктов через Open Food Facts + локальное сохранение в SQLite
"""

import streamlit as st
import sqlite3
import requests
import pandas as pd
from datetime import date, datetime, timedelta
from pathlib import Path
import time

# ==================== НАСТРОЙКИ ====================
DB_PATH = Path(__file__).parent / "nutrition.db"
USER_AGENT = "Mozilla/5.0 (compatible; NutritionTracker/2.1; +https://github.com/nutrition-tracker)"
SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"


def get_turso_config():
    """
    Облачная БД Turso (SQLite в облаке).
    Streamlit secrets:
      [turso]
      url = "libsql://YOUR-DB.turso.io"
      auth_token = "..."
    или переменные TURSO_DATABASE_URL / TURSO_AUTH_TOKEN
    """
    url = token = None
    try:
        if "turso" in st.secrets:
            url = st.secrets["turso"].get("url") or st.secrets["turso"].get("database_url")
            token = st.secrets["turso"].get("auth_token") or st.secrets["turso"].get("token")
    except Exception:
        pass
    if not url or not token:
        import os
        url = url or os.environ.get("TURSO_DATABASE_URL") or os.environ.get("LIBSQL_URL")
        token = token or os.environ.get("TURSO_AUTH_TOKEN") or os.environ.get("LIBSQL_AUTH_TOKEN")
    if url and token:
        return str(url).strip(), str(token).strip()
    return None, None


def is_cloud_db():
    url, token = get_turso_config()
    return bool(url and token)

st.set_page_config(
    page_title="Трекер питания",
    page_icon="🍎",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# ==================== СТИЛИ ====================
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }
    
    .main-header {
        font-size: 1.8rem;
        font-weight: 700;
        background: linear-gradient(135deg, #10b981, #059669);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.2rem;
    }
    
    .sub-header {
        color: #6b7280;
        font-size: 0.95rem;
        margin-bottom: 1rem;
    }
    
    /* Крупнее кнопки — удобнее на телефоне */
    .stButton > button {
        border-radius: 12px !important;
        font-weight: 600 !important;
        min-height: 2.8rem !important;
        font-size: 0.95rem !important;
    }
    
    /* Метрики компактнее */
    div[data-testid="stMetricValue"] {
        font-size: 1.25rem !important;
    }
    div[data-testid="stMetricLabel"] {
        font-size: 0.8rem !important;
    }
    
    /* Меньше отступы на мобиле */
    @media (max-width: 768px) {
        .main-header { font-size: 1.5rem !important; }
        .block-container {
            padding-left: 0.8rem !important;
            padding-right: 0.8rem !important;
            padding-top: 1rem !important;
        }
        .stButton > button {
            min-height: 3rem !important;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.1rem !important;
        }
    }
    
    .metric-card {
        background: linear-gradient(145deg, #ffffff, #f8fafc);
        border: 1px solid #e2e8f0;
        border-radius: 16px;
        padding: 1.2rem 1.4rem;
        box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);
        text-align: center;
    }
    
    .metric-value {
        font-size: 1.8rem;
        font-weight: 700;
        color: #0f172a;
    }
    
    .metric-label {
        font-size: 0.85rem;
        color: #64748b;
        margin-top: 0.25rem;
    }
    
    .stButton > button {
        border-radius: 10px;
        font-weight: 500;
        transition: all 0.2s;
    }
    
    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(16, 185, 129, 0.25);
    }
    
    div[data-testid="stMetricValue"] {
        font-size: 1.6rem;
        font-weight: 700;
    }
    
    .product-card {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 1rem 1.2rem;
        margin-bottom: 0.8rem;
    }
    
    .success-box {
        background: #ecfdf5;
        border-left: 4px solid #10b981;
        padding: 0.9rem 1.2rem;
        border-radius: 0 10px 10px 0;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)


# ==================== БАЗА ДАННЫХ ====================
# Кэш соединения и схема (Streamlit на каждый клик перезапускает скрипт —
# без кэша каждый раз новый round-trip в Turso → тормоза)
_DB_CONN = None
_DB_INITED = False
_TURSO_LOCAL = Path("/tmp/nutrition_turso_cache.db")


class _ConnProxy:
    """Прокси: close() не закрывает singleton; commit() синхронизирует с Turso."""

    def __init__(self, conn, is_cloud=False):
        self._conn = conn
        self._is_cloud = is_cloud

    def cursor(self):
        return self._conn.cursor()

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._conn.executemany(*args, **kwargs)

    def commit(self):
        self._conn.commit()
        if self._is_cloud and hasattr(self._conn, "sync"):
            try:
                self._conn.sync()
            except Exception:
                pass

    def close(self):
        # не закрываем общий коннект — переиспользуем
        return None

    def sync(self):
        if hasattr(self._conn, "sync"):
            self._conn.sync()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def get_conn():
    """
    Локально → nutrition.db
    Turso → локальный кэш + sync (быстрые чтения, облако для постоянного хранения)
    """
    global _DB_CONN
    url, token = get_turso_config()

    if url and token:
        if _DB_CONN is not None:
            return _ConnProxy(_DB_CONN, is_cloud=True)
        try:
            import libsql
        except ImportError as e:
            raise ImportError(
                "Пакет libsql не установлен. В requirements.txt: libsql==0.1.11, затем Redeploy."
            ) from e
        try:
            # Встроенная реплика: читаем/пишем локально в /tmp, иногда синкаем в Turso
            _DB_CONN = libsql.connect(
                str(_TURSO_LOCAL),
                sync_url=url,
                auth_token=token,
            )
            try:
                _DB_CONN.sync()
            except Exception:
                pass
            return _ConnProxy(_DB_CONN, is_cloud=True)
        except Exception:
            # fallback: прямое облачное соединение (медленнее)
            try:
                _DB_CONN = libsql.connect(database=url, auth_token=token)
                return _ConnProxy(_DB_CONN, is_cloud=True)
            except Exception as e:
                raise RuntimeError(f"Не удалось подключиться к Turso: {e}") from e

    return sqlite3.connect(DB_PATH, check_same_thread=False)


MEAL_TYPES = {
    "breakfast": "🌅 Завтрак",
    "lunch": "☀️ Обед",
    "dinner": "🌙 Ужин",
    "snack": "🍎 Перекус",
    "other": "📋 Другое",
}


def init_db():
    """Создаёт таблицы один раз за жизнь процесса (не на каждый клик)."""
    global _DB_INITED
    if _DB_INITED:
        return
    _init_db_schema()
    _DB_INITED = True


def _init_db_schema():
    conn = get_conn()
    cur = conn.cursor()

    # ---- Пользователи ----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            pin TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # ---- Таблицы данных (с user_id) ----
    # Пользователи создаются только через регистрацию (пустой старт для нового друга)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            date TEXT NOT NULL,
            product_name TEXT NOT NULL,
            barcode TEXT,
            amount_g REAL NOT NULL,
            calories REAL,
            proteins REAL,
            fats REAL,
            carbs REAL,
            meal_type TEXT DEFAULT 'other',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            product_name TEXT NOT NULL,
            barcode TEXT,
            calories_100g REAL,
            proteins_100g REAL,
            fats_100g REAL,
            carbs_100g REAL,
            last_amount REAL DEFAULT 100,
            times_used INTEGER DEFAULT 1,
            UNIQUE(user_id, product_name)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER NOT NULL DEFAULT 1,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS weight_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            date TEXT NOT NULL,
            weight_kg REAL NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, date)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS water_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            date TEXT NOT NULL,
            amount_ml REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    def _table_columns(cursor, table_name):
        """Список колонок таблицы (sqlite / libsql / Turso)."""
        try:
            cursor.execute(f"PRAGMA table_info({table_name})")
            rows = cursor.fetchall()
            # PRAGMA: cid, name, type, ...
            return [r[1] for r in rows] if rows else []
        except Exception:
            return []

    def _add_column_if_missing(cursor, table, col_name, col_def):
        cols = _table_columns(cursor, table)
        if col_name in cols:
            return
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        except Exception:
            # колонка уже есть / Turso / старый sqlite — игнор
            pass

    # Миграции старых локальных баз (на Turso при первом запуске таблицы уже с user_id)
    _add_column_if_missing(cur, "meals", "user_id", "user_id INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(cur, "favorites", "user_id", "user_id INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(cur, "water_log", "user_id", "user_id INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(cur, "weight_log", "user_id", "user_id INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(cur, "meals", "meal_type", "meal_type TEXT DEFAULT 'other'")

    # Старые settings без user_id → перенос (только локальные древние БД)
    try:
        scols = _table_columns(cur, "settings")
        if scols and "user_id" not in scols and "key" in scols:
            cur.execute("SELECT key, value FROM settings")
            old = cur.fetchall()
            cur.execute("DROP TABLE settings")
            cur.execute("""
                CREATE TABLE settings (
                    user_id INTEGER NOT NULL DEFAULT 1,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (user_id, key)
                )
            """)
            for k, v in old:
                cur.execute(
                    "INSERT OR IGNORE INTO settings (user_id, key, value) VALUES (1, ?, ?)",
                    (k, v),
                )
    except Exception:
        pass

    conn.commit()
    conn.close()


def list_users():
    conn = get_conn()
    df = pd.read_sql_query("SELECT id, name FROM users ORDER BY id", conn)
    conn.close()
    return df


def create_user(name, pin=None):
    name = (name or "").strip()
    if not name:
        return None, "Введите имя"
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (name, pin) VALUES (?, ?)", (name, pin or None))
        conn.commit()
        uid = cur.lastrowid
        conn.close()
        return uid, None
    except sqlite3.IntegrityError:
        conn.close()
        return None, "Такое имя уже есть"


def get_user_name(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "?"


def check_user_pin(user_id, pin):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT pin FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    stored = row[0]
    if not stored:
        return True  # пин не задан
    return str(stored) == str(pin or "")


def add_weight(user_id, weight_kg, log_date=None, note=None):
    if log_date is None:
        log_date = date.today().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO weight_log (user_id, date, weight_kg, note) VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, date) DO UPDATE SET weight_kg = excluded.weight_kg, note = excluded.note""",
        (user_id, log_date, weight_kg, note)
    )
    conn.commit()
    conn.close()


def get_latest_weight(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT weight_kg, date FROM weight_log WHERE user_id = ? ORDER BY date DESC LIMIT 1",
        (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    return (row[0], row[1]) if row else (None, None)


def get_weight_history(user_id, days=90):
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT date, weight_kg, note FROM weight_log
           WHERE user_id = ? ORDER BY date DESC LIMIT ?""",
        conn, params=(user_id, days,)
    )
    conn.close()
    return df


def delete_weight(user_id, log_date):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM weight_log WHERE user_id = ? AND date = ?", (user_id, log_date))
    conn.commit()
    conn.close()


def add_water(user_id, amount_ml, log_date=None):
    if log_date is None:
        log_date = date.today().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO water_log (user_id, date, amount_ml) VALUES (?, ?, ?)",
        (user_id, log_date, amount_ml)
    )
    conn.commit()
    conn.close()


def get_water_today(user_id, for_date=None):
    if for_date is None:
        for_date = date.today().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(amount_ml), 0) FROM water_log WHERE user_id = ? AND date = ?",
        (user_id, for_date,)
    )
    total = cur.fetchone()[0]
    conn.close()
    return float(total or 0)


def get_water_entries(user_id, for_date=None):
    if for_date is None:
        for_date = date.today().isoformat()
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT id, amount_ml, created_at FROM water_log
           WHERE user_id = ? AND date = ? ORDER BY created_at DESC""",
        conn, params=(user_id, for_date,)
    )
    conn.close()
    return df


def delete_water_entry(entry_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM water_log WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()


def water_goal_from_weight(weight_kg):
    """Норма воды ≈ 35 мл на кг веса (округление до 100 мл)"""
    if not weight_kg or weight_kg <= 0:
        return 2000  # дефолт
    return int(round(weight_kg * 35 / 100) * 100)


def import_meals_csv(df: pd.DataFrame, mode="append"):
    """
    Импорт дневника из CSV.
    Ожидаемые колонки: date, meal_type, product_name, amount_g, calories, proteins, fats, carbs
    mode: append — добавить, replace_dates — удалить дни из файла и вставить заново
    """
    required = {"date", "product_name", "amount_g"}
    cols = {c.lower().strip(): c for c in df.columns}
    # нормализуем имена колонок
    df = df.rename(columns={c: c.lower().strip() for c in df.columns})

    if not required.issubset(set(df.columns)):
        missing = required - set(df.columns)
        return 0, f"Нет колонок: {', '.join(missing)}"

    # defaults
    for col in ["meal_type", "calories", "proteins", "fats", "carbs", "barcode"]:
        if col not in df.columns:
            df[col] = None if col == "barcode" else (0 if col != "meal_type" else "other")

    df["meal_type"] = df["meal_type"].fillna("other").astype(str).str.lower().str.strip()
    # русские названия типов → ключи
    mt_map = {
        "завтрак": "breakfast", "breakfast": "breakfast",
        "обед": "lunch", "lunch": "lunch",
        "ужин": "dinner", "dinner": "dinner",
        "перекус": "snack", "snack": "snack",
        "другое": "other", "other": "other",
    }
    df["meal_type"] = df["meal_type"].map(lambda x: mt_map.get(x, "other"))

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["date", "product_name"])
    df["amount_g"] = pd.to_numeric(df["amount_g"], errors="coerce").fillna(0)
    for col in ["calories", "proteins", "fats", "carbs"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    conn = get_conn()
    cur = conn.cursor()

    # user_id передаётся через df.attrs или глобально — см. вызов import_meals_csv(user_id=...)
    user_id = getattr(df, "attrs", {}).get("user_id", 1)

    if mode == "replace_dates":
        dates = df["date"].unique().tolist()
        for d in dates:
            cur.execute("DELETE FROM meals WHERE user_id = ? AND date = ?", (user_id, d))

    count = 0
    for _, row in df.iterrows():
        cur.execute(
            """INSERT INTO meals (user_id, date, product_name, barcode, amount_g, calories, proteins, fats, carbs, meal_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                row["date"],
                str(row["product_name"]).strip(),
                None,
                float(row["amount_g"]),
                float(row["calories"]),
                float(row["proteins"]),
                float(row["fats"]),
                float(row["carbs"]),
                row["meal_type"],
            )
        )
        count += 1

    conn.commit()
    conn.close()
    return count, None


def import_meals_for_user(user_id, df, mode="append"):
    df = df.copy()
    df.attrs["user_id"] = user_id
    return import_meals_csv(df, mode=mode)


def calc_goals_from_weight(weight_kg, height_cm=None, age=None, sex="male", activity="moderate", goal="maintain"):
    """
    Простой расчёт BMR (Mifflin-St Jeor) + TDEE + цели.
    goal: maintain / lose / gain
    """
    if not weight_kg or weight_kg <= 0:
        return None

    # Если роста/возраста нет — грубая оценка по весу
    if height_cm and age and sex:
        if sex == "male":
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
        else:
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161
    else:
        # Упрощённо: ~22–24 ккал на кг для поддержания при средней активности
        bmr = weight_kg * 22

    activity_mult = {
        "sedentary": 1.2,
        "light": 1.375,
        "moderate": 1.55,
        "active": 1.725,
        "very_active": 1.9,
    }.get(activity, 1.55)

    tdee = bmr * activity_mult

    if goal == "lose":
        calories = tdee - 400
    elif goal == "gain":
        calories = tdee + 300
    else:
        calories = tdee

    calories = max(1200, round(calories / 10) * 10)  # не ниже 1200, округление

    # Белки: 1.6–2.2 г/кг в зависимости от цели
    prot_per_kg = 2.0 if goal == "lose" else (1.8 if goal == "maintain" else 1.6)
    proteins = round(weight_kg * prot_per_kg)

    # Жиры: ~0.8–1 г/кг
    fats = round(weight_kg * 0.9)

    # Углеводы — остаток
    # 1г белка/жира = 4/9 ккал, углеводы 4 ккал
    remaining = calories - proteins * 4 - fats * 9
    carbs = max(50, round(remaining / 4))

    return {
        "calories": int(calories),
        "proteins": int(proteins),
        "fats": int(fats),
        "carbs": int(carbs),
        "tdee": int(tdee),
        "bmr": int(bmr),
    }


def get_setting(user_id, key, default=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE user_id = ? AND key = ?", (user_id, key))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(user_id, key, value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)
           ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value""",
        (user_id, key, str(value))
    )
    conn.commit()
    conn.close()


def add_meal(user_id, product_name, barcode, amount_g, cal, prot, fat, carb, meal_date=None, meal_type="other"):
    if meal_date is None:
        meal_date = date.today().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO meals (user_id, date, product_name, barcode, amount_g, calories, proteins, fats, carbs, meal_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, meal_date, product_name, barcode, amount_g, cal, prot, fat, carb, meal_type)
    )
    conn.commit()
    conn.close()


def update_meal_amount(meal_id, new_amount):
    """Пересчитать КБЖУ пропорционально новому количеству грамм"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT amount_g, calories, proteins, fats, carbs FROM meals WHERE id = ?", (meal_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        conn.close()
        return False
    old_amt, cal, prot, fat, carb = row
    factor = new_amount / old_amt
    cur.execute(
        """UPDATE meals SET amount_g = ?, calories = ?, proteins = ?, fats = ?, carbs = ?
           WHERE id = ?""",
        (new_amount, (cal or 0) * factor, (prot or 0) * factor, (fat or 0) * factor, (carb or 0) * factor, meal_id)
    )
    conn.commit()
    conn.close()
    return True


def update_meal_type(meal_id, meal_type):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE meals SET meal_type = ? WHERE id = ?", (meal_type, meal_id))
    conn.commit()
    conn.close()


def get_meals(user_id, for_date=None):
    if for_date is None:
        for_date = date.today().isoformat()
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT id, product_name, amount_g, calories, proteins, fats, carbs, meal_type, created_at
           FROM meals WHERE user_id = ? AND date = ? ORDER BY
             CASE meal_type
               WHEN 'breakfast' THEN 1
               WHEN 'lunch' THEN 2
               WHEN 'dinner' THEN 3
               WHEN 'snack' THEN 4
               ELSE 5
             END, created_at""",
        conn, params=(user_id, for_date,)
    )
    conn.close()
    return df


def get_summary(user_id, for_date=None):
    if for_date is None:
        for_date = date.today().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT COALESCE(SUM(calories),0), COALESCE(SUM(proteins),0),
                  COALESCE(SUM(fats),0), COALESCE(SUM(carbs),0), COUNT(*)
           FROM meals WHERE user_id = ? AND date = ?""",
        (user_id, for_date,)
    )
    row = cur.fetchone()
    conn.close()
    return {
        "calories": row[0], "proteins": row[1],
        "fats": row[2], "carbs": row[3], "count": row[4]
    }


def delete_meal(meal_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_history(user_id, days=14):
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT date,
                  ROUND(SUM(calories), 0) as calories,
                  ROUND(SUM(proteins), 1) as proteins,
                  ROUND(SUM(fats), 1) as fats,
                  ROUND(SUM(carbs), 1) as carbs,
                  COUNT(*) as meals
           FROM meals WHERE user_id = ?
           GROUP BY date
           ORDER BY date DESC
           LIMIT ?""",
        conn, params=(user_id, days,)
    )
    conn.close()
    return df


def add_or_update_favorite(user_id, name, barcode, cal100, prot100, fat100, carb100, amount):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, times_used FROM favorites WHERE user_id = ? AND product_name = ?",
        (user_id, name),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            """UPDATE favorites SET last_amount = ?, times_used = times_used + 1,
               calories_100g = ?, proteins_100g = ?, fats_100g = ?, carbs_100g = ?
               WHERE id = ?""",
            (amount, cal100, prot100, fat100, carb100, row[0])
        )
    else:
        cur.execute(
            """INSERT INTO favorites (user_id, product_name, barcode, calories_100g, proteins_100g,
               fats_100g, carbs_100g, last_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, barcode, cal100, prot100, fat100, carb100, amount)
        )
    conn.commit()
    conn.close()


def get_favorites(user_id, limit=12):
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT product_name, barcode, calories_100g, proteins_100g, fats_100g, carbs_100g, last_amount, times_used
           FROM favorites WHERE user_id = ? ORDER BY times_used DESC, product_name LIMIT ?""",
        conn, params=(user_id, limit,)
    )
    conn.close()
    return df


def get_recent_products(user_id, limit=8):
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT product_name,
                  ROUND(AVG(amount_g), 0) as avg_amount,
                  ROUND(AVG(calories * 100.0 / NULLIF(amount_g, 0)), 1) as cal100,
                  ROUND(AVG(proteins * 100.0 / NULLIF(amount_g, 0)), 1) as prot100,
                  ROUND(AVG(fats * 100.0 / NULLIF(amount_g, 0)), 1) as fat100,
                  ROUND(AVG(carbs * 100.0 / NULLIF(amount_g, 0)), 1) as carb100,
                  COUNT(*) as cnt
           FROM meals
           WHERE user_id = ? AND date >= date('now', '-7 days')
           GROUP BY product_name
           ORDER BY MAX(created_at) DESC
           LIMIT ?""",
        conn, params=(user_id, limit,)
    )
    conn.close()
    return df


def remove_favorite(user_id, name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM favorites WHERE user_id = ? AND product_name = ?", (user_id, name))
    conn.commit()
    conn.close()


# ==================== ЛОКАЛЬНАЯ БАЗА (на случай если OFF недоступен) ====================
# Примерные значения КБЖУ на 100 г (средние по популярным продуктам)
LOCAL_PRODUCTS = [
    {"name": "Творог 5%", "calories": 121, "proteins": 17.2, "fats": 5.0, "carbs": 1.8},
    {"name": "Творог 9%", "calories": 159, "proteins": 16.7, "fats": 9.0, "carbs": 2.0},
    {"name": "Творог 0%", "calories": 71, "proteins": 16.5, "fats": 0.1, "carbs": 1.3},
    {"name": "Творог зернёный", "calories": 105, "proteins": 12.0, "fats": 4.0, "carbs": 3.5},
    {"name": "Куриная грудка (сырая)", "calories": 113, "proteins": 23.6, "fats": 1.9, "carbs": 0.4},
    {"name": "Куриная грудка (варёная)", "calories": 137, "proteins": 29.8, "fats": 1.8, "carbs": 0.5},
    {"name": "Куриное бедро (без кожи)", "calories": 144, "proteins": 18.0, "fats": 8.0, "carbs": 0},
    {"name": "Яйцо куриное", "calories": 157, "proteins": 12.7, "fats": 11.5, "carbs": 0.7},
    {"name": "Яичный белок", "calories": 52, "proteins": 11.1, "fats": 0.2, "carbs": 0.7},
    {"name": "Рис белый варёный", "calories": 130, "proteins": 2.7, "fats": 0.3, "carbs": 28.0},
    {"name": "Рис бурый варёный", "calories": 110, "proteins": 2.6, "fats": 0.9, "carbs": 23.0},
    {"name": "Гречка варёная", "calories": 101, "proteins": 4.2, "fats": 1.1, "carbs": 18.6},
    {"name": "Овсянка на воде", "calories": 88, "proteins": 3.0, "fats": 1.7, "carbs": 15.0},
    {"name": "Овсянка на молоке", "calories": 102, "proteins": 3.2, "fats": 2.5, "carbs": 16.0},
    {"name": "Банан", "calories": 89, "proteins": 1.1, "fats": 0.3, "carbs": 22.8},
    {"name": "Яблоко", "calories": 52, "proteins": 0.3, "fats": 0.2, "carbs": 13.8},
    {"name": "Апельсин", "calories": 47, "proteins": 0.9, "fats": 0.1, "carbs": 11.8},
    {"name": "Клубника", "calories": 33, "proteins": 0.7, "fats": 0.3, "carbs": 7.7},
    {"name": "Авокадо", "calories": 160, "proteins": 2.0, "fats": 14.7, "carbs": 8.5},
    {"name": "Картофель варёный", "calories": 82, "proteins": 1.8, "fats": 0.1, "carbs": 18.5},
    {"name": "Картофель жареный", "calories": 192, "proteins": 2.8, "fats": 9.5, "carbs": 23.4},
    {"name": "Брокколи", "calories": 34, "proteins": 2.8, "fats": 0.4, "carbs": 6.6},
    {"name": "Огурец", "calories": 15, "proteins": 0.8, "fats": 0.1, "carbs": 3.6},
    {"name": "Помидор", "calories": 18, "proteins": 0.9, "fats": 0.2, "carbs": 3.9},
    {"name": "Морковь", "calories": 41, "proteins": 0.9, "fats": 0.2, "carbs": 9.6},
    {"name": "Лосось (сырой)", "calories": 208, "proteins": 20.0, "fats": 13.0, "carbs": 0},
    {"name": "Тунец консервированный", "calories": 96, "proteins": 21.0, "fats": 1.0, "carbs": 0},
    {"name": "Говядина постная", "calories": 158, "proteins": 22.0, "fats": 7.0, "carbs": 0},
    {"name": "Свинина постная", "calories": 143, "proteins": 21.0, "fats": 6.0, "carbs": 0},
    {"name": "Молоко 2.5%", "calories": 52, "proteins": 2.9, "fats": 2.5, "carbs": 4.7},
    {"name": "Молоко 3.2%", "calories": 60, "proteins": 2.9, "fats": 3.2, "carbs": 4.7},
    {"name": "Кефир 1%", "calories": 40, "proteins": 3.0, "fats": 1.0, "carbs": 4.0},
    {"name": "Йогурт натуральный", "calories": 66, "proteins": 5.0, "fats": 3.2, "carbs": 3.5},
    {"name": "Сыр российский", "calories": 363, "proteins": 23.0, "fats": 30.0, "carbs": 0.3},
    {"name": "Сыр моцарелла", "calories": 280, "proteins": 22.0, "fats": 22.0, "carbs": 2.2},
    {"name": "Хлеб белый", "calories": 265, "proteins": 7.6, "fats": 3.2, "carbs": 49.0},
    {"name": "Хлеб цельнозерновой", "calories": 247, "proteins": 8.5, "fats": 3.5, "carbs": 41.0},
    {"name": "Макароны варёные", "calories": 131, "proteins": 5.0, "fats": 0.5, "carbs": 25.0},
    {"name": "Масло сливочное", "calories": 748, "proteins": 0.5, "fats": 82.5, "carbs": 0.8},
    {"name": "Масло оливковое", "calories": 884, "proteins": 0, "fats": 100.0, "carbs": 0},
    {"name": "Арахисовая паста", "calories": 588, "proteins": 25.0, "fats": 50.0, "carbs": 20.0},
    {"name": "Шоколад тёмный 70%", "calories": 598, "proteins": 7.8, "fats": 42.6, "carbs": 45.9},
    {"name": "Мёд", "calories": 304, "proteins": 0.3, "fats": 0, "carbs": 82.4},
    {"name": "Сахар", "calories": 387, "proteins": 0, "fats": 0, "carbs": 99.8},
    {"name": "Орехи грецкие", "calories": 654, "proteins": 15.2, "fats": 65.2, "carbs": 13.7},
    {"name": "Миндаль", "calories": 579, "proteins": 21.2, "fats": 49.9, "carbs": 21.6},
    {"name": "Протеин сывороточный (порошок)", "calories": 380, "proteins": 80.0, "fats": 5.0, "carbs": 5.0},
]


def search_local(query: str):
    """Поиск по локальной базе"""
    q = query.lower().strip()
    results = []
    for p in LOCAL_PRODUCTS:
        if q in p["name"].lower():
            # Приводим к формату, похожему на OFF
            results.append({
                "product_name": p["name"],
                "brands": "локальная база",
                "code": None,
                "nutriments": {
                    "energy-kcal_100g": p["calories"],
                    "proteins_100g": p["proteins"],
                    "fat_100g": p["fats"],
                    "carbohydrates_100g": p["carbs"],
                }
            })
    return results


# ==================== ПОИСК ====================
def search_products(query: str, page_size: int = 12):
    """
    Поиск продуктов.
    Сначала пробуем Open Food Facts, при ошибке — локальную базу.
    Возвращает (products_list, error_or_info_message)
    """
    if not query or len(query.strip()) < 2:
        return [], "Введите минимум 2 символа"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    params = {
        "search_terms": query.strip(),
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "page_size": page_size,
    }

    # --- Пробуем Open Food Facts (с одной повторной попыткой) ---
    last_error = None
    for attempt in range(2):
        try:
            r = requests.get(SEARCH_URL, params=params, headers=headers, timeout=12)
            if r.status_code in (503, 429, 502, 504):
                last_error = f"Сервер Open Food Facts вернул {r.status_code}"
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                break
            r.raise_for_status()
            data = r.json()
            products = data.get("products") or []
            if products:
                return products, None
            # Ничего не нашли в OFF — попробуем локально
            break
        except requests.exceptions.Timeout:
            last_error = "Таймаут соединения с Open Food Facts"
        except requests.exceptions.ConnectionError:
            last_error = "Нет подключения к интернету"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        if attempt == 0:
            time.sleep(1.2)

    # --- Fallback: локальная база ---
    local = search_local(query)
    if local:
        msg = "Open Food Facts недоступен → показаны результаты из локальной базы"
        if last_error:
            msg = f"{last_error}. Показаны результаты из локальной базы"
        return local, msg

    if last_error:
        return [], f"{last_error}. В локальной базе тоже ничего не найдено. Добавьте продукт вручную."
    return [], "Ничего не найдено ни в Open Food Facts, ни в локальной базе"


def extract_nutrients(product):
    nutr = product.get("nutriments") or {}

    def val(*keys):
        for k in keys:
            v = nutr.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    return {
        "calories": val("energy-kcal_100g", "energy-kcal"),
        "proteins": val("proteins_100g", "proteins"),
        "fats": val("fat_100g", "fat"),
        "carbs": val("carbohydrates_100g", "carbohydrates"),
    }


# ==================== БЭКАП / ВОССТАНОВЛЕНИЕ ====================
def get_db_bytes():
    """Сырые байты файла базы (для скачивания бэкапа)."""
    init_db()
    if not DB_PATH.exists():
        return None
    # checkpoint чтобы WAL сбросил в основной файл
    try:
        conn = get_conn()
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        conn.close()
    except Exception:
        pass
    return DB_PATH.read_bytes()


def restore_db_from_bytes(data: bytes):
    """Полностью заменить nutrition.db из бэкапа."""
    try:
        for suffix in ("-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
        DB_PATH.write_bytes(data)
        init_db()
        return True, None
    except Exception as e:
        return False, str(e)


# ==================== UI ====================
def show_login_screen():
    """Экран входа / регистрации — друг видит пустой старт и создаёт свой аккаунт."""
    st.markdown('<p class="main-header">Трекер питания</p>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Войдите или создайте свой аккаунт</p>', unsafe_allow_html=True)

    users_df = list_users()
    cloud = is_cloud_db()
    if cloud:
        st.success("☁️ Облачная база Turso подключена — данные сохраняются постоянно.")
    elif users_df.empty:
        st.warning(
            "⚠️ Аккаунтов нет. Без Turso на Streamlit Cloud база **стирается** после простоя. "
            "Настройте облако (см. README) или восстановите бэкап `.db` ниже."
        )

    tab_login, tab_reg, tab_backup = st.tabs(["🔑 Войти", "✨ Создать аккаунт", "💾 Бэкап"])

    with tab_reg:
        st.markdown("#### Новый аккаунт")
        st.caption("Свой дневник, вес и цели — никто другой их не изменит")
        reg_name = st.text_input("Имя / ник", key="reg_name", placeholder="Например: Маша")
        reg_pin = st.text_input("Пин-код (рекомендуется)", type="password", key="reg_pin",
                               placeholder="4+ цифры или буквы")
        reg_pin2 = st.text_input("Повторите пин", type="password", key="reg_pin2")
        if st.button("Зарегистрироваться", type="primary", use_container_width=True, key="btn_reg"):
            if not reg_name or not reg_name.strip():
                st.error("Введите имя")
            elif reg_pin and reg_pin != reg_pin2:
                st.error("Пины не совпадают")
            else:
                nid, err = create_user(reg_name.strip(), reg_pin or None)
                if err:
                    st.error(err)
                else:
                    st.session_state.logged_in = True
                    st.session_state.my_user_id = nid
                    st.session_state.view_user_id = nid
                    st.success(f"Аккаунт «{reg_name.strip()}» создан!")
                    time.sleep(0.6)
                    st.rerun()

    with tab_login:
        st.markdown("#### Вход")
        if users_df.empty:
            st.info("Нет аккаунтов. Восстановите бэкап или создайте новый аккаунт.")
        else:
            user_ids = users_df["id"].tolist()
            user_names = {int(r["id"]): r["name"] for _, r in users_df.iterrows()}
            login_uid = st.selectbox(
                "Аккаунт",
                options=user_ids,
                format_func=lambda x: user_names.get(x, str(x)),
                key="login_uid",
            )
            login_pin = st.text_input("Пин-код", type="password", key="login_pin",
                                      placeholder="Если пин не задавали — оставьте пустым")
            if st.button("Войти", type="primary", use_container_width=True, key="btn_login"):
                if check_user_pin(int(login_uid), login_pin):
                    st.session_state.logged_in = True
                    st.session_state.my_user_id = int(login_uid)
                    st.session_state.view_user_id = int(login_uid)
                    st.rerun()
                else:
                    st.error("Неверный пин-код")

    with tab_backup:
        st.markdown("#### Восстановить данные из бэкапа")
        st.caption(
            "На Streamlit Cloud после простоя приложение перезапускается с **пустой** базой. "
            "Загрузите ранее скачанный файл `nutrition_backup_….db` — вернутся все аккаунты и записи."
        )
        up = st.file_uploader("Файл бэкапа (.db)", type=["db", "sqlite", "sqlite3"], key="restore_db")
        if up is not None:
            if st.button("Восстановить базу", type="primary", use_container_width=True, key="btn_restore"):
                ok, err = restore_db_from_bytes(up.read())
                if ok:
                    st.session_state.logged_in = False
                    st.success("База восстановлена! Теперь войдите во вкладке «Войти».")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(f"Ошибка: {err}")

        st.divider()
        st.markdown("#### Скачать текущую базу (если ещё есть данные)")
        db_bytes = get_db_bytes()
        if db_bytes and not users_df.empty:
            st.download_button(
                "💾 Скачать nutrition_backup.db",
                db_bytes,
                file_name=f"nutrition_backup_{date.today().isoformat()}.db",
                mime="application/octet-stream",
                use_container_width=True,
            )
        else:
            st.caption("Сейчас сохранять нечего — база пустая.")


def main():
    init_db()

    # ---- Не вошёл → только вход / регистрация ----
    if not st.session_state.get("logged_in"):
        show_login_screen()
        return

    users_df = list_users()
    user_ids = users_df["id"].tolist() if not users_df.empty else []
    user_names = {int(r["id"]): r["name"] for _, r in users_df.iterrows()} if not users_df.empty else {}

    if st.session_state.my_user_id not in user_ids:
        st.session_state.logged_in = False
        st.rerun()
        return

    if "view_user_id" not in st.session_state or st.session_state.view_user_id not in user_ids:
        st.session_state.view_user_id = st.session_state.my_user_id

    # ---- Sidebar ----
    with st.sidebar:
        st.markdown("### 🍎 Трекер питания")
        my_name = user_names.get(st.session_state.my_user_id, "?")
        st.caption(f"Вы вошли как **{my_name}**")
        if st.button("🚪 Выйти", use_container_width=True):
            st.session_state.logged_in = False
            st.session_state.pop("my_user_id", None)
            st.session_state.pop("view_user_id", None)
            st.rerun()

        st.divider()
        st.markdown("#### 👁 Чей дневник смотрю")
        view_idx = user_ids.index(st.session_state.view_user_id) if st.session_state.view_user_id in user_ids else 0
        view_uid = st.selectbox(
            "Профиль",
            options=user_ids,
            index=view_idx,
            format_func=lambda x: (
                f"{user_names.get(x, x)} (я)" if x == st.session_state.my_user_id
                else f"{user_names.get(x, x)} — только просмотр"
            ),
            key="sel_view_user",
        )
        st.session_state.view_user_id = int(view_uid)

        can_edit = st.session_state.my_user_id == st.session_state.view_user_id
        uid = st.session_state.view_user_id

        if can_edit:
            st.success("✏️ Ваш дневник — можно редактировать")
        else:
            st.warning(f"👁 Смотрите **{user_names.get(uid, '')}** — менять нельзя")

        st.divider()
        page = st.radio(
            "Навигация",
            ["🏠 Сегодня", "🔍 Поиск продуктов", "➕ Свой продукт", "📅 История", "⚖️ Вес", "🗂 База продуктов"],
            label_visibility="collapsed"
        )

        st.divider()
        st.markdown("#### Приём пищи")
        meal_type = st.selectbox(
            "Куда добавлять",
            options=list(MEAL_TYPES.keys()),
            format_func=lambda x: MEAL_TYPES[x],
            key="current_meal_type",
            disabled=not can_edit,
        )

        st.divider()
        latest_w, latest_w_date = get_latest_weight(uid)
        if latest_w:
            st.markdown(f"#### ⚖️ Вес: **{latest_w:.1f} кг**")
            st.caption(f"от {latest_w_date}")
        else:
            st.markdown("#### ⚖️ Вес не указан")
            st.caption("Укажите во вкладке «Вес»")

        st.divider()
        st.markdown("#### Цели на день")
        saved_cal = int(float(get_setting(uid, "goal_cal", 2000)))
        saved_prot = int(float(get_setting(uid, "goal_prot", 120)))
        saved_fat = int(float(get_setting(uid, "goal_fat", 70)))
        saved_carb = int(float(get_setting(uid, "goal_carb", 250)))

        goal_cal = st.number_input("Калории", min_value=0, value=saved_cal, step=50, key="g_cal")
        goal_prot = st.number_input("Белки (г)", min_value=0, value=saved_prot, step=5, key="g_prot")
        goal_fat = st.number_input("Жиры (г)", min_value=0, value=saved_fat, step=5, key="g_fat")
        goal_carb = st.number_input("Углеводы (г)", min_value=0, value=saved_carb, step=10, key="g_carb")

        if st.button("💾 Сохранить цели", use_container_width=True):
            set_setting(uid, "goal_cal", goal_cal)
            set_setting(uid, "goal_prot", goal_prot)
            set_setting(uid, "goal_fat", goal_fat)
            set_setting(uid, "goal_carb", goal_carb)
            st.toast("Цели сохранены", icon="💾")

        # Подсказка целей по весу
        if latest_w:
            with st.expander("📐 Рассчитать цели по весу"):
                sex = st.selectbox("Пол", ["male", "female"], format_func=lambda x: "Мужской" if x == "male" else "Женский", key="calc_sex")
                height = st.number_input("Рост (см)", min_value=100, max_value=250, value=int(float(get_setting(uid, "height_cm", 175))), key="calc_h")
                age = st.number_input("Возраст", min_value=14, max_value=100, value=int(float(get_setting(uid, "age", 30))), key="calc_age")
                activity = st.selectbox(
                    "Активность",
                    ["sedentary", "light", "moderate", "active", "very_active"],
                    index=2,
                    format_func=lambda x: {
                        "sedentary": "Сидячий",
                        "light": "Лёгкая",
                        "moderate": "Средняя",
                        "active": "Высокая",
                        "very_active": "Очень высокая",
                    }[x],
                    key="calc_act"
                )
                goal_type = st.selectbox(
                    "Цель",
                    ["lose", "maintain", "gain"],
                    index=1,
                    format_func=lambda x: {"lose": "Похудение (−400 ккал)", "maintain": "Поддержание", "gain": "Набор (+300 ккал)"}[x],
                    key="calc_goal"
                )
                if st.button("Рассчитать и применить", use_container_width=True):
                    set_setting(uid, "height_cm", height)
                    set_setting(uid, "age", age)
                    set_setting(uid, "sex", sex)
                    set_setting(uid, "activity", activity)
                    res = calc_goals_from_weight(latest_w, height, age, sex, activity, goal_type)
                    if res:
                        set_setting(uid, "goal_cal", res["calories"])
                        set_setting(uid, "goal_prot", res["proteins"])
                        set_setting(uid, "goal_fat", res["fats"])
                        set_setting(uid, "goal_carb", res["carbs"])
                        set_setting(uid, "tdee", res["tdee"])
                        st.success(f"Цели: {res['calories']} ккал · Б {res['proteins']} · Ж {res['fats']} · У {res['carbs']}")
                        st.caption(f"BMR ≈ {res['bmr']} · расход на поддержание (TDEE) ≈ {res['tdee']}")
                        time.sleep(0.8)
                        st.rerun()

        st.divider()
        st.markdown("#### 💾 Бэкап")
        st.caption("Streamlit Cloud стирает базу при «сне». Скачивайте бэкап!")
        db_bytes = get_db_bytes()
        if db_bytes:
            st.download_button(
                "💾 Скачать всю базу (.db)",
                db_bytes,
                file_name=f"nutrition_backup_{date.today().isoformat()}.db",
                mime="application/octet-stream",
                use_container_width=True,
                key="sidebar_db_backup",
            )
        if st.button("📥 Экспорт еды в CSV", use_container_width=True):
            conn = get_conn()
            export_df = pd.read_sql_query(
                """SELECT date, meal_type, product_name, amount_g, calories, proteins, fats, carbs
                   FROM meals WHERE user_id = ? ORDER BY date DESC, created_at""",
                conn, params=(uid,),
            )
            conn.close()
            if not export_df.empty:
                csv = export_df.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "Скачать CSV",
                    csv,
                    file_name=f"nutrition_export_{date.today().isoformat()}.csv",
                    mime="text/csv",
                    use_container_width=True,
                    key="sidebar_csv_dl",
                )
            else:
                st.caption("Пока нет данных для экспорта")

        st.divider()
        if is_cloud_db():
            st.caption("v2.4 · ☁️ Turso — данные в облаке")
        else:
            st.caption("v2.4 · локальная БД · на Cloud нужен Turso")

    # Выбранный день (для просмотра / планирования / правок)
    if "selected_date" not in st.session_state:
        st.session_state.selected_date = date.today()

    selected = st.session_state.selected_date
    selected_str = selected.isoformat()
    is_today = selected == date.today()
    is_future = selected > date.today()
    is_past = selected < date.today()

    # ---- Header ----
    st.markdown('<p class="main-header">Трекер питания</p>', unsafe_allow_html=True)
    day_label = "сегодня" if is_today else ("завтра" if selected == date.today() + timedelta(days=1) else selected.strftime("%d.%m.%Y"))
    profile_line = f"Профиль: **{user_names.get(uid, '')}**"
    if not can_edit:
        profile_line += " · 👁 только просмотр"
    st.markdown(f'<p class="sub-header">{profile_line} · {selected.strftime("%d %B %Y")} ({day_label})</p>', unsafe_allow_html=True)

    # ==================== СТРАНИЦА: СЕГОДНЯ ====================
    if page == "🏠 Сегодня":
        # ---- Переключатель дня ----
        # on_click выполняется в начале следующего прогона, ДО виджетов — без ошибки date_picker
        def _nav_prev():
            st.session_state.selected_date = st.session_state.selected_date - timedelta(days=1)
            st.session_state.date_picker = st.session_state.selected_date

        def _nav_today():
            st.session_state.selected_date = date.today()
            st.session_state.date_picker = date.today()

        def _nav_tomorrow():
            st.session_state.selected_date = date.today() + timedelta(days=1)
            st.session_state.date_picker = st.session_state.selected_date

        def _nav_next():
            st.session_state.selected_date = st.session_state.selected_date + timedelta(days=1)
            st.session_state.date_picker = st.session_state.selected_date

        if "date_picker" not in st.session_state:
            st.session_state.date_picker = st.session_state.selected_date

        st.markdown("#### 📅 День")
        d1, d2, d3, d4, d5 = st.columns([1, 1, 2, 1, 1])
        with d1:
            st.button("◀", use_container_width=True, help="Предыдущий день",
                      key="btn_prev_day", on_click=_nav_prev)
        with d2:
            st.button("Сегодня", use_container_width=True,
                      key="btn_today", on_click=_nav_today)
        with d3:
            picked = st.date_input("Дата", key="date_picker", label_visibility="collapsed")
            if picked != st.session_state.selected_date:
                st.session_state.selected_date = picked
        with d4:
            st.button("Завтра", use_container_width=True,
                      key="btn_tomorrow", on_click=_nav_tomorrow)
        with d5:
            st.button("▶", use_container_width=True, help="Следующий день",
                      key="btn_next_day", on_click=_nav_next)

        # актуальная дата после кнопок / календаря
        selected = st.session_state.selected_date
        selected_str = selected.isoformat()
        is_today = selected == date.today()
        is_future = selected > date.today()
        is_past = selected < date.today()

        if is_future:
            st.info("📌 Режим планирования — можно заранее добавить еду на этот день")
        elif is_past:
            st.caption("Редактирование прошлого дня")

        summary = get_summary(uid, selected_str)
        meals_df = get_meals(uid, selected_str)

        # Метрики — 2 ряда по 2 (удобно на телефоне)
        col1, col2 = st.columns(2)
        with col1:
            delta_cal = summary['calories'] - goal_cal
            st.metric("Съедено", f"{summary['calories']:.0f} ккал", f"{delta_cal:+.0f} к цели")
            st.metric("Белки", f"{summary['proteins']:.1f} г", f"из {goal_prot} г")
        with col2:
            st.metric("Жиры", f"{summary['fats']:.1f} г", f"из {goal_fat} г")
            st.metric("Углеводы", f"{summary['carbs']:.1f} г", f"из {goal_carb} г")

        if latest_w:
            prot_per_kg = summary["proteins"] / latest_w if latest_w else 0
            st.caption(f"Белки: **{prot_per_kg:.2f} г/кг** · записей: {summary['count']}")
        else:
            st.caption(f"Записей: {summary['count']}")

        # ---- Баланс: поддержание / съедено / остаток ----
        st.markdown("#### 🔥 Баланс калорий")
        eaten = summary["calories"]

        # TDEE (сколько примерно сжигает тело за день) — из профиля или от веса
        height_s = get_setting(uid, "height_cm")
        age_s = get_setting(uid, "age")
        sex_s = get_setting(uid, "sex", "male")
        act_s = get_setting(uid, "activity", "moderate")
        tdee = None
        if latest_w:
            res_m = calc_goals_from_weight(
                latest_w,
                float(height_s) if height_s else None,
                int(float(age_s)) if age_s else None,
                sex_s or "male",
                act_s or "moderate",
                "maintain",
            )
            if res_m:
                tdee = res_m["tdee"]

        if tdee:
            balance = tdee - eaten  # нужно для поддержания минус съедено
            b1, b2, b3 = st.columns(3)
            with b1:
                st.metric("Сжигание (TDEE)", f"{tdee} ккал", help="Оценка расхода на поддержание веса")
            with b2:
                st.metric("Съедено", f"{eaten:.0f} ккал")
            with b3:
                # плюс = ещё можно съесть до поддержания; минус = перебор относительно поддержания
                st.metric(
                    "Остаток",
                    f"{balance:+.0f} ккал",
                    help="TDEE − съедено. Плюс — ещё есть запас до поддержания, минус — выше поддержания",
                )

            # прогресс относительно поддержания
            pct_tdee = min(eaten / tdee, 1.5) if tdee else 0
            st.progress(min(pct_tdee, 1.0), text=f"От поддержания: {eaten:.0f} / {tdee} ккал ({eaten/tdee*100:.0f}%)")

            if balance > 50:
                st.caption(f"До поддержания веса можно ещё ≈ **{balance:.0f} ккал**")
            elif balance < -50:
                st.caption(f"Выше поддержания на ≈ **{abs(balance):.0f} ккал** (профицит)")
            else:
                st.caption("Около уровня поддержания веса")

            # сравнение с личной целью (похудение/набор)
            if goal_cal and abs(goal_cal - tdee) > 20:
                to_goal = goal_cal - eaten
                if to_goal > 0:
                    st.caption(f"До **личной цели** ({goal_cal} ккал): ещё **{to_goal:.0f} ккал**")
                else:
                    st.caption(f"Личная цель ({goal_cal} ккал) превышена на **{abs(to_goal):.0f} ккал**")
        else:
            st.info("Укажи вес во вкладке «⚖️ Вес» — появится расход на поддержание и остаток (нужно − съедено).")
            # без веса всё равно покажем относительно цели
            to_goal = goal_cal - eaten
            st.metric("До цели", f"{to_goal:+.0f} ккал", help="Цель − съедено")

        # Прогресс-бары к личной цели
        st.markdown("#### Прогресс к цели")
        pct = min(summary["calories"] / goal_cal, 1.0) if goal_cal else 0
        st.progress(pct, text=f"Ккал {pct*100:.0f}%")
        pct = min(summary["proteins"] / goal_prot, 1.0) if goal_prot else 0
        st.progress(pct, text=f"Белки {pct*100:.0f}%")
        pct = min(summary["fats"] / goal_fat, 1.0) if goal_fat else 0
        st.progress(pct, text=f"Жиры {pct*100:.0f}%")
        pct = min(summary["carbs"] / goal_carb, 1.0) if goal_carb else 0
        st.progress(pct, text=f"Углеводы {pct*100:.0f}%")

        # ---- Вода ----
        st.markdown("#### 💧 Вода")
        water_today = get_water_today(uid, selected_str)
        saved_water_goal = get_setting(uid, "water_goal_ml")
        if saved_water_goal:
            water_goal = int(float(saved_water_goal))
        elif latest_w:
            water_goal = water_goal_from_weight(latest_w)
        else:
            water_goal = 2000

        w_pct = min(water_today / water_goal, 1.0) if water_goal else 0
        st.progress(w_pct, text=f"{water_today:.0f} / {water_goal} мл ({w_pct*100:.0f}%) · {water_today/1000:.2f} л")
        if latest_w and not saved_water_goal:
            st.caption(f"Норма ≈ 35 мл/кг × {latest_w:.1f} кг = {water_goal} мл")

        if can_edit:
            wb1, wb2, wb3 = st.columns(3)
            with wb1:
                if st.button("+200 мл", key="water_200", use_container_width=True):
                    add_water(uid, 200, selected_str)
                    st.toast("+200 мл", icon="💧")
                    time.sleep(0.3)
                    st.rerun()
            with wb2:
                if st.button("+250 мл", key="water_250", use_container_width=True):
                    add_water(uid, 250, selected_str)
                    st.toast("+250 мл", icon="💧")
                    time.sleep(0.3)
                    st.rerun()
            with wb3:
                if st.button("+500 мл", key="water_500", use_container_width=True):
                    add_water(uid, 500, selected_str)
                    st.toast("+500 мл", icon="💧")
                    time.sleep(0.3)
                    st.rerun()
            if st.button("+1 литр", key="water_1000", use_container_width=True):
                add_water(uid, 1000, selected_str)
                st.toast("+1000 мл", icon="💧")
                time.sleep(0.3)
                st.rerun()
        else:
            st.caption("Добавление воды недоступно в режиме просмотра")

        with st.expander("💧 Своё количество / записи"):
            if can_edit:
                custom_ml = st.number_input("мл", min_value=50, max_value=2000, value=250, step=50, key="custom_water")
                if st.button("Добавить", key="add_custom_water"):
                    add_water(uid, custom_ml, selected_str)
                    st.toast(f"+{custom_ml} мл", icon="💧")
                    time.sleep(0.3)
                    st.rerun()
            else:
                st.caption("Только просмотр")

            entries = get_water_entries(uid, selected_str)
            if not entries.empty:
                st.caption("Записи за этот день:")
                for _, e in entries.iterrows():
                    ec1, ec2 = st.columns([4, 1])
                    ec1.write(f"{e['amount_ml']:.0f} мл · {str(e['created_at'])[11:16] if e['created_at'] else ''}")
                    if ec2.button("✕", key=f"del_water_{e['id']}"):
                        delete_water_entry(int(e["id"]))
                        st.rerun()

            new_goal = st.number_input(
                "Цель на день (мл)",
                min_value=500,
                max_value=6000,
                value=water_goal,
                step=100,
                key="set_water_goal"
            )
            if st.button("Сохранить цель воды"):
                set_setting(uid, "water_goal_ml", new_goal)
                st.toast("Цель воды сохранена")
                time.sleep(0.3)
                st.rerun()

        st.divider()

        # ---- Быстрое добавление: Избранное + Недавние ----
        favs = get_favorites(uid, 10)
        recent = get_recent_products(uid, 8)

        if not favs.empty or not recent.empty:
            st.markdown("#### ⚡ Быстрое добавление")

            if not favs.empty:
                st.caption("⭐ Избранное")
                cols = st.columns(2)
                for idx, row in favs.iterrows():
                    with cols[idx % 2]:
                        label = row["product_name"][:28] + ("…" if len(row["product_name"]) > 28 else "")
                        amt = int(row["last_amount"] or 100)
                        if st.button(f"⭐ {label} · {amt}г", key=f"fav_{idx}", use_container_width=True):
                            factor = amt / 100.0
                            add_meal(uid, 
                                row["product_name"], row["barcode"], amt,
                                (row["calories_100g"] or 0) * factor,
                                (row["proteins_100g"] or 0) * factor,
                                (row["fats_100g"] or 0) * factor,
                                (row["carbs_100g"] or 0) * factor,
                                meal_date=selected_str,
                                meal_type=meal_type
                            )
                            add_or_update_favorite(uid, 
                                row["product_name"], row["barcode"],
                                row["calories_100g"], row["proteins_100g"],
                                row["fats_100g"], row["carbs_100g"], amt
                            )
                            st.toast(f"✅ {amt} г «{row['product_name']}»", icon="🍎")
                            time.sleep(0.4)
                            st.rerun()

            if not recent.empty:
                st.caption("🕐 Недавние")
                cols = st.columns(2)
                for idx, row in recent.iterrows():
                    with cols[idx % 2]:
                        label = row["product_name"][:28] + ("…" if len(row["product_name"]) > 28 else "")
                        amt = int(row["avg_amount"] or 100)
                        if st.button(f"🕐 {label} · {amt}г", key=f"rec_{idx}", use_container_width=True):
                            factor = amt / 100.0
                            add_meal(uid, 
                                row["product_name"], None, amt,
                                (row["cal100"] or 0) * factor,
                                (row["prot100"] or 0) * factor,
                                (row["fat100"] or 0) * factor,
                                (row["carb100"] or 0) * factor,
                                meal_date=selected_str,
                                meal_type=meal_type
                            )
                            st.toast(f"✅ {amt} г «{row['product_name']}»", icon="🍎")
                            time.sleep(0.4)
                            st.rerun()

            st.divider()

        # ---- Дневник с группировкой и редактированием ----
        day_title = "сегодня" if is_today else selected.strftime("%d.%m.%Y")
        st.markdown(f"#### 📋 Дневник · {day_title}")
        if meals_df.empty:
            st.info("Пока пусто. Добавьте продукты через поиск или быстрые кнопки.")
        else:
            # Группируем по типу приёма пищи (карточки — удобно на телефоне)
            for mt_key, mt_label in MEAL_TYPES.items():
                group = meals_df[meals_df["meal_type"] == mt_key] if "meal_type" in meals_df.columns else pd.DataFrame()
                if group.empty:
                    continue

                group_cal = group["calories"].sum()
                st.markdown(f"**{mt_label}** · {group_cal:.0f} ккал")

                for _, m in group.iterrows():
                    c1, c2 = st.columns([5, 1])
                    with c1:
                        st.markdown(
                            f"**{m['product_name']}**  \n"
                            f"{m['amount_g']:.0f} г · {m['calories']:.0f} ккал · "
                            f"Б {m['proteins']:.1f} · Ж {m['fats']:.1f} · У {m['carbs']:.1f}"
                        )
                    with c2:
                        if st.button("✕", key=f"del_{m['id']}", help="Удалить", use_container_width=True):
                            delete_meal(int(m["id"]))
                            st.toast("Удалено", icon="🗑")
                            time.sleep(0.3)
                            st.rerun()

                st.markdown("")

            # Редактирование записи (граммы + приём пищи)
            with st.expander("✏️ Редактировать запись"):
                edit_ids = meals_df["id"].tolist()
                edit_id = st.selectbox(
                    "Запись",
                    options=edit_ids,
                    format_func=lambda x: f"#{x} — {meals_df.loc[meals_df['id']==x, 'product_name'].values[0]} ({meals_df.loc[meals_df['id']==x, 'amount_g'].values[0]:.0f} г)",
                    key="edit_meal_select"
                )
                row = meals_df.loc[meals_df["id"] == edit_id].iloc[0]
                current_amt = float(row["amount_g"])
                current_mt = row.get("meal_type", "other") or "other"

                new_amt = st.number_input("Количество (г)", min_value=1.0, value=current_amt, step=5.0, key="edit_amt")
                new_mt = st.selectbox(
                    "Приём пищи",
                    options=list(MEAL_TYPES.keys()),
                    index=list(MEAL_TYPES.keys()).index(current_mt) if current_mt in MEAL_TYPES else 4,
                    format_func=lambda x: MEAL_TYPES[x],
                    key="edit_mt"
                )
                if st.button("💾 Сохранить изменения", type="primary"):
                    changed = False
                    if abs(new_amt - current_amt) > 0.01:
                        if update_meal_amount(int(edit_id), new_amt):
                            changed = True
                    if new_mt != current_mt:
                        update_meal_type(int(edit_id), new_mt)
                        changed = True
                    if changed:
                        st.success("Сохранено")
                        time.sleep(0.4)
                        st.rerun()
                    else:
                        st.info("Ничего не изменилось")

            st.caption("✕ — удалить · ✏️ — граммы и приём пищи")

    # ==================== СТРАНИЦА: ПОИСК ====================
    elif page == "🔍 Поиск продуктов":
        st.markdown("#### 🔍 Поиск продуктов в Open Food Facts")
        if not can_edit:
            st.warning("👁 Чужой профиль — только просмотр. Чтобы писать данные, выберите себя в «Я (мой профиль)».")
        st.caption(f"Добавление в день: **{selected.strftime('%d.%m.%Y')}**")

        # Инициализация session state для результатов поиска
        if "search_results" not in st.session_state:
            st.session_state.search_results = []
        if "search_error" not in st.session_state:
            st.session_state.search_error = None
        if "last_query" not in st.session_state:
            st.session_state.last_query = ""

        with st.form("search_form", clear_on_submit=False):
            col_search, col_btn = st.columns([4, 1])
            with col_search:
                query = st.text_input(
                    "Название продукта",
                    placeholder="например: творог, banana, овсянка  (Enter = найти)",
                    label_visibility="collapsed",
                    key="search_query"
                )
            with col_btn:
                search_clicked = st.form_submit_button("Найти", type="primary", use_container_width=True)

        # Поиск по кнопке или Enter
        if search_clicked:
            if not query or len(query.strip()) < 2:
                st.warning("Введите минимум 2 символа")
                st.session_state.search_results = []
                st.session_state.search_error = None
            else:
                with st.spinner("Ищу продукты..."):
                    products, error = search_products(query)
                st.session_state.search_results = products
                st.session_state.search_error = error
                st.session_state.last_query = query

        # Показываем сообщение / результаты
        if st.session_state.search_error and not st.session_state.search_results:
            st.error(st.session_state.search_error)
            st.info("Можно добавить продукт вручную во вкладке «Свой продукт».")
        elif st.session_state.search_error and st.session_state.search_results:
            st.warning(st.session_state.search_error)
        if st.session_state.search_results:
            st.success(f"Найдено: {len(st.session_state.search_results)} продуктов по запросу «{st.session_state.last_query}»")

            for i, p in enumerate(st.session_state.search_results):
                name = p.get("product_name") or "Без названия"
                brands = p.get("brands") or ""
                code = p.get("code") or None
                nutr = extract_nutrients(p)
                full_name = name + (f" ({brands})" if brands and brands != "локальная база" else "")

                with st.container():
                    c1, c2 = st.columns([3, 1.4])
                    with c1:
                        brand_txt = f" · {brands}" if brands else ""
                        st.markdown(f"**{name}**{brand_txt}")
                        if code:
                            st.caption(f"Штрихкод: {code}")

                        cal = f"{nutr['calories']:.0f}" if nutr["calories"] is not None else "—"
                        prot = f"{nutr['proteins']:.1f}" if nutr["proteins"] is not None else "—"
                        fat = f"{nutr['fats']:.1f}" if nutr["fats"] is not None else "—"
                        carb = f"{nutr['carbs']:.1f}" if nutr["carbs"] is not None else "—"
                        st.markdown(f"на 100 г → **{cal}** ккал · Б {prot} г · Ж {fat} г · У {carb} г")

                    with c2:
                        # Быстрые кнопки граммов
                        q1, q2, q3, q4 = st.columns(4)
                        quick_amounts = [50, 100, 150, 200]
                        for qi, qa in enumerate(quick_amounts):
                            with [q1, q2, q3, q4][qi]:
                                if st.button(f"{qa}", key=f"q_{i}_{qa}", help=f"Добавить {qa} г"):
                                    factor = qa / 100.0
                                    cal_v = (nutr["calories"] or 0) * factor
                                    prot_v = (nutr["proteins"] or 0) * factor
                                    fat_v = (nutr["fats"] or 0) * factor
                                    carb_v = (nutr["carbs"] or 0) * factor
                                    add_meal(uid, full_name, code, qa, cal_v, prot_v, fat_v, carb_v, meal_date=selected_str, meal_type=meal_type)
                                    add_or_update_favorite(uid, 
                                        full_name, code,
                                        nutr["calories"], nutr["proteins"],
                                        nutr["fats"], nutr["carbs"], qa
                                    )
                                    st.toast(f"✅ {qa} г «{name}» → {MEAL_TYPES.get(meal_type, '')}", icon="🍎")
                                    time.sleep(0.4)
                                    st.rerun()

                        amount = st.number_input(
                            "Своё кол-во (г)",
                            min_value=1.0,
                            value=100.0,
                            step=10.0,
                            key=f"amt_{i}"
                        )
                        b1, b2 = st.columns(2)
                        with b1:
                            if st.button("➕", key=f"add_{i}", use_container_width=True, help="Добавить"):
                                factor = amount / 100.0
                                cal_v = (nutr["calories"] or 0) * factor
                                prot_v = (nutr["proteins"] or 0) * factor
                                fat_v = (nutr["fats"] or 0) * factor
                                carb_v = (nutr["carbs"] or 0) * factor
                                add_meal(uid, full_name, code, amount, cal_v, prot_v, fat_v, carb_v, meal_date=selected_str, meal_type=meal_type)
                                add_or_update_favorite(uid, 
                                    full_name, code,
                                    nutr["calories"], nutr["proteins"],
                                    nutr["fats"], nutr["carbs"], amount
                                )
                                st.toast(f"✅ {amount:.0f} г «{name}» → {MEAL_TYPES.get(meal_type, '')}", icon="🍎")
                                time.sleep(0.4)
                                st.rerun()
                        with b2:
                            if st.button("⭐", key=f"star_{i}", use_container_width=True, help="В избранное"):
                                add_or_update_favorite(uid, 
                                    full_name, code,
                                    nutr["calories"], nutr["proteins"],
                                    nutr["fats"], nutr["carbs"], amount
                                )
                                st.toast(f"⭐ «{name}» в избранном", icon="⭐")
                                time.sleep(0.4)
                                st.rerun()

                    st.divider()
        elif not search_clicked:
            st.info("Введите название продукта и нажмите кнопку «Найти»")

    # ==================== СТРАНИЦА: СВОЙ ПРОДУКТ ====================
    elif page == "➕ Свой продукт":
        st.markdown("#### ➕ Добавить свой продукт")
        st.caption("Если продукта нет в базе — введите КБЖУ вручную")

        with st.form("manual_form", clear_on_submit=True):
            name = st.text_input("Название продукта *", placeholder="Например: Домашний омлет")
            c1, c2 = st.columns(2)
            with c1:
                cal100 = st.number_input("Калории (ккал / 100 г)", min_value=0.0, value=0.0, step=1.0)
                prot100 = st.number_input("Белки (г / 100 г)", min_value=0.0, value=0.0, step=0.1)
            with c2:
                fat100 = st.number_input("Жиры (г / 100 г)", min_value=0.0, value=0.0, step=0.1)
                carb100 = st.number_input("Углеводы (г / 100 г)", min_value=0.0, value=0.0, step=0.1)

            amount = st.number_input("Сколько грамм съели *", min_value=1.0, value=100.0, step=10.0)

            submitted = st.form_submit_button("Добавить в дневник", type="primary", use_container_width=True)

            if submitted:
                if not name.strip():
                    st.error("Укажите название")
                else:
                    factor = amount / 100.0
                    add_meal(uid, 
                        name.strip(), None, amount,
                        cal100 * factor, prot100 * factor,
                        fat100 * factor, carb100 * factor,
                        meal_date=selected_str,
                        meal_type=meal_type
                    )
                    add_or_update_favorite(uid, 
                        name.strip(), None, cal100, prot100, fat100, carb100, amount
                    )
                    st.success(f"✅ Добавлено {amount:.0f} г «{name}» — {cal100*factor:.0f} ккал → {MEAL_TYPES.get(meal_type, '')}")
                    time.sleep(0.8)
                    st.rerun()

    # ==================== СТРАНИЦА: ИСТОРИЯ ====================
    elif page == "📅 История":
        st.markdown("#### 📅 История питания")
        days = st.slider("Показать последних дней", 7, 60, 14)

        hist = get_history(uid, days)
        if hist.empty:
            st.info("Пока нет данных.")
        else:
            # TDEE для баланса по дням
            height_s = get_setting(uid, "height_cm")
            age_s = get_setting(uid, "age")
            sex_s = get_setting(uid, "sex", "male")
            act_s = get_setting(uid, "activity", "moderate")
            tdee = None
            if latest_w:
                res_m = calc_goals_from_weight(
                    latest_w,
                    float(height_s) if height_s else None,
                    int(float(age_s)) if age_s else None,
                    sex_s or "male",
                    act_s or "moderate",
                    "maintain",
                )
                if res_m:
                    tdee = res_m["tdee"]

            hist_bal = hist.copy().sort_values("date")
            if tdee:
                # Остаток = TDEE − съедено: плюс = дефицит (сожгли «в минус к еде»), минус = профицит (набор)
                hist_bal["tdee"] = tdee
                hist_bal["balance"] = tdee - hist_bal["calories"]
                hist_bal["status"] = hist_bal["balance"].apply(
                    lambda x: "дефицит" if x > 50 else ("профицит" if x < -50 else "баланс")
                )

                st.markdown("##### Баланс по дням (TDEE − съедено)")
                st.caption(
                    f"Расход на поддержание (TDEE) ≈ **{tdee} ккал/день**. "
                    "Плюс = дефицит (ниже поддержания), минус = профицит (выше поддержания)."
                )

                # График остатка
                bal_chart = hist_bal.set_index("date")[["balance"]]
                st.bar_chart(bal_chart, color="#3b82f6")

                # Сводка за период
                total_def = hist_bal.loc[hist_bal["balance"] > 0, "balance"].sum()
                total_sur = hist_bal.loc[hist_bal["balance"] < 0, "balance"].sum()  # отрицательное
                net = hist_bal["balance"].sum()
                s1, s2, s3, s4 = st.columns(4)
                s1.metric("Σ дефицит", f"+{total_def:.0f} ккал", help="Сумма дней ниже поддержания")
                s2.metric("Σ профицит", f"{total_sur:.0f} ккал", help="Сумма дней выше поддержания")
                s3.metric("Итого за период", f"{net:+.0f} ккал", help="Общий баланс: плюс — в сумме дефицит")
                s4.metric("Ср. съедено", f"{hist_bal['calories'].mean():.0f}")

                st.markdown("##### Таблица по дням")
                show = hist_bal[["date", "calories", "tdee", "balance", "status", "proteins", "fats", "carbs", "meals"]].copy()
                show = show.rename(columns={
                    "date": "Дата",
                    "calories": "Съедено",
                    "tdee": "Сжигание",
                    "balance": "Остаток",
                    "status": "Итог",
                    "proteins": "Белки",
                    "fats": "Жиры",
                    "carbs": "Углеводы",
                    "meals": "Записей",
                })
                show["Остаток"] = show["Остаток"].round(0).astype(int)
                st.dataframe(show, use_container_width=True, hide_index=True)

                # Список понятным языком
                with st.expander("📋 По дням словами"):
                    for _, row in hist_bal.sort_values("date", ascending=False).iterrows():
                        bal = row["balance"]
                        if bal > 50:
                            st.write(f"**{row['date']}**: съедено {row['calories']:.0f} → дефицит **{bal:.0f} ккал** (ниже поддержания)")
                        elif bal < -50:
                            st.write(f"**{row['date']}**: съедено {row['calories']:.0f} → профицит **{abs(bal):.0f} ккал** (набор относительно поддержания)")
                        else:
                            st.write(f"**{row['date']}**: съедено {row['calories']:.0f} → около баланса")
            else:
                st.warning("Укажи вес во вкладке «⚖️ Вес» — тогда по каждому дню будет дефицит/профицит.")
                st.markdown("##### Калории по дням")
                chart_df = hist.copy().sort_values("date")
                st.bar_chart(chart_df.set_index("date")["calories"], color="#10b981")
                display = hist.copy().rename(columns={
                    "date": "Дата", "calories": "Ккал", "proteins": "Белки (г)",
                    "fats": "Жиры (г)", "carbs": "Углеводы (г)", "meals": "Записей"
                })
                st.dataframe(display, use_container_width=True, hide_index=True)

            st.divider()
            st.markdown("##### Средние БЖУ")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Ср. калории", f"{hist['calories'].mean():.0f}")
            m2.metric("Ср. белки", f"{hist['proteins'].mean():.1f} г")
            m3.metric("Ср. жиры", f"{hist['fats'].mean():.1f} г")
            m4.metric("Ср. углеводы", f"{hist['carbs'].mean():.1f} г")

            # Клик по дню — детальный просмотр
            st.markdown("##### 🔎 Разобрать конкретный день")
            day_options = hist["date"].tolist()
            selected_day = st.selectbox("Выберите день", options=day_options)
            if selected_day:
                day_meals = get_meals(uid, selected_day)
                day_sum = get_summary(uid, selected_day)
                st.markdown(f"**{selected_day}** · {day_sum['calories']:.0f} ккал · Б {day_sum['proteins']:.1f} · Ж {day_sum['fats']:.1f} · У {day_sum['carbs']:.1f}")

                if day_meals.empty:
                    st.caption("Нет записей")
                else:
                    for mt_key, mt_label in MEAL_TYPES.items():
                        group = day_meals[day_meals["meal_type"] == mt_key] if "meal_type" in day_meals.columns else pd.DataFrame()
                        if group.empty:
                            continue
                        st.markdown(f"*{mt_label}*")
                        for _, m in group.iterrows():
                            st.write(f"• {m['product_name']} — {m['amount_g']:.0f} г → {m['calories']:.0f} ккал")

    # ==================== СТРАНИЦА: ВЕС ====================
    elif page == "⚖️ Вес":
        st.markdown("#### ⚖️ Трекер веса")

        latest_w, latest_w_date = get_latest_weight(uid)
        hist_w = get_weight_history(uid, 120)

        # Текущий вес и изменение
        c1, c2, c3 = st.columns(3)
        with c1:
            if latest_w:
                st.metric("Текущий вес", f"{latest_w:.1f} кг", help=f"от {latest_w_date}")
            else:
                st.metric("Текущий вес", "—")
        with c2:
            if len(hist_w) >= 2:
                prev = hist_w.iloc[1]["weight_kg"]
                delta = latest_w - prev
                st.metric("Изменение", f"{delta:+.1f} кг", delta_color="inverse")
            else:
                st.metric("Изменение", "—")
        with c3:
            if len(hist_w) >= 2:
                first = hist_w.iloc[-1]["weight_kg"]
                total_delta = latest_w - first
                st.metric("За период", f"{total_delta:+.1f} кг", delta_color="inverse")
            else:
                st.metric("За период", "—")

        st.divider()

        # Добавить / обновить вес
        st.markdown("##### Записать вес")
        col_w1, col_w2, col_w3 = st.columns([2, 2, 1])
        with col_w1:
            w_val = st.number_input(
                "Вес (кг)",
                min_value=30.0,
                max_value=300.0,
                value=float(latest_w) if latest_w else 70.0,
                step=0.1,
                format="%.1f",
                key="weight_input"
            )
        with col_w2:
            w_date = st.date_input("Дата", value=date.today(), key="weight_date")
        with col_w3:
            st.write("")
            st.write("")
            if st.button("💾 Сохранить", type="primary", use_container_width=True):
                add_weight(uid, w_val, w_date.isoformat())
                st.toast(f"Вес {w_val:.1f} кг сохранён", icon="⚖️")
                time.sleep(0.4)
                st.rerun()

        st.divider()

        # График
        if not hist_w.empty and len(hist_w) >= 2:
            st.markdown("##### Динамика веса")
            chart_w = hist_w.copy().sort_values("date")
            st.line_chart(chart_w.set_index("date")["weight_kg"], color="#3b82f6")

        # Таблица
        if not hist_w.empty:
            st.markdown("##### История")
            for _, row in hist_w.iterrows():
                hc1, hc2, hc3 = st.columns([2, 2, 1])
                hc1.write(row["date"])
                hc2.write(f"**{row['weight_kg']:.1f} кг**")
                if hc3.button("✕", key=f"del_w_{row['date']}", help="Удалить"):
                    delete_weight(row["date"])
                    st.toast("Удалено")
                    time.sleep(0.3)
                    st.rerun()
        else:
            st.info("Пока нет записей веса. Укажите свой вес выше.")

        # Белки на кг — подсказка
        if latest_w:
            st.divider()
            st.markdown("##### С учётом веса")
            today_sum = get_summary(uid)
            prot_per_kg = today_sum["proteins"] / latest_w if latest_w else 0
            cal_per_kg = today_sum["calories"] / latest_w if latest_w else 0
            p1, p2 = st.columns(2)
            p1.metric("Белки сегодня", f"{prot_per_kg:.2f} г/кг")
            p2.metric("Калории сегодня", f"{cal_per_kg:.0f} ккал/кг")
            st.caption("Рекомендуется белка: 1.6–2.2 г на кг веса")

    # ==================== СТРАНИЦА: БАЗА ====================
    elif page == "🗂 База продуктов":
        st.markdown("#### 🗂 База данных")
        tab1, tab2, tab3 = st.tabs(["⭐ Избранное", "📋 Все записи", "⚙️ Управление"])

        # --- Избранное ---
        with tab1:
            favs = get_favorites(uid, 50)
            if favs.empty:
                st.info("Избранное пусто. Добавляйте продукты через поиск — они появятся здесь автоматически.")
            else:
                st.caption(f"Всего в избранном: {len(favs)}")
                for idx, row in favs.iterrows():
                    c1, c2 = st.columns([5, 1])
                    with c1:
                        st.markdown(
                            f"**{row['product_name']}**  \n"
                            f"{row['calories_100g'] or 0:.0f} ккал/100г · "
                            f"×{int(row['times_used'] or 1)} · обычно {int(row['last_amount'] or 100)} г"
                        )
                    with c2:
                        if st.button("🗑", key=f"rmfav_{idx}", help="Убрать", use_container_width=True):
                            remove_favorite(uid, row["product_name"])
                            st.toast("Удалено из избранного")
                            time.sleep(0.3)
                            st.rerun()
                    st.divider()

                st.divider()
                # Редактирование избранного
                with st.expander("✏️ Редактировать продукт в избранном"):
                    fav_names = favs["product_name"].tolist()
                    sel = st.selectbox("Продукт", fav_names, key="edit_fav_sel")
                    frow = favs[favs["product_name"] == sel].iloc[0]
                    nc = st.number_input("Ккал / 100г", value=float(frow["calories_100g"] or 0), key="ef_cal")
                    np_ = st.number_input("Белки / 100г", value=float(frow["proteins_100g"] or 0), key="ef_prot")
                    nf = st.number_input("Жиры / 100г", value=float(frow["fats_100g"] or 0), key="ef_fat")
                    ncarb = st.number_input("Углеводы / 100г", value=float(frow["carbs_100g"] or 0), key="ef_carb")
                    namt = st.number_input("Последнее кол-во (г)", value=float(frow["last_amount"] or 100), key="ef_amt")
                    if st.button("Сохранить", key="save_fav_edit"):
                        add_or_update_favorite(uid, sel, frow["barcode"], nc, np_, nf, ncarb, namt)
                        st.success("Обновлено")
                        time.sleep(0.4)
                        st.rerun()

        # --- Все записи ---
        with tab2:
            conn = get_conn()
            all_meals = pd.read_sql_query(
                """SELECT id, date, meal_type, product_name, amount_g, calories, proteins, fats, carbs
                   FROM meals ORDER BY date DESC, created_at DESC LIMIT 500""",
                conn
            )
            conn.close()

            if all_meals.empty:
                st.info("Записей пока нет.")
            else:
                st.caption(f"Показаны последние {len(all_meals)} записей")

                # Фильтры
                fcol1, fcol2 = st.columns(2)
                with fcol1:
                    filter_date = st.selectbox(
                        "Фильтр по дате",
                        options=["Все"] + sorted(all_meals["date"].unique().tolist(), reverse=True),
                        key="db_filter_date"
                    )
                with fcol2:
                    filter_mt = st.selectbox(
                        "Фильтр по приёму",
                        options=["Все"] + list(MEAL_TYPES.keys()),
                        format_func=lambda x: x if x == "Все" else MEAL_TYPES.get(x, x),
                        key="db_filter_mt"
                    )

                view = all_meals.copy()
                if filter_date != "Все":
                    view = view[view["date"] == filter_date]
                if filter_mt != "Все":
                    view = view[view["meal_type"] == filter_mt]

                # Карточки — удобно на телефоне
                for _, m in view.iterrows():
                    mt_options = list(MEAL_TYPES.keys())
                    cur_mt = m.get("meal_type") or "other"
                    if cur_mt not in mt_options:
                        cur_mt = "other"

                    st.markdown(
                        f"**{m['product_name']}**  \n"
                        f"{m['date']} · {m['amount_g']:.0f} г · {m['calories']:.0f} ккал"
                    )
                    bc1, bc2 = st.columns([3, 1])
                    with bc1:
                        new_mt = st.selectbox(
                            "Приём пищи",
                            options=mt_options,
                            index=mt_options.index(cur_mt),
                            format_func=lambda x: MEAL_TYPES[x],
                            key=f"db_mt_{m['id']}",
                            label_visibility="collapsed"
                        )
                        if new_mt != cur_mt:
                            update_meal_type(int(m["id"]), new_mt)
                            st.rerun()
                    with bc2:
                        if st.button("Удалить", key=f"db_del_{m['id']}", use_container_width=True):
                            delete_meal(int(m["id"]))
                            st.toast("Удалено")
                            time.sleep(0.3)
                            st.rerun()
                    st.divider()

        # --- Управление ---
        with tab3:
            st.markdown("##### 📥 Импорт CSV")
            st.caption(
                "Формат колонок: `date, meal_type, product_name, amount_g, calories, proteins, fats, carbs`\n\n"
                "meal_type: breakfast/lunch/dinner/snack (или завтрак/обед/ужин/перекус)"
            )
            uploaded = st.file_uploader("Выберите CSV файл", type=["csv"], key="csv_upload")
            import_mode = st.radio(
                "Режим импорта",
                ["append", "replace_dates"],
                format_func=lambda x: "Добавить к существующим" if x == "append" else "Заменить дни из файла (удалить старые записи этих дат)",
                key="import_mode"
            )
            if uploaded is not None:
                try:
                    # пробуем utf-8-sig (Excel), потом utf-8, потом cp1251
                    raw = uploaded.read()
                    df_imp = None
                    for enc in ("utf-8-sig", "utf-8", "cp1251"):
                        try:
                            import io
                            df_imp = pd.read_csv(io.BytesIO(raw), encoding=enc)
                            break
                        except Exception:
                            continue
                    if df_imp is None:
                        st.error("Не удалось прочитать CSV")
                    else:
                        st.dataframe(df_imp.head(10), use_container_width=True)
                        st.caption(f"Строк в файле: {len(df_imp)}")
                        if st.button("Импортировать", type="primary", key="do_import"):
                            n, err = import_meals_for_user(uid, df_imp, mode=import_mode)
                            if err:
                                st.error(err)
                            else:
                                st.success(f"Импортировано записей: {n}")
                                time.sleep(0.8)
                                st.rerun()
                except Exception as e:
                    st.error(f"Ошибка: {e}")

            st.divider()
            st.markdown("##### Опасная зона")
            st.caption("Действия необратимы")

            if st.button("🗑 Очистить дневник за сегодня", type="secondary"):
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("DELETE FROM meals WHERE date = ?", (date.today().isoformat(),))
                conn.commit()
                conn.close()
                st.success("Дневник за сегодня очищен")
                time.sleep(0.5)
                st.rerun()

            if st.button("🗑 Очистить всё избранное", type="secondary"):
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("DELETE FROM favorites")
                conn.commit()
                conn.close()
                st.success("Избранное очищено")
                time.sleep(0.5)
                st.rerun()

            st.divider()
            st.markdown("##### Статистика базы")
            conn = get_conn()
            total_meals = pd.read_sql_query("SELECT COUNT(*) as c FROM meals", conn).iloc[0]["c"]
            total_favs = pd.read_sql_query("SELECT COUNT(*) as c FROM favorites", conn).iloc[0]["c"]
            total_days = pd.read_sql_query("SELECT COUNT(DISTINCT date) as c FROM meals", conn).iloc[0]["c"]
            conn.close()
            s1, s2, s3 = st.columns(3)
            s1.metric("Всего записей", int(total_meals))
            s2.metric("Избранных", int(total_favs))
            s3.metric("Дней с записями", int(total_days))


if __name__ == "__main__":
    main()
