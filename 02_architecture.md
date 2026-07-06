# Спецификация локальной схемы БД и Mock-API для клиентского приложения «Кулинарная студия»

> **Назначение документа:** Артефакт проектирования для команды разработки клиентского мобильного приложения.  
> **Контекст:** Бэкенд — «чёрный ящик», мы имитируем его ответы через локальную SQLite-базу и Mock-сервер.  
> **Дата актуализации:** 6 июля 2026 г.  
> **Скоуп:** Только MVP-сущности (Clients, Slots, Bookings, Equipment + вспомогательные Chefs). Без оплаты, отзывов, админки.

---

## 1. SQL-скрипт создания схемы SQLite

Схема спроектирована с учётом:
- атомарности операций (защита от овербукинга через транзакции);
- бизнес-правила по лимиту мест (8 или 12);
- отдельного учёта фонда прокатной экипировки на каждый слот;
- жизненного цикла статусов слотов и броней согласно доменной модели.

```sql
-- ==========================================================
-- Таблица клиентов
-- ==========================================================
CREATE TABLE Clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    allergies_info TEXT,                  -- JSON или строка с аллергиями
    loyalty_status TEXT DEFAULT 'standard'
        CHECK(loyalty_status IN ('standard', 'vip'))
);

-- ==========================================================
-- Таблица шефов (минимальная, только для связи со слотами)
-- ==========================================================
CREATE TABLE Chefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rating REAL DEFAULT 0.0
        CHECK(rating >= 0.0 AND rating <= 5.0)
);

-- ==========================================================
-- Таблица слотов (расписание)
-- ==========================================================
CREATE TABLE Slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time DATETIME NOT NULL,
    menu_name TEXT NOT NULL,
    chef_id INTEGER NOT NULL,
    capacity INTEGER NOT NULL DEFAULT 12
        CHECK(capacity IN (8, 12)),       -- Бизнес-правило: 8 или 12 мест
    status TEXT DEFAULT 'active'
        CHECK(status IN ('active', 'cancelled_by_studio')),
    cancellation_reason TEXT,
    FOREIGN KEY (chef_id) REFERENCES Chefs(id)
);

-- ==========================================================
-- Таблица учёта прокатной экипировки (фонд на конкретный слот)
-- ==========================================================
CREATE TABLE Equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL UNIQUE,      -- Уникальный фонд для каждого слота
    total_rental_sets INTEGER NOT NULL DEFAULT 0,   -- Общее количество наборов
    booked_rental_sets INTEGER NOT NULL DEFAULT 0,  -- Уже забронировано
    FOREIGN KEY (slot_id) REFERENCES Slots(id)
);

-- ==========================================================
-- Таблица броней
-- ==========================================================
CREATE TABLE Bookings (
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

-- ==========================================================
-- Индексы для ускорения выборок и проверок
-- ==========================================================
CREATE INDEX idx_slots_start_time ON Slots(start_time);
CREATE INDEX idx_bookings_slot_status ON Bookings(slot_id, status);
CREATE INDEX idx_equipment_slot ON Equipment(slot_id);
```

---

## 2. Контракт Mock-API (REST)

Контракт описан для эмуляции бэкенда. Бэкенд выступает как «чёрный ящик» и единственный источник истины.

### 2.1. `GET /slots` — получение списка слотов

Получение списка слотов. По умолчанию возвращает данные на 7 дней.

| Параметр | Значение |
| --- | --- |
| **Method** | `GET` |
| **URL** | `/api/v1/slots` |
| **Query Parameters** | `start_date` (optional, ISO 8601, default: today)<br>`end_date` (optional, ISO 8601, default: today + 6 days) |

**Response JSON (200 OK):**

```json
{
  "slots": [
    {
      "id": 1,
      "start_time": "2026-07-07T18:00:00Z",
      "menu_name": "Итальянская классика",
      "chef": {
        "id": 1,
        "name": "Марко Росси",
        "rating": 4.8
      },
      "capacity": 12,
      "free_seats_count": 5,
      "is_rent_available": true,
      "status": "active"
    }
  ]
}
```

> **Примечание:** Если слотов нет, возвращается пустой массив `slots: []` — это триггер для Empty State на клиенте.

---

### 2.2. `POST /bookings` — создание брони

Создание брони. Бэкенд атомарно проверяет наличие мест и прокатных наборов.

| Параметр | Значение |
| --- | --- |
| **Method** | `POST` |
| **URL** | `/api/v1/bookings` |

**Request Body:**

```json
{
  "client_id": 101,
  "slot_id": 1,
  "equipment_type": "rental",
  "allergies": "Глютен, орехи"
}
```

**Response JSON (201 Created):**

```json
{
  "id": 501,
  "status": "confirmed",
  "message": "Бронь успешно создана"
}
```

**Ошибки (400 Bad Request):**

- Если мест нет:
  ```json
  {
    "error_code": "SLOT_FULL",
    "message": "К сожалению, на этот класс только что записались. Выберите другое время"
  }
  ```
