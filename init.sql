-- Экспресс-диагностика ноутбуков при скупке (эпизод 53, пример, не запускался на живом PostgreSQL).
-- Таблицы: devices (журнал приёмок), stop_list (серийники, которые нельзя брать), price_rules (базовые цены по маске модели).
-- Система только выявляет следы корпоративной привязки (MDM, Autopilot) и помогает отказаться от сделки: блокировки она не снимает и не обходит.

CREATE TABLE IF NOT EXISTS stop_list (
    serial    text PRIMARY KEY,                         -- серийник в верхнем регистре, без пробелов
    reason    text NOT NULL,                            -- почему в списке: украден, корпоративный возврат, чужой договор
    added_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_rules (
    id             bigserial PRIMARY KEY,
    model_pattern  text    NOT NULL,                    -- маска для ILIKE, например 'EXAMPLE-MODEL%'
    base_price     integer NOT NULL CHECK (base_price > 0)   -- базовая цена в рублях до уценки; ваши цифры, не рыночные
);

CREATE TABLE IF NOT EXISTS devices (
    id                 bigserial PRIMARY KEY,
    created_at         timestamptz NOT NULL DEFAULT now(),
    board_serial       text NOT NULL,
    chassis_serial     text NOT NULL,                   -- серийник системы, прошитый в плату (SMBIOS)
    sticker_serial     text NOT NULL,                   -- серийник с наклейки на корпусе, вводит приёмщик
    serial_match       boolean NOT NULL,
    model              text,
    bios_vendor        text,
    bios_version       text,
    battery_wear_pct   numeric(5,1),
    battery_cycles     integer,
    ssd_health_pct     numeric(5,1),
    ssd_kind           text,
    flags              jsonb NOT NULL DEFAULT '{}'::jsonb,   -- что проверено офлайн и что найдено
    verdict            text NOT NULL CHECK (verdict IN ('allow', 'review', 'forbidden')),
    reasons            jsonb NOT NULL DEFAULT '[]'::jsonb,
    base_price         integer,
    price_limit        integer,                         -- лимит цены, только при verdict = allow
    extra_discount_pct numeric(4,1) NOT NULL DEFAULT 0 CHECK (extra_discount_pct BETWEEN 0 AND 15),
    ollama_note        text
);
CREATE INDEX IF NOT EXISTS devices_board_serial ON devices (board_serial);
CREATE INDEX IF NOT EXISTS devices_created ON devices (created_at DESC);

-- Примеры строк: замените на свои цены и свой стоп-лист.
INSERT INTO price_rules (model_pattern, base_price) VALUES ('EXAMPLE-MODEL%', 30000) ON CONFLICT DO NOTHING;
INSERT INTO stop_list (serial, reason) VALUES ('EXAMPLE-STOPPED-0001', 'пример: серийник из стоп-листа') ON CONFLICT DO NOTHING;
