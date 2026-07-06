import sqlite3
from datetime import datetime, timedelta

from db import DB_NAME


def get_slots(start_date=None, end_date=None):
    """
    Возвращает список активных слотов на указанный диапазон дат.
    По умолчанию — 7 дней (сегодня + 6 дней вперёд).
    Слоты со статусом 'cancelled_by_studio' исключаются (Empty State).
    """
    if start_date is None:
        start_date = datetime.now().date().isoformat()
    if end_date is None:
        end_date = (datetime.now() + timedelta(days=6)).date().isoformat()

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row

    query = """
    SELECT
        s.id, s.start_time, s.menu_name, s.capacity, s.status,
        c.id AS chef_id, c.name AS chef_name, c.rating AS chef_rating,
        e.total_rental_sets, e.booked_rental_sets,
        (
            SELECT COUNT(*)
            FROM Bookings b
            WHERE b.slot_id = s.id AND b.status = 'confirmed'
        ) AS booked_seats
    FROM Slots s
    JOIN Chefs c ON s.chef_id = c.id
    LEFT JOIN Equipment e ON s.id = e.slot_id
    WHERE s.status = 'active'
      AND s.start_time >= ?
      AND s.start_time <= ?
    ORDER BY s.start_time ASC
    """

    rows = conn.execute(query, (start_date, end_date + "T23:59:59")).fetchall()
    conn.close()

    slots = []
    for r in rows:
        free_seats = r["capacity"] - r["booked_seats"]
        total_rent = r["total_rental_sets"] or 0
        booked_rent = r["booked_rental_sets"] or 0
        is_rent_available = (total_rent - booked_rent) > 0

        slots.append({
            "id": r["id"],
            "start_time": r["start_time"],
            "menu_name": r["menu_name"],
            "chef": {
                "id": r["chef_id"],
                "name": r["chef_name"],
                "rating": r["chef_rating"],
            },
            "capacity": r["capacity"],
            "free_seats_count": free_seats,
            "is_rent_available": is_rent_available,
            "status": r["status"],
        })

    return {"slots": slots}


# ============================================================
# POST /bookings
# ============================================================
def create_booking(slot_id, client_id, equipment_choice, allergies):
    """
    Создаёт бронь. Атомарно проверяет наличие мест и прокатных наборов.

    Параметры:
      slot_id           — ID слота
      client_id         — ID клиента
      equipment_choice  — 'own' (своя экипировка) или 'rental' (прокат)
      allergies         — строка с аллергиями (или пустая строка)

    Возвращает:
      При успехе:  {"id": <int>, "status": "confirmed", "message": "..."}
      При ошибке:  {"error_code": "<код>", "message": "<текст>"}
    """

    # Нормализуем выбор экипировки (защита от опечаток клиента)
    if equipment_choice not in ("own", "rental"):
        return {
            "error_code": "INVALID_EQUIPMENT",
            "message": "equipment_type должен быть 'own' или 'rental'",
        }

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    # Явное управление транзакцией — критично для атомарности проверок
    conn.isolation_level = None

    try:
        # Открываем транзакцию
        conn.execute("BEGIN")

        # ----------------------------------------------------------
        # 1. Проверка существования и статуса слота
        # ----------------------------------------------------------
        slot = conn.execute(
            "SELECT capacity, status FROM Slots WHERE id = ?",
            (slot_id,),
        ).fetchone()

        if not slot:
            raise ValueError("SLOT_NOT_FOUND")
        if slot["status"] != "active":
            raise ValueError("SLOT_CANCELLED")

        # ----------------------------------------------------------
        # 2. Проверка наличия свободных мест
        #    Считаем только подтверждённые брони (status = 'confirmed').
        #    Отменённые брони места НЕ занимают.
        # ----------------------------------------------------------
        booked = conn.execute(
            "SELECT COUNT(*) AS cnt FROM Bookings "
            "WHERE slot_id = ? AND status = 'confirmed'",
            (slot_id,),
        ).fetchone()["cnt"]

        if booked >= slot["capacity"]:
            raise ValueError("SLOT_FULL")

        # ----------------------------------------------------------
        # 3. Если выбран прокат — проверяем фонд экипировки
        #    total_rental_sets vs booked_rental_sets
        # ----------------------------------------------------------
        if equipment_choice == "rental":
            equip = conn.execute(
                "SELECT total_rental_sets, booked_rental_sets "
                "FROM Equipment WHERE slot_id = ?",
                (slot_id,),
            ).fetchone()

            if not equip:
                # На всякий случай: если записи в Equipment нет
                raise ValueError("RENT_EXHAUSTED")

            if equip["booked_rental_sets"] >= equip["total_rental_sets"]:
                raise ValueError("RENT_EXHAUSTED")

        # ----------------------------------------------------------
        # 4. Вставляем бронь
        # ----------------------------------------------------------
        cursor = conn.execute(
            "INSERT INTO Bookings (client_id, slot_id, equipment_type, allergies) "
            "VALUES (?, ?, ?, ?)",
            (client_id, slot_id, equipment_choice, allergies or ""),
        )
        booking_id = cursor.lastrowid

        # ----------------------------------------------------------
        # 5. Если прокат — инкрементируем booked_rental_sets
        #    Это часть той же транзакции, поэтому атомарно с INSERT.
        # ----------------------------------------------------------
        if equipment_choice == "rental":
            conn.execute(
                "UPDATE Equipment "
                "SET booked_rental_sets = booked_rental_sets + 1 "
                "WHERE slot_id = ?",
                (slot_id,),
            )

        # Фиксируем транзакцию
        conn.execute("COMMIT")

        return {
            "id": booking_id,
            "status": "confirmed",
            "message": "Бронь успешно создана",
        }

    # ----------------------------------------------------------
    # Обработка бизнес-ошибок (ожидаемые сценарии)
    # ----------------------------------------------------------
    except ValueError as e:
        conn.execute("ROLLBACK")
        error_code = str(e)
        messages = {
            "SLOT_FULL": (
                "К сожалению, на этот класс только что записались. "
                "Выберите другое время"
            ),
            "RENT_EXHAUSTED": (
                "К сожалению, все прокатные наборы на этот класс разобраны. "
                "Пожалуйста, приходите со своей экипировкой или выберите другой слот"
            ),
            "SLOT_NOT_FOUND": "Слот не найден",
            "SLOT_CANCELLED": "Слот отменён студией",
        }
        return {
            "error_code": error_code,
            "message": messages.get(error_code, "Ошибка бронирования"),
        }

    # ----------------------------------------------------------
    # Обработка непредвиденных ошибок (БД, сеть и т.п.)
    # ----------------------------------------------------------
    except Exception as e:
        conn.execute("ROLLBACK")
        return {
            "error_code": "INTERNAL_ERROR",
            "message": f"Внутренняя ошибка: {e}",
        }

    finally:
        conn.close()

