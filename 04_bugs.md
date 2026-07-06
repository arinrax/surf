```markdown
# Bug Report: Отмена брони без проверки правила 10 минут

**ID:** BUG-0042  
**Приоритет:** Critical  
**Компонент:** `api.py` → `cancel_booking()`  
**Дата обнаружения:** 6 июля 2026 г.  
**Статус:** Исправлено

---

## Описание проблемы

### Симптом
Клиент может отменить бронь за 5 минут до начала слота. Это приводит к тому, что студия теряет закупленные под конкретный мастер-класс продукты, но при этом освобождает место, которое уже невозможно продать.

**Воспроизведение:**
1. Клиент бронирует слот, который начинается через 5 минут.
2. Клиент нажимает «Отменить бронь».
3. Бронь отменяется успешно, хотя по бизнес-правилам это должно быть запрещено.

### Требование
**[US-03]** Отмена брони менее чем за 10 минут до начала слота запрещена.

> *«Отмена невозможна менее чем за 10 минут до начала, так как продукты уже закуплены. Пожалуйста, свяжитесь с нами по телефону».*

**Обоснование:** За 10 минут до начала мастер-класса студия уже закупила и начала готовить продукты под конкретное количество участников. Отмена в этот момент делает продукты неликвидными и создаёт убытки.

**Source of Truth:** Серверное время (`datetime.now()`), а не клиентское — это исключает обход правила через перевод часов на устройстве.

---

## "Плохой" код (с багом)

Функция просто меняет статус брони, не проверяя время до начала слота.

```python
def cancel_booking(booking_id):
    """
    БАГ: Нет проверки времени до начала слота.
    Отменяет любую бронь независимо от того, когда она начинается.
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None

    try:
        conn.execute("BEGIN")

        # Получаем бронь
        booking = conn.execute(
            "SELECT slot_id, equipment_type, status FROM Bookings WHERE id = ?",
            (booking_id,),
        ).fetchone()

        if not booking:
            raise ValueError("BOOKING_NOT_FOUND")
        if booking["status"] != "confirmed":
            raise ValueError("ALREADY_CANCELLED")

        # КРИТИЧЕСКИЙ ПРОПУСК: нет проверки start_time!
        # Просто меняем статус, не глядя на время начала слота.
        conn.execute(
            "UPDATE Bookings SET status = 'cancelled_by_client' WHERE id = ?",
            (booking_id,),
        )

        # Возвращаем прокат в фонд
        if booking["equipment_type"] == "rental":
            conn.execute(
                "UPDATE Equipment SET booked_rental_sets = booked_rental_sets - 1 "
                "WHERE slot_id = ?",
                (booking["slot_id"],),
            )

        conn.execute("COMMIT")
        return {"status": "cancelled_by_client", "message": "Бронь успешно отменена"}

    except ValueError as e:
        conn.execute("ROLLBACK")
        return {"error_code": str(e), "message": "Ошибка отмены брони"}
    finally:
        conn.close()
```

**Последствия бага:**
- Студия несёт убытки из-за закупленных, но неиспользованных продуктов.
- Нарушается SLA с поставщиками (свежие продукты нельзя хранить долго).
- Клиент получает ложное ощущение, что отмена всегда возможна.

---

## "Исправленный" код

Добавлена проверка времени начала слота относительно серверного времени. Если разница ≤ 10 минут — отклоняем отмену с ошибкой `CANCELLATION_TOO_LATE`.

```python
import sqlite3
from datetime import datetime

from db import DB_NAME


def cancel_booking(booking_id):
    """
    ИСПРАВЛЕНО: Отменяет бронь только если до начала слота > 10 минут.
    Source of Truth — серверное время (datetime.now).
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None

    try:
        conn.execute("BEGIN")

        # 1. Получаем бронь
        booking = conn.execute(
            "SELECT slot_id, equipment_type, status FROM Bookings WHERE id = ?",
            (booking_id,),
        ).fetchone()

        if not booking:
            raise ValueError("BOOKING_NOT_FOUND")
        if booking["status"] != "confirmed":
            raise ValueError("ALREADY_CANCELLED")

        # 2. Получаем время начала слота
        slot = conn.execute(
            "SELECT start_time FROM Slots WHERE id = ?",
            (booking["slot_id"],),
        ).fetchone()

        if not slot:
            raise ValueError("SLOT_NOT_FOUND")

        start_time = datetime.fromisoformat(slot["start_time"])

        # 3. КРИТИЧЕСКАЯ ПРОВЕРКА: правило 10 минут
        now = datetime.now()
        diff_minutes = (start_time - now).total_seconds() / 60

        if diff_minutes <= 10:
            raise ValueError("CANCELLATION_TOO_LATE")

        # 4. Меняем статус брони (мягкое удаление)
        conn.execute(
            "UPDATE Bookings SET status = 'cancelled_by_client' WHERE id = ?",
            (booking_id,),
        )

        # 5. Возвращаем прокат в фонд
        if booking["equipment_type"] == "rental":
            conn.execute(
                "UPDATE Equipment SET booked_rental_sets = booked_rental_sets - 1 "
                "WHERE slot_id = ?",
                (booking["slot_id"],),
            )

        conn.execute("COMMIT")
        return {"status": "cancelled_by_client", "message": "Бронь успешно отменена"}

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
    finally:
        conn.close()
