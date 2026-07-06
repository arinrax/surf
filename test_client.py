# ============================================================
# test_client.py — Имитация работы клиента
# Проходит по всем сценариям: успехи + ошибки API
# ============================================================

import sqlite3
from datetime import datetime, timedelta

from db import DB_NAME, init_db
from api import get_slots, create_booking, cancel_booking


def print_response(title, response):
    """Красиво печатает ответ API в консоль."""
    print(f"\n[{title}]")
    if "error_code" in response:
        print(f"  ❌ Ошибка: {response['error_code']}")
        print(f"  Сообщение: {response['message']}")
    else:
        print(f"  ✅ Успех: {response}")


# ============================================================
# 1. Инициализация БД
# ============================================================
print("=" * 60)
print("ШАГ 1. Инициализация БД")
print("=" * 60)
init_db()


# ============================================================
# 2. GET /slots — список слотов на 7 дней
# ============================================================
print("\n" + "=" * 60)
print("ШАГ 2. GET /slots — список слотов")
print("=" * 60)
slots_resp = get_slots()
print(f"Найдено слотов: {len(slots_resp['slots'])}")
for s in slots_resp["slots"]:
    print(f"  • Слот {s['id']}: {s['menu_name']}")
    print(f"      Время: {s['start_time']}")
    print(f"      Шеф: {s['chef']['name']} (⭐ {s['chef']['rating']})")
    print(f"      Мест: {s['free_seats_count']} из {s['capacity']}")
    print(f"      Прокат: {'да' if s['is_rent_available'] else 'нет'}")

# Проверка Empty State: слот 4 (cancelled_by_studio) не должен попасть в выдачу
ids = [s["id"] for s in slots_resp["slots"]]
assert 4 not in ids, "Слот 4 (cancelled_by_studio) не должен возвращаться!"
print("✓ Отменённые студией слоты корректно исключены.")


# ============================================================
# 3. POST /bookings — успешная бронь
# ============================================================
print("\n" + "=" * 60)
print("ШАГ 3. POST /bookings — успешная бронь")
print("=" * 60)
res = create_booking(
    slot_id=1,
    client_id=101,
    equipment_choice="own",
    allergies="Нет",
)
print_response("Бронь слота 1, своя экипировка", res)


# ============================================================
# 4. POST /bookings — ошибка RENT_EXHAUSTED
# ============================================================
print("\n" + "=" * 60)
print("ШАГ 4. POST /bookings — прокат закончился")
print("=" * 60)
# Слот 2: total_rental_sets=5, booked_rental_sets=5 — проката нет
res = create_booking(
    slot_id=2,
    client_id=102,
    equipment_choice="rental",
    allergies="Глютен",
)
print_response("Слот 2, прокат (должен быть RENT_EXHAUSTED)", res)


# ============================================================
# 5. POST /bookings — ошибка SLOT_FULL
# ============================================================
print("\n" + "=" * 60)
print("ШАГ 5. POST /bookings — мест нет (SLOT_FULL)")
print("=" * 60)
# Слот 2 имеет capacity=8. Забьём его 8 бронями от разных клиентов.
# Клиент 102 уже забронировал (своя экипировка, т.к. проката нет).
# Добавим ещё 7 броней от клиентов 103..109 — это займёт все 8 мест.
for i, client_id in enumerate([103, 104, 105, 106, 107, 108, 109], start=1):
    create_booking(
        slot_id=2,
        client_id=client_id,
        equipment_choice="own",
        allergies="",
    )
    print(f"  Бронь #{i} от клиента {client_id} — ок")

# 9-я бронь (клиент 110) должна дать SLOT_FULL
res = create_booking(
    slot_id=2,
    client_id=110,
    equipment_choice="own",
    allergies="",
)
print_response("9-я бронь на слот 2 (должен быть SLOT_FULL)", res)


# ============================================================
# 6. DELETE /bookings/{id} — успешная отмена
# ============================================================
print("\n" + "=" * 60)
print("ШАГ 6. DELETE /bookings — успешная отмена")
print("=" * 60)
# Бронируем слот 3 (10 июля, далеко в будущем — отмена точно разрешена)
booking_resp = create_booking(
    slot_id=3,
    client_id=101,
    equipment_choice="own",
    allergies="",
)
booking_id = booking_resp["id"]
print(f"  Создана бронь id={booking_id}")

res = cancel_booking(booking_id)
print_response(f"Отмена брони {booking_id}", res)


# ============================================================
# 7. DELETE /bookings/{id} — ошибка CANCELLATION_TOO_LATE
# ============================================================
print("\n" + "=" * 60)
print("ШАГ 7. DELETE /bookings — правило 10 минут")
print("=" * 60)
# Создаём «горячий» слот: начинается через 5 минут.
# Для этого напрямую пишем в БД (тестовый хак).
hot_slot_time = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
conn = sqlite3.connect(DB_NAME)
conn.execute(
    "INSERT INTO Slots (start_time, menu_name, chef_id, capacity, status) "
    "VALUES (?, ?, ?, ?, ?)",
    (hot_slot_time, "Экспресс-класс (через 5 минут)", 1, 12, "active"),
)
hot_slot_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.execute(
    "INSERT INTO Equipment (slot_id, total_rental_sets, booked_rental_sets) VALUES (?, 5, 0)",
    (hot_slot_id,),
)
conn.commit()
conn.close()
print(f"  Создан горячий слот id={hot_slot_id}, старт в {hot_slot_time}")

# Бронируем его
booking_resp = create_booking(
    slot_id=hot_slot_id,
    client_id=101,
    equipment_choice="own",
    allergies="",
)
hot_booking_id = booking_resp["id"]
print(f"  Забронирован, booking_id={hot_booking_id}")

# Пытаемся отменить — должно сработать правило 10 минут
res = cancel_booking(hot_booking_id)
print_response(f"Попытка отмены брони {hot_booking_id} (должен быть CANCELLATION_TOO_LATE)", res)


# ============================================================
# Итог
# ============================================================
print("\n" + "=" * 60)
print("✅ Все сценарии отработаны.")
print("=" * 60)