# ============================================================
# DELETE /bookings/{id}
# ============================================================
def cancel_booking(booking_id):
    """
    Отменяет бронь клиентом.

    Критическая бизнес-логика:
      - Если до начала слота <= 10 минут — отмена невозможна
        (продукты уже закуплены).
      - Иначе — статус брони меняется на 'cancelled_by_client',
        а прокатный набор (если был) возвращается в фонд.

    Source of Truth — серверное время (datetime.now),
    а не клиентское. Это исключает обход через перевод часов.

    Параметры:
      booking_id — ID брони

    Возвращает:
      При успехе:  {"status": "cancelled_by_client", "message": "..."}
      При ошибке:  {"error_code": "<код>", "message": "<текст>"}
    """

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    # Явное управление транзакцией для атомарности
    conn.isolation_level = None

    try:
        conn.execute("BEGIN")

        # ----------------------------------------------------------
        # 1. Получаем бронь и проверяем её существование/статус
        # ----------------------------------------------------------
        booking = conn.execute(
            "SELECT slot_id, equipment_type, status "
            "FROM Bookings WHERE id = ?",
            (booking_id,),
        ).fetchone()

        if not booking:
            raise ValueError("BOOKING_NOT_FOUND")

        if booking["status"] != "confirmed":
            # Бронь уже отменена (клиентом или студией) — повторная отмена запрещена
            raise ValueError("ALREADY_CANCELLED")

        # ----------------------------------------------------------
        # 2. Получаем время начала слота
        # ----------------------------------------------------------
        slot = conn.execute(
            "SELECT start_time FROM Slots WHERE id = ?",
            (booking["slot_id"],),
        ).fetchone()

        if not slot:
            # На всякий случай: слот мог быть удалён (хотя по FK это невозможно)
            raise ValueError("SLOT_NOT_FOUND")

        # Парсим ISO-формат "2026-07-07T18:00:00"
        start_time = datetime.fromisoformat(slot["start_time"])

        # ----------------------------------------------------------
        # 3. КРИТИЧЕСКАЯ ПРОВЕРКА: правило 10 минут
        #    Сравниваем серверное время (Source of Truth) с start_time
        # ----------------------------------------------------------
        now = datetime.now()
        diff_seconds = (start_time - now).total_seconds()
        diff_minutes = diff_seconds / 60

        # Если до начала <= 10 минут — отмена запрещена
        if diff_minutes <= 10:
            raise ValueError("CANCELLATION_TOO_LATE")

        # ----------------------------------------------------------
        # 4. Меняем статус брони (не удаляем физически — сохраняем историю)
        # ----------------------------------------------------------
        conn.execute(
            "UPDATE Bookings SET status = 'cancelled_by_client' WHERE id = ?",
            (booking_id,),
        )

        # ----------------------------------------------------------
        # 5. Если был прокат — возвращаем набор в фонд
        #    book_rental_sets декрементируется на 1
        # ----------------------------------------------------------
        if booking["equipment_type"] == "rental":
            conn.execute(
                "UPDATE Equipment "
                "SET booked_rental_sets = booked_rental_sets - 1 "
                "WHERE slot_id = ?",
                (booking["slot_id"],),
            )

        conn.execute("COMMIT")

        return {
            "status": "cancelled_by_client",
            "message": "Бронь успешно отменена",
        }

    # ----------------------------------------------------------
    # Обработка бизнес-ошибок
    # ----------------------------------------------------------
    except ValueError as e:
        conn.execute("ROLLBACK")
        error_code = str(e)
        messages = {
            "CANCELLATION_TOO_LATE": (
                "Отмена невозможна менее чем за 10 минут до начала, "
                "так как продукты уже закуплены. "
                "Пожалуйста, свяжитесь с нами по телефону"
            ),
            "BOOKING_NOT_FOUND": "Бронь не найдена",
            "ALREADY_CANCELLED": "Бронь уже отменена",
            "SLOT_NOT_FOUND": "Слот не найден",
        }
        return {
            "error_code": error_code,
            "message": messages.get(error_code, "Ошибка отмены брони"),
        }

    # ----------------------------------------------------------
    # Непредвиденные ошибки
    # ----------------------------------------------------------
    except Exception as e:
        conn.execute("ROLLBACK")
        return {
            "error_code": "INTERNAL_ERROR",
            "message": f"Внутренняя ошибка: {e}",
        }

    finally:
        conn.close()