```

---

## Проверка исправления

Тестовый сценарий, подтверждающий, что баг исправлен:

```python
from datetime import datetime, timedelta
import sqlite3
from db import DB_NAME, init_db
from api import create_booking, cancel_booking

init_db()

# Создаём «горячий» слот — стартует через 5 минут
hot_time = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
conn = sqlite3.connect(DB_NAME)
conn.execute(
    "INSERT INTO Slots (start_time, menu_name, chef_id, capacity, status) "
    "VALUES (?, ?, ?, ?, ?)",
    (hot_time, "Экспресс-класс", 1, 12, "active"),
)
hot_slot_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.execute(
    "INSERT INTO Equipment (slot_id, total_rental_sets, booked_rental_sets) "
    "VALUES (?, 5, 0)",
    (hot_slot_id,),
)
conn.commit()
conn.close()

# Бронируем горячий слот
booking = create_booking(
    slot_id=hot_slot_id,
    client_id=101,
    equipment_choice="own",
    allergies="",
)

# Пытаемся отменить — должно сработать правило 10 минут
result = cancel_booking(booking["id"])
print(result)
# Ожидаемый результат:
# {'error_code': 'CANCELLATION_TOO_LATE',
#  'message': 'Отмена невозможна менее чем за 10 минут до начала...'}
```

---

## Промпт, который привёл к исправлению

> **От:** QA-инженер  
> **Кому:** Python-разработчик  
> **Тема:** Баг в `cancel_booking()` — отмена работает даже за 5 минут до начала
>
> Привет!
>
> Нашёл баг в mock-бэкенде. Функция `cancel_booking()` отменяет любую бронь без проверки времени до начала слота.
>
> **Шаги воспроизведения:**
> 1. Запускаю `test_client.py`.
> 2. Создаю слот, который стартует через 5 минут.
> 3. Бронирую его, потом сразу пытаюсь отменить.
> 4. Бронь отменяется успешно — хотя по [US-03] это должно быть запрещено.
>
> **Ожидаемое поведение:**  
> Если до начала слота ≤ 10 минут, функция должна возвращать ошибку `CANCELLATION_TOO_LATE` с сообщением *«Продукты уже закуплены»*.
>
> **Фактическое поведение:**  
> Бронь отменяется в любом случае, студия теряет продукты.
>
> **Прошу:**
> 1. Добавить проверку `start_time - now <= 10 минут`.
> 2. Использовать серверное время (`datetime.now()`), а не клиентское — чтобы нельзя было обойти переводом часов.
> 3. При срабатывании правила возвращать `error_code: "CANCELLATION_TOO_LATE"`.
> 4. Проверить, что транзакция откатывается (`ROLLBACK`) при срабатывании правила — чтобы не трогать `Equipment` и `Bookings`.
>
> Приложи, пожалуйста, diff «плохой» vs «исправленный» код в баг-репорте для истории.
>
> Спасибо!

---

# Bug Report: Информация об аллергиях не сохраняется в БД

**ID:** BUG-0043  
**Приоритет:** Critical (медицинские риски)  
**Компонент:** `api.py` → `create_booking()`  
**Дата обнаружения:** 6 июля 2026 г.  
**Статус:** Исправлено

---

## Описание проблемы

### Симптом
Информация об аллергиях клиента, указанная при бронировании, теряется. Шеф не видит её ни в интерфейсе, ни в распечатке списка участников мастер-класса. Это приводит к тому, что клиент с аллергией (например, на орехи или глютен) может получить блюдо, опасное для его здоровья.

**Воспроизведение:**
1. Клиент бронирует слот и указывает в поле `allergies`: `"Глютен, орехи"`.
2. Бронь успешно создаётся, клиент получает подтверждение.
3. Шеф открывает список участников — поле `allergies` в записи `Bookings` пустое (`NULL` или `""`).
4. Шеф готовит стандартное меню без учёта ограничений клиента.
5. Клиент получает блюдо с аллергеном → инцидент со здоровьем.

**Почему это критично:**  
В отличие от UI-багов, этот баг имеет **медицинские последствия** — анафилактический шок, отёк Квинке, госпитализация. Студия несёт юридическую ответственность.

### Требование
Из брифа проекта (пункт «Сбор данных о клиенте»):

> *«Хорошо бы заранее спрашивать про аллергии — это важно для безопасности и качества сервиса. Клиент должен иметь возможность указать ограничения по питанию при бронировании, а шеф — видеть их ДО начала мастер-класса».*

**Дополнительные требования из архитектурной документации:**
- Поле `allergies` в таблице `Bookings` существует (`TEXT`, nullable).
- Поле должно заполняться при создании брони.
- Данные должны быть доступны для чтения шефом/администратором (в будущих итерациях).

---

## "Плохой" код (с багом)

Функция принимает параметр `allergies`, но **не передаёт его в SQL-запрос INSERT**. Поле в БД остаётся `NULL` (дефолтное значение).

```python
def create_booking(slot_id, client_id, equipment_choice, allergies):
    """
    БАГ: Параметр allergies принимается, но не сохраняется в БД.
    INSERT-запрос не содержит поле allergies.
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None

    try:
        conn.execute("BEGIN")

        # Проверка слота
        slot = conn.execute(
            "SELECT capacity, status FROM Slots WHERE id = ?",
            (slot_id,),
        ).fetchone()
        if not slot or slot["status"] != "active":
            raise ValueError("SLOT_NOT_FOUND")

        # Проверка мест
        booked = conn.execute(
            "SELECT COUNT(*) FROM Bookings WHERE slot_id = ? AND status = 'confirmed'",
            (slot_id,),
        ).fetchone()[0]
        if booked >= slot["capacity"]:
            raise ValueError("SLOT_FULL")

        # Проверка проката
        if equipment_choice == "rental":
            equip = conn.execute(
                "SELECT total_rental_sets, booked_rental_sets FROM Equipment WHERE slot_id = ?",
                (slot_id,),
            ).fetchone()
            if not equip or equip["booked_rental_sets"] >= equip["total_rental_sets"]:
                raise ValueError("RENT_EXHAUSTED")

        # КРИТИЧЕСКИЙ БАГ: в INSERT отсутствует поле allergies!
        # Параметр allergies просто игнорируется.
        cursor = conn.execute(
            "INSERT INTO Bookings (client_id, slot_id, equipment_type) "
            "VALUES (?, ?, ?)",
            (client_id, slot_id, equipment_choice),
        )
        booking_id = cursor.lastrowid

        if equipment_choice == "rental":
            conn.execute(
                "UPDATE Equipment SET booked_rental_sets = booked_rental_sets + 1 "
                "WHERE slot_id = ?",
                (slot_id,),
            )

        conn.execute("COMMIT")
        return {
            "id": booking_id,
            "status": "confirmed",
            "message": "Бронь успешно создана",
        }

    except ValueError as e:
        conn.execute("ROLLBACK")
        return {"error_code": str(e), "message": "Ошибка бронирования"}
    finally:
        conn.close()
```

**Последствия бага:**
- Шеф не видит аллергии → готовит опасное меню.
- Клиент получает блюдо с аллергеном → медицинский инцидент.
- Студия несёт юридическую и репутационную ответственность.
- Данные, которые клиент потратил время на ввод, просто теряются.

---

## "Исправленный" код

Добавлено поле `allergies` в SQL-запрос `INSERT`. Значение нормализуется через `or ""` — если клиент не указал аллергии, в БД сохраняется пустая строка (а не `NULL`), что упрощает последующие выборки.

```python
import sqlite3
from db import DB_NAME


def create_booking(slot_id, client_id, equipment_choice, allergies):
    """
    ИСПРАВЛЕНО: Поле allergies корректно сохраняется в БД.
    Если allergies не передан или пустой — сохраняется пустая строка.
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None

    try:
        conn.execute("BEGIN")

        # Проверка слота
        slot = conn.execute(
            "SELECT capacity, status FROM Slots WHERE id = ?",
            (slot_id,),
        ).fetchone()
        if not slot or slot["status"] != "active":
            raise ValueError("SLOT_NOT_FOUND")

        # Проверка мест
        booked = conn.execute(
            "SELECT COUNT(*) FROM Bookings WHERE slot_id = ? AND status = 'confirmed'",
            (slot_id,),
        ).fetchone()[0]
        if booked >= slot["capacity"]:
            raise ValueError("SLOT_FULL")

        # Проверка проката
        if equipment_choice == "rental":
            equip = conn.execute(
                "SELECT total_rental_sets, booked_rental_sets FROM Equipment WHERE slot_id = ?",
                (slot_id,),
            ).fetchone()
            if not equip or equip["booked_rental_sets"] >= equip["total_rental_sets"]:
                raise ValueError("RENT_EXHAUSTED")

        # ИСПРАВЛЕНИЕ: добавлено поле allergies в INSERT
        # Нормализация: None → "" (защита от NULL в БД)
        cursor = conn.execute(
            "INSERT INTO Bookings (client_id, slot_id, equipment_type, allergies) "
            "VALUES (?, ?, ?, ?)",
            (client_id, slot_id, equipment_choice, allergies or ""),
        )
        booking_id = cursor.lastrowid

        if equipment_choice == "rental":
            conn.execute(
                "UPDATE Equipment SET booked_rental_sets = booked_rental_sets + 1 "
                "WHERE slot_id = ?",
                (slot_id,),
            )

        conn.execute("COMMIT")
        return {
            "id": booking_id,
            "status": "confirmed",
            "message": "Бронь успешно создана",
        }

    except ValueError as e:
        conn.execute("ROLLBACK")
        error_code = str(e)
        messages = {
            "SLOT_FULL": "К сожалению, на этот класс только что записались. Выберите другое время",
            "RENT_EXHAUSTED": "К сожалению, все прокатные наборы разобраны",
            "SLOT_NOT_FOUND": "Слот не найден",
        }
        return {
            "error_code": error_code,
            "message": messages.get(error_code, "Ошибка бронирования"),
        }
    finally:
        conn.close()