- Если выбран прокат, но фонды исчерпаны:
  ```json
  {
    "error_code": "RENT_EXHAUSTED",
    "message": "К сожалению, все прокатные наборы на этот класс разобраны. Пожалуйста, приходите со своей экипировкой или выберите другой слот"
  }
  ```

---

### 2.3. `DELETE /bookings/{id}` — отмена брони

Отмена брони клиентом. **Важно:** здесь реализована серверная валидация бизнес-правила 10 минут.

| Параметр | Значение |
| --- | --- |
| **Method** | `DELETE` |
| **URL** | `/api/v1/bookings/{id}` |

**Response JSON (200 OK):**

```json
{
  "status": "cancelled_by_client",
  "message": "Бронь успешно отменена"
}
```

**Ошибки (400 Bad Request) — логика 10 минут:**

- **Условие:** `slot.start_time - current_server_time <= 10 минут`.
- **Response:**
  ```json
  {
    "error_code": "CANCELLATION_TOO_LATE",
    "message": "Отмена невозможна менее чем за 10 минут до начала, так как продукты уже закуплены. Пожалуйста, свяжитесь с нами по телефону"
  }
  ```

---

## 3. JSON-файл с моковыми данными (Seed Data)

Файл для наполнения SQLite-базы перед тестами. Включает 4 слота на разные дни, с разными шефами, лимитами мест (8 и 12) и разным фондом прокатной экипировки.

```json
{
  "chefs": [
    { "id": 1, "name": "Марко Росси", "rating": 4.9 },
    { "id": 2, "name": "Анна Светлова", "rating": 4.5 },
    { "id": 3, "name": "Дмитрий Волков", "rating": 3.8 }
  ],
  "slots": [
    {
      "id": 1,
      "start_time": "2026-07-07T18:00:00",
      "menu_name": "Итальянская классика (Паста и пицца)",
      "chef_id": 1,
      "capacity": 12,
      "status": "active",
      "equipment": {
        "total_rental_sets": 8,
        "booked_rental_sets": 2
      }
    },
    {
      "id": 2,
      "start_time": "2026-07-08T19:00:00",
      "menu_name": "Азиатский фуршет (Вок и суши)",
      "chef_id": 2,
      "capacity": 8,
      "status": "active",
      "equipment": {
        "total_rental_sets": 5,
        "booked_rental_sets": 5
      }
    },
    {
      "id": 3,
      "start_time": "2026-07-10T14:00:00",
      "menu_name": "Французские десерты (Сложный класс)",
      "chef_id": 1,
      "capacity": 8,
      "status": "active",
      "equipment": {
        "total_rental_sets": 0,
        "booked_rental_sets": 0
      }
    },
    {
      "id": 4,
      "start_time": "2026-07-12T18:00:00",
      "menu_name": "Грузинское застолье",
      "chef_id": 3,
      "capacity": 12,
      "status": "cancelled_by_studio",
      "cancellation_reason": "Срыв поставки продуктов",
      "equipment": {
        "total_rental_sets": 10,
        "booked_rental_sets": 0
      }
    }
  ],
  "clients": [
    {
      "id": 101,
      "full_name": "Елена Тестова",
      "phone": "+79990001122",
      "allergies_info": "Нет аллергий",
      "loyalty_status": "standard"
    }
  ]
}
```

---

## 4. Архитектурные заметки для разработки Mock-сервера

1. **Транзакционность.** При обработке `POST /bookings` обязательно оборачивайте проверку `free_seats_count` и инкремент `booked_rental_sets` (если выбрана экипировка) в одну транзакцию SQLite:
   ```sql
   BEGIN TRANSACTION;
   -- проверка и обновление
   COMMIT;
   ```
   Это защитит от race conditions при одновременных запросах.

2. **Время как Source of Truth.** Для проверки правила 10 минут в `DELETE /bookings/{id}` используйте **серверное время** (время выполнения запроса), а не время клиента — это исключает обход блокировки переводом часов на устройстве.

3. **Empty State.** Убедитесь, что слоты со статусом `cancelled_by_studio` либо не возвращаются в общем списке активных, либо возвращаются с явным флагом — это позволит клиентскому UI корректно отрисовать Empty State или карточку отмены.

4. **Исключения из скоупа (явно зафиксировано).** В рамках текущего MVP **не реализуются**:
   - онлайн-оплата (эквайринг);
   - сущность отзывов/оценок шефа (несмотря на упоминание в исходном тексте — вынесено в отдельную итерацию по явному указанию);
   - интерфейсы Шефа и Администратора;
   - управление расписанием и ресурсами.

5. **Гарантия от двойных броней.** Клиентское приложение не проверяет наличие мест локально перед отправкой запроса. Гарантия обеспечивается атомарной проверкой на стороне Mock-сервера (эмуляция бэкенда). Приложение лишь корректно обрабатывает ответ об отказе и показывает пользователю понятное сообщение.