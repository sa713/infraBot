#!/usr/bin/env python3
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


@dataclass(frozen=True)
class Config:
    bot_token: str
    services: list[str]
    allowed_user_ids: set[int]
    alert_chat_ids: list[int]
    check_interval_sec: int
    command_timeout_sec: int
    daily_report_time: time
    use_sudo: bool


def _parse_int_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    values: list[int] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        values.append(int(item))
    return values


def _parse_service_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    deduped: list[str] = []
    seen: set[str] = set()
    for chunk in raw.split(","):
        service = chunk.strip()
        if not service or service in seen:
            continue
        seen.add(service)
        deduped.append(service)
    return deduped


def _parse_hhmm_time(raw: str) -> time:
    value = raw.strip()
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("DAILY_REPORT_TIME must be in HH:MM format")

    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("DAILY_REPORT_TIME has invalid hour/minute values")

    tzinfo = datetime.now().astimezone().tzinfo
    return time(hour=hour, minute=minute, tzinfo=tzinfo)


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    services = _parse_service_list(os.getenv("MONITORED_SERVICES"))

    vpn_service = os.getenv("VPN_SERVICE", "").strip()
    if vpn_service and vpn_service not in services:
        services.append(vpn_service)

    allowed_user_ids = set(_parse_int_list(os.getenv("ALLOWED_USER_IDS")))
    alert_chat_ids = _parse_int_list(os.getenv("ALERT_CHAT_IDS"))

    check_interval_sec = int(os.getenv("CHECK_INTERVAL_SEC", "60"))
    command_timeout_sec = int(os.getenv("COMMAND_TIMEOUT_SEC", "15"))
    daily_report_time = _parse_hhmm_time(os.getenv("DAILY_REPORT_TIME", "08:00"))
    use_sudo = os.getenv("USE_SUDO", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if not bot_token:
        raise ValueError("BOT_TOKEN is required")
    if not services:
        raise ValueError("At least one service is required in MONITORED_SERVICES or VPN_SERVICE")
    if not allowed_user_ids:
        raise ValueError("ALLOWED_USER_IDS is required for secure control")
    if not alert_chat_ids:
        alert_chat_ids = sorted(allowed_user_ids)

    return Config(
        bot_token=bot_token,
        services=services,
        allowed_user_ids=allowed_user_ids,
        alert_chat_ids=alert_chat_ids,
        check_interval_sec=check_interval_sec,
        command_timeout_sec=command_timeout_sec,
        daily_report_time=daily_report_time,
        use_sudo=use_sudo,
    )


async def run_command(*args: str, timeout_sec: int) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return (124, "", f"Command timed out after {timeout_sec}s")

    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def run_systemctl(
    cfg: Config, subcommand: str, service: str
) -> tuple[int, str, str]:
    cmd = ["systemctl", subcommand, service]
    if cfg.use_sudo:
        cmd = ["sudo", *cmd]
    return await run_command(*cmd, timeout_sec=cfg.command_timeout_sec)


async def get_service_state(cfg: Config, service: str) -> dict[str, str]:
    rc_active, out_active, err_active = await run_systemctl(cfg, "is-active", service)
    active = out_active if out_active else (err_active if err_active else "unknown")
    if rc_active != 0 and not out_active:
        active = "unknown"

    rc_enabled, out_enabled, err_enabled = await run_systemctl(
        cfg, "is-enabled", service
    )
    enabled = out_enabled if out_enabled else (err_enabled if err_enabled else "unknown")
    if rc_enabled != 0 and not out_enabled:
        enabled = "unknown"

    return {
        "active": active,
        "enabled": enabled,
    }


def is_problem(active_state: str) -> bool:
    return active_state != "active"


def is_authorized(update: Update, cfg: Config) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return user.id in cfg.allowed_user_ids


def build_keyboard(services: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("Общий статус", callback_data="all")]
    ]

    for idx, service in enumerate(services):
        rows.append(
            [
                InlineKeyboardButton(f"Статус {service}", callback_data=f"s|{idx}|status"),
                InlineKeyboardButton("Рестарт", callback_data=f"s|{idx}|restart"),
                InlineKeyboardButton("Стоп", callback_data=f"s|{idx}|stop"),
            ]
        )

    return InlineKeyboardMarkup(rows)