```

---

## Проверка исправления

Тестовый сценарий, подтверждающий, что аллергии сохраняются:

```python
import sqlite3
from db import DB_NAME, init_db
from api import create_booking

init_db()

# Бронируем слот с указанием аллергий
booking = create_booking(
    slot_id=1,
    client_id=101,
    equipment_choice="own",
    allergies="Глютен, орехи",
)

# Проверяем, что аллергии сохранились в БД
conn = sqlite3.connect(DB_NAME)
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT allergies FROM Bookings WHERE id = ?",
    (booking["id"],),
).fetchone()
conn.close()

print(f"Аллергии в БД: '{row['allergies']}'")
# Ожидаемый результат: 'Глютен, орехи'

assert row["allergies"] == "Глютен, орехи", "Аллергии не сохранились!"
print("✓ Аллергии корректно сохранены в БД")

---

## Промпт, который привёл к исправлению

> **От:** QA-инженер  
> **Кому:** Python-разработчик  
> **Тема:** Критический баг — аллергии не сохраняются в БД
>
> Привет!
>
> Нашёл критический баг в `create_booking()`. Параметр `allergies` принимается функцией, но **не сохраняется в таблицу `Bookings`**.
>
> **Шаги воспроизведения:**
> 1. Запускаю `test_client.py` или вызываю `create_booking()` напрямую.
> 2. Передаю `allergies="Глютен, орехи"`.
> 3. Бронь создаётся успешно.
> 4. Делаю `SELECT allergies FROM Bookings WHERE id = <booking_id>` — поле пустое (`NULL`).
>
> **Ожидаемое поведение:**  
> В поле `allergies` таблицы `Bookings` должно сохраниться значение `"Глютен, орехи"`.
>
> **Фактическое поведение:**  
> Поле остаётся `NULL`, информация теряется.
>
> **Критичность:** Critical  
> Это медицинский риск. Шеф не видит аллергии → может приготовить опасное блюдо.
>
> **Прошу:**
> 1. Добавить поле `allergies` в SQL-запрос `INSERT`.
> 2. Нормализовать значение: если `None` или пустая строка — сохранять `""` (не `NULL`), чтобы упростить последующие выборки.
> 3. Добавить тест в `test_client.py`, который проверяет сохранение аллергий.
>
> Приложи diff «плохой» vs «исправленный» код в баг-репорте.
>
> Спасибо!

