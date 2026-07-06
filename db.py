import sqlite3
import os

DB_NAME = "studio.db"

# SQL-скрипт схемы из документации (02_architecture.md)
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS Clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    allergies_info TEXT,
    loyalty_status TEXT DEFAULT 'standard'
        CHECK(loyalty_status IN ('standard', 'vip'))
);

CREATE TABLE IF NOT EXISTS Chefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rating REAL DEFAULT 0.0
        CHECK(rating >= 0.0 AND rating <= 5.0)
);

CREATE TABLE IF NOT EXISTS Slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time DATETIME NOT NULL,
    menu_name TEXT NOT NULL,
    chef_id INTEGER NOT NULL,
    capacity INTEGER NOT NULL DEFAULT 12
        CHECK(capacity IN (8, 12)),
    status TEXT DEFAULT 'active'
        CHECK(status IN ('active', 'cancelled_by_studio')),
    cancellation_reason TEXT,
    FOREIGN KEY (chef_id) REFERENCES Chefs(id)
);

CREATE TABLE IF NOT EXISTS Equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL UNIQUE,
    total_rental_sets INTEGER NOT NULL DEFAULT 0,
    booked_rental_sets INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (slot_id) REFERENCES Slots(id)
);

CREATE TABLE IF NOT EXISTS Bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    slot_id INTEGER NOT NULL,
    status TEXT DEFAULT 'confirmed'
        CHECK(status IN (
            'confirmed',
            'cancelled_by_client',
            'cancelled_by_studio'
        )),
    equipment_type TEXT NOT NULL
        CHECK(equipment_type IN ('own', 'rental')),
    allergies TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (client_id) REFERENCES Clients(id),
    FOREIGN KEY (slot_id) REFERENCES Slots(id)
);

CREATE INDEX IF NOT EXISTS idx_slots_start_time ON Slots(start_time);
CREATE INDEX IF NOT EXISTS idx_bookings_slot_status ON Bookings(slot_id, status);
CREATE INDEX IF NOT EXISTS idx_equipment_slot ON Equipment(slot_id);
"""

# Моковые данные из документации + дополнительные клиенты для тестов
SEED_DATA = {
    "chefs": [
        {"id": 1, "name": "Марко Росси", "rating": 4.9},
        {"id": 2, "name": "Анна Светлова", "rating": 4.5},
        {"id": 3, "name": "Дмитрий Волков", "rating": 3.8},
    ],
    # 10 клиентов — чтобы можно было забивать слоты в тестах
    "clients": [
        {"id": 101, "full_name": "Елена Тестова",   "phone": "+79990001101", "allergies_info": "Нет",            "loyalty_status": "standard"},
        {"id": 102, "full_name": "Иван Петров",     "phone": "+79990001102", "allergies_info": "Глютен",         "loyalty_status": "standard"},
        {"id": 103, "full_name": "Ольга Сидорова",  "phone": "+79990001103", "allergies_info": "Нет",            "loyalty_status": "vip"},
        {"id": 104, "full_name": "Сергей Иванов",   "phone": "+79990001104", "allergies_info": "Орехи",          "loyalty_status": "standard"},
        {"id": 105, "full_name": "Мария Кузнецова", "phone": "+79990001105", "allergies_info": "Нет",            "loyalty_status": "standard"},
        {"id": 106, "full_name": "Алексей Смирнов", "phone": "+79990001106", "allergies_info": "Лактоза",        "loyalty_status": "standard"},
        {"id": 107, "full_name": "Наталья Орлова",  "phone": "+79990001107", "allergies_info": "Нет",            "loyalty_status": "vip"},
        {"id": 108, "full_name": "Дмитрий Новиков", "phone": "+79990001108", "allergies_info": "Нет",            "loyalty_status": "standard"},
        {"id": 109, "full_name": "Анна Белова",     "phone": "+79990001109", "allergies_info": "Морепродукты",   "loyalty_status": "standard"},
        {"id": 110, "full_name": "Павел Морозов",   "phone": "+79990001110", "allergies_info": "Нет",            "loyalty_status": "standard"},
    ],
    "slots": [
        {
            "id": 1,
            "start_time": "2026-07-07T18:00:00",
            "menu_name": "Итальянская классика (Паста и пицца)",
            "chef_id": 1,
            "capacity": 12,
            "status": "active",
            "equipment": {"total_rental_sets": 8, "booked_rental_sets": 2},
        },
        {
            "id": 2,
            "start_time": "2026-07-08T19:00:00",
            "menu_name": "Азиатский фуршет (Вок и суши)",
            "chef_id": 2,
            "capacity": 8,
            "status": "active",
            "equipment": {"total_rental_sets": 5, "booked_rental_sets": 5},  # прокат полностью занят
        },
        {
            "id": 3,
            "start_time": "2026-07-10T14:00:00",
            "menu_name": "Французские десерты (Сложный класс)",
            "chef_id": 1,
            "capacity": 8,
            "status": "active",
            "equipment": {"total_rental_sets": 0, "booked_rental_sets": 0},  # проката нет вообще
        },
        {
            "id": 4,
            "start_time": "2026-07-12T18:00:00",
            "menu_name": "Грузинское застолье",
            "chef_id": 3,
            "capacity": 12,
            "status": "cancelled_by_studio",  # триггер для Empty State
            "cancellation_reason": "Срыв поставки продуктов",
            "equipment": {"total_rental_sets": 10, "booked_rental_sets": 0},
        },
    ],
}


def init_db():
    """
    Очищает старую БД (если есть), создаёт схему и заполняет её моковыми данными.
    Гарантирует чистое состояние перед каждым запуском тестов.
    """
    if os.path.exists(DB_NAME):
        os.remove(DB_NAME)

    conn = sqlite3.connect(DB_NAME)
    try:
        conn.executescript(SCHEMA_SQL)

        # Шефы
        for chef in SEED_DATA["chefs"]:
            conn.execute(
                "INSERT INTO Chefs (id, name, rating) VALUES (?, ?, ?)",
                (chef["id"], chef["name"], chef["rating"]),
            )

        # Клиенты
        for client in SEED_DATA["clients"]:
            conn.execute(
                "INSERT INTO Clients (id, full_name, phone, allergies_info, loyalty_status) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    client["id"],
                    client["full_name"],
                    client["phone"],
                    client["allergies_info"],
                    client["loyalty_status"],
                ),
            )

        # Слоты + экипировка
        for slot in SEED_DATA["slots"]:
            conn.execute(
                "INSERT INTO Slots (id, start_time, menu_name, chef_id, capacity, status, cancellation_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    slot["id"],
                    slot["start_time"],
                    slot["menu_name"],
                    slot["chef_id"],
                    slot["capacity"],
                    slot["status"],
                    slot.get("cancellation_reason"),
                ),
            )
            eq = slot["equipment"]
            conn.execute(
                "INSERT INTO Equipment (slot_id, total_rental_sets, booked_rental_sets) "
                "VALUES (?, ?, ?)",
                (slot["id"], eq["total_rental_sets"], eq["booked_rental_sets"]),
            )

        conn.commit()
        print(f"[init_db] База '{DB_NAME}' создана и заполнена.")
    except Exception as e:
        conn.rollback()
        print(f"[init_db] Ошибка: {e}")
        raise
    finally:
        conn.close()