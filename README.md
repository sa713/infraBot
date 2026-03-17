# Telegram Bot For Service/VPN Monitoring

Бот мониторит ваши `systemd`-сервисы (боты и VPN), присылает алерты в Telegram и даёт кнопки `статус / рестарт / стоп`.

## Что умеет

- Периодически проверяет `systemctl is-active` и `is-enabled` для сервисов из конфигурации.
- Отправляет уведомления при падении сервиса и при восстановлении.
- Раз в сутки присылает сводку по всем сервисам (даже если всё в порядке).
- Поддерживает `oneshot watchdog`-сервисы (например VPN watchdog): для таких сервисов проверка выполняется через `systemctl start`.
- Даёт inline-кнопки для каждого сервиса:
  - `Сводка сейчас` (мгновенный общий статус, не дожидаясь 08:00)
  - `Статус <service>`
  - `Рестарт`
  - `Стоп`
  - Для `WATCHDOG_SERVICES`: `Рестарт` запускает `systemctl start`, `Стоп` не выполняется.
- Отдельно поддерживает VPN-сервис через переменную `VPN_SERVICE`.
- Ограничивает доступ к управлению по `ALLOWED_USER_IDS`.

## Файлы

- `bot_manager.py` — основной код бота.
- `.env.example` — пример конфигурации.
- `requirements.txt` — зависимости.

## Быстрый запуск

1. Создайте виртуальное окружение и установите зависимости:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Подготовьте конфиг:

```bash
cp .env.example .env
```

3. Заполните `.env`:

- `BOT_TOKEN` — токен от @BotFather
- `ALLOWED_USER_IDS` — ваш Telegram user id
- `MONITORED_SERVICES` — сервисы ваших ботов
- `WATCHDOG_SERVICES` — `oneshot` watchdog-сервисы (опционально)
- `SERVICE_BUTTON_LABELS` — короткие подписи кнопок (`service=label` через запятую)
- `VPN_SERVICE` — сервис VPN (например `wg-quick@wg0.service`)
- `DAILY_REPORT_TIME` — время ежедневной сводки, по локальному времени сервера (по умолчанию `08:00`)

4. Запустите:

```bash
set -a
source .env
set +a
python bot_manager.py
```

## Команды в Telegram

- `/start` — открыть панель с кнопками
- `/status` — показать статус всех сервисов
- `/help` — подсказка

## Права на systemctl

Боту нужны права выполнять:

- `systemctl is-active <service>`
- `systemctl is-enabled <service>`
- `systemctl restart <service>`
- `systemctl stop <service>`
- `systemctl start <watchdog_service>` (для `WATCHDOG_SERVICES`)

Варианты:

1. Запускать бота от `root`.
2. Или выдать точечные `sudo`-права пользователю бота.

Пример фрагмента `/etc/sudoers.d/telegram-service-bot` (рекомендуется ограничить список только вашими сервисами):

```sudoers
botuser ALL=(root) NOPASSWD: /bin/systemctl is-active bot-alpha.service
botuser ALL=(root) NOPASSWD: /bin/systemctl is-enabled bot-alpha.service
botuser ALL=(root) NOPASSWD: /bin/systemctl restart bot-alpha.service
botuser ALL=(root) NOPASSWD: /bin/systemctl stop bot-alpha.service
botuser ALL=(root) NOPASSWD: /bin/systemctl is-active wg-quick@wg0.service
botuser ALL=(root) NOPASSWD: /bin/systemctl is-enabled wg-quick@wg0.service
botuser ALL=(root) NOPASSWD: /bin/systemctl restart wg-quick@wg0.service
botuser ALL=(root) NOPASSWD: /bin/systemctl stop wg-quick@wg0.service
botuser ALL=(root) NOPASSWD: /bin/systemctl start adguard-vpn-watchdog.service
botuser ALL=(root) NOPASSWD: /bin/systemctl is-enabled adguard-vpn-watchdog.service
```

Если используете `sudo`, просто установите `USE_SUDO=true` в `.env`.

## Пример systemd-юнита для самого Telegram-бота

Создайте `/etc/systemd/system/telegram-service-monitor.service`:

```ini
[Unit]
Description=Telegram service monitor bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/telegram-service-monitor
EnvironmentFile=/opt/telegram-service-monitor/.env
ExecStart=/opt/telegram-service-monitor/.venv/bin/python /opt/telegram-service-monitor/bot_manager.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Активация:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-service-monitor.service
sudo systemctl status telegram-service-monitor.service
```

## Что настроить под себя

- Список сервисов в `MONITORED_SERVICES`
- Watchdog-юниты в `WATCHDOG_SERVICES` (если используете oneshot watchdog)
- VPN-юнит в `VPN_SERVICE`
- Ваш `ALLOWED_USER_IDS` и `ALERT_CHAT_IDS`
- Период проверки `CHECK_INTERVAL_SEC`
- Время ежедневной сводки `DAILY_REPORT_TIME`