---

# Bug Report: Время слотов не конвертируется в локальный часовой пояс

**ID:** BUG-0044  
**Приоритет:** High (серьёзный UX-баг)  
**Компонент:** `api.py` → `get_slots()`  
**Дата обнаружения:** 6 июля 2026 г.  
**Статус:** Исправлено

---

## Описание проблемы

### Симптом
Клиент видит время начала слота `"2026-07-07T18:00:00"` и думает, что это его локальное время (например, московское). На самом деле бэкенд хранит и отдаёт время в UTC. Клиент из Москвы (UTC+3) приходит на мастер-класс в 18:00 по местному времени — то есть на 3 часа раньше начала (которое в Москве будет 21:00).

**Воспроизведение:**
1. Клиент из Москвы (UTC+3) открывает список слотов.
2. Видит слот с `start_time: "2026-07-07T18:00:00"`.
3. Думает, что мастер-класс начинается в 18:00 по московскому времени.
4. Приходит в студию в 18:00 — а класс ещё не начался (стартует в 21:00 по Москве).
5. Или наоборот: клиент из Калининграда (UTC+2) видит то же время и приходит на час позже, чем нужно.

**Почему это критично:**  
Клиенты теряют время, нервничают, оставляют негативные отзывы. Студия несёт репутационные потери. В поддержке растёт количество обращений «я пришёл не вовремя».