async def broadcast_alert(application: Application, cfg: Config, text: str) -> None:
    for chat_id in cfg.alert_chat_ids:
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logging.exception("Failed to send alert to chat_id=%s", chat_id)


async def format_all_status(cfg: Config, header: str = "Текущий статус сервисов:") -> str:
    lines = [header]
    for service in cfg.services:
        state = await get_service_state(cfg, service)
        mark = "OK" if not is_problem(state["active"]) else "PROBLEM"
        lines.append(
            f"- {service}: {state['active']} (enabled: {state['enabled']}) [{mark}]"
        )
    return "\n".join(lines)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    if not is_authorized(update, cfg):
        return

    await update.effective_message.reply_text(
        "Панель управления сервисами.",
        reply_markup=build_keyboard(cfg.services),
    )


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    if not is_authorized(update, cfg):
        return

    await update.effective_message.reply_text(await format_all_status(cfg))


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    if not is_authorized(update, cfg):
        return

    await update.effective_message.reply_text(
        "Команды:\n"
        "/start - показать кнопки\n"
        "/status - показать статус всех сервисов\n"
        "/help - помощь"
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]

    query = update.callback_query
    await query.answer()

    if not is_authorized(update, cfg):
        await query.edit_message_text("Недостаточно прав для управления.")
        return

    data = query.data or ""
    if data == "all":
        await query.edit_message_text(
            await format_all_status(cfg),
            reply_markup=build_keyboard(cfg.services),
        )
        return

    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "s":
        await query.edit_message_text("Неизвестное действие.")
        return

    idx_raw, action = parts[1], parts[2]
    if not idx_raw.isdigit():
        await query.edit_message_text("Некорректный индекс сервиса.")
        return

    idx = int(idx_raw)
    if idx < 0 or idx >= len(cfg.services):
        await query.edit_message_text("Сервис не найден.")
        return

    service = cfg.services[idx]

    if action == "status":
        state = await get_service_state(cfg, service)
        await query.edit_message_text(
            (
                f"{service}\n"
                f"active: {state['active']}\n"
                f"enabled: {state['enabled']}"
            ),
            reply_markup=build_keyboard(cfg.services),
        )
        return

    if action not in {"restart", "stop"}:
        await query.edit_message_text("Неизвестная команда.")
        return

    rc, _, err = await run_systemctl(cfg, action, service)

    state = await get_service_state(cfg, service)
    result = "успешно" if rc == 0 else f"ошибка ({err or 'без stderr'})"

    await query.edit_message_text(
        (
            f"Команда {action} для {service}: {result}\n"
            f"active: {state['active']}\n"
            f"enabled: {state['enabled']}"
        ),
        reply_markup=build_keyboard(cfg.services),
    )


async def periodic_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    cfg: Config = application.bot_data["cfg"]
    last_states: dict[str, str] = application.bot_data.setdefault("last_states", {})

    for service in cfg.services:
        state = await get_service_state(cfg, service)
        current = state["active"]
        previous = last_states.get(service)
        last_states[service] = current

        # First observation: alert only if service is already unhealthy.
        if previous is None:
            if is_problem(current):
                await broadcast_alert(
                    application,
                    cfg,
                    f"ALERT: {service} has unhealthy state: {current}",
                )
            continue

        if previous == current:
            continue

        if is_problem(current):
            await broadcast_alert(
                application,
                cfg,
                f"ALERT: {service} changed {previous} -> {current}",
            )
        else:
            await broadcast_alert(
                application,
                cfg,
                f"RECOVERY: {service} changed {previous} -> {current}",
            )


async def daily_status_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    cfg: Config = application.bot_data["cfg"]
    report = await format_all_status(cfg, header="Ежедневный отчет по сервисам:")
    await broadcast_alert(application, cfg, report)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )

    cfg = load_config()

    app = Application.builder().token(cfg.bot_token).build()
    app.bot_data["cfg"] = cfg

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue is not available. Install dependency with: python-telegram-bot[job-queue]"
        )

    app.job_queue.run_repeating(periodic_check, interval=cfg.check_interval_sec, first=5)
    app.job_queue.run_daily(daily_status_report, time=cfg.daily_report_time)

    logging.info("Starting bot. Monitoring services: %s", ", ".join(cfg.services))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