### Требование
Из архитектурных заметок (пункт «Время как Source of Truth»):

> *«Для проверки правила 10 минут в `DELETE /bookings/{id}` используйте серверное время (время выполнения запроса), а не время клиента».*

**Дополнительное требование (неявное, но критичное):**  
Время, отдаваемое клиенту, должно быть либо:
- В UTC с явным указанием timezone (суффикс `Z` или `+00:00`), чтобы клиентское приложение могло конвертировать его локально.
- Либо уже сконвертированным в локальное время пользователя (если бэкенд знает часовой пояс клиента).

**В рамках mock-бэкенда** реализуем второй вариант: конвертируем время из UTC в локальное время системы, на которой запущен Python. Это эмулирует поведение, которое ожидает клиентское приложение.

---

## "Плохой" код (с багом)

Функция возвращает время начала слота как есть — строку из БД без timezone info. Клиент не понимает, в каком часовом поясе это время.

```python
import sqlite3
from datetime import datetime, timedelta

from db import DB_NAME


def get_slots(start_date=None, end_date=None):
    """
    БАГ: Время start_time возвращается как есть (строка из БД).
    Нет информации о часовом поясе — клиент не может корректно отобразить время.
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

        # КРИТИЧЕСКИЙ БАГ: start_time возвращается как есть (строка из БД)
        # Нет timezone info — клиент не знает, UTC это или локальное время
        slots.append({
            "id": r["id"],
            "start_time": r["start_time"],  # ← "2026-07-07T18:00:00" (без timezone)
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
```

**Последствия бага:**
- Клиенты путаются во времени → приходят не вовремя.
- Растёт нагрузка на поддержку.
- Негативные отзывы и потеря репутации.
- Клиенты могут пропустить мастер-класс или прийти слишком рано.

---

## "Исправленный" код

Добавлена конвертация времени из UTC (в котором оно хранится в БД) в локальное время системы. Используется `datetime.fromisoformat()` для парсинга, `.replace(tzinfo=timezone.utc)` для явного указания UTC, и `.astimezone()` для конвертации в локальное время.

```python
import sqlite3
from datetime import datetime, timedelta, timezone

from db import DB_NAME


def get_slots(start_date=None, end_date=None):
    """
    ИСПРАВЛЕНО: Время start_time конвертируется из UTC в локальное время.
    Клиент получает время в своём часовом поясе.
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

        #ИСПРАВЛЕНИЕ: конвертация времени из UTC в локальное
        # 1. Парсим строку из БД в datetime (без timezone info)
        start_time_naive = datetime.fromisoformat(r["start_time"])
        
        # 2. Явно указываем, что это UTC (добавляем tzinfo)
        start_time_utc = start_time_naive.replace(tzinfo=timezone.utc)
        
        # 3. Конвертируем в локальное время системы
        start_time_local = start_time_utc.astimezone()
        
        # 4. Форматируем обратно в ISO-строку (теперь с timezone info)
        start_time_str = start_time_local.isoformat()

        slots.append({
            "id": r["id"],
            "start_time": start_time_str,  # ← "2026-07-07T21:00:00+03:00" (локальное время)
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
```

---

##Проверка исправления

Тестовый сценарий, подтверждающий, что время корректно конвертируется:

```python
from db import init_db
from api import get_slots
from datetime import datetime, timezone

init_db()

# Получаем список слотов
response = get_slots()

# Проверяем первый слот (должен быть "Итальянская классика" 07.07 18:00 UTC)
slot = response["slots"][0]
print(f"Время слота: {slot['start_time']}")

# Парсим время и проверяем, что есть timezone info
start_time = datetime.fromisoformat(slot["start_time"])
print(f"Timezone info: {start_time.tzinfo}")

# Ожидаемый результат (для Москвы, UTC+3):
# Время слота: 2026-07-07T21:00:00+03:00
# Timezone info: UTC+03:00

assert start_time.tzinfo is not None, "Timezone info отсутствует!"
print("✓ Время корректно сконвертировано в локальное")
```

---

## Промпт, который привёл к исправлению

> **От:** QA-инженер  
> **Кому:** Python-разработчик  
> **Тема:** Баг в `get_slots()` — время не конвертируется в локальный часовой пояс
>
> Привет!
>
> Нашёл серьёзный UX-баг в `get_slots()`. Время начала слота возвращается как строка из БД без timezone info. Клиент не понимает, в каком часовом поясе это время.
>
> **Шаги воспроизведения:**
> 1. Запускаю `test_client.py` на машине с часовым поясом UTC+3 (Москва).
> 2. Вызываю `get_slots()`.
> 3. Получаю слот с `start_time: "2026-07-07T18:00:00"`.
> 4. Нет суффикса `Z` или `+00:00` — непонятно, UTC это или локальное время.
> 5. Клиент думает, что это 18:00 по Москве, а на самом деле это 18:00 UTC (21:00 по Москве).
>
> **Ожидаемое поведение:**  
> Время должно быть либо в UTC с явным указанием timezone (`"2026-07-07T18:00:00Z"`), либо сконвертировано в локальное время клиента (`"2026-07-07T21:00:00+03:00"`).
>
> **Фактическое поведение:**  
> Время возвращается как `"2026-07-07T18:00:00"` — без timezone info. Клиент путается.
>
> **Прошу:**
> 1. В `get_slots()` парсить `start_time` из БД через `datetime.fromisoformat()`.
> 2. Явно указывать, что это UTC: `.replace(tzinfo=timezone.utc)`.
> 3. Конвертировать в локальное время: `.astimezone()`.
> 4. Форматировать обратно в ISO-строку: `.isoformat()`.
> 5. Добавить тест, который проверяет наличие timezone info в ответе.
>
> Приложи diff «плохой» vs «исправленный» код в баг-репорте.
>
> Спасибо!

```