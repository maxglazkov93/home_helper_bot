import logging
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from db import Database, calculate_due_date, parse_date, parse_usage_days

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

(
    ADD_ITEM_NAME,
    ADD_ITEM_DATE,
    ADD_ITEM_USAGE,
    UPDATE_ITEM_SELECT,
    UPDATE_ITEM_DATE,
    DELETE_ITEM_SELECT,
    DELETE_ITEM_CONFIRM,
) = range(7)

BTN_ADD = "Добавить предмет и дату"
BTN_SHOW = "Показать предмет и дату"
BTN_UPDATE = "Обновить дату замены"
BTN_DELETE = "Удалить предмет"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_ADD)],
        [KeyboardButton(BTN_SHOW)],
        [KeyboardButton(BTN_UPDATE)],
        [KeyboardButton(BTN_DELETE)],
    ],
    resize_keyboard=True,
)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def get_db() -> Database:
    db = Database()
    db.init_db()
    return db


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_type = update.effective_chat.type if update.effective_chat else "unknown"
    text = (
        "Бот готов к работе.\n\n"
        "Кнопки:\n"
        "- Добавить предмет и дату\n"
        "- Показать предмет и дату\n"
        "- Обновить дату замены\n"
        "- Удалить предмет\n\n"
        "Формат даты: ДД.ММ.ГГГГ (например, 02.05.2026)\n"
        f"Текущий чат: {chat_type}"
    )
    await update.effective_message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def open_add_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Введите название предмета:")
    return ADD_ITEM_NAME


async def add_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item_name = update.effective_message.text.strip()
    if not item_name:
        await update.effective_message.reply_text("Название не может быть пустым. Введите снова:")
        return ADD_ITEM_NAME

    context.user_data["item_name"] = item_name
    await update.effective_message.reply_text(
        "Введите дату замены в формате ДД.ММ.ГГГГ (например, 02.05.2026):"
    )
    return ADD_ITEM_DATE


async def add_item_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    date_raw = update.effective_message.text.strip()

    try:
        replaced_at = parse_date(date_raw)
    except ValueError:
        await update.effective_message.reply_text(
            "Некорректная дата. Используйте формат ДД.ММ.ГГГГ (например, 02.05.2026):"
        )
        return ADD_ITEM_DATE

    context.user_data["replaced_at"] = replaced_at
    await update.effective_message.reply_text(
        "Введите срок использования (например: 14 дней, 2 недели, 1 месяц):"
    )
    return ADD_ITEM_USAGE


async def add_item_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.application.bot_data["db"]
    usage_raw = update.effective_message.text.strip()
    item_name = context.user_data.get("item_name")
    replaced_at = context.user_data.get("replaced_at")
    chat_id = update.effective_chat.id

    try:
        usage_days = parse_usage_days(usage_raw)
    except ValueError:
        await update.effective_message.reply_text(
            "Некорректный срок. Пример: 14 дней, 2 недели, 1 месяц."
        )
        return ADD_ITEM_USAGE

    db.upsert_item(
        chat_id=chat_id,
        item_name=item_name,
        replaced_at=replaced_at,
        usage_days=usage_days,
    )
    due_date = calculate_due_date(replaced_at, usage_days)
    await update.effective_message.reply_text(
        (
            f"Сохранено:\n"
            f"- Предмет: {item_name}\n"
            f"- Дата замены: {replaced_at.strftime('%d.%m.%Y')}\n"
            f"- Срок использования: {usage_days} дн.\n"
            f"- Дата окончания: {due_date.strftime('%d.%m.%Y')}"
        ),
        reply_markup=MAIN_KEYBOARD,
    )
    context.user_data.pop("item_name", None)
    context.user_data.pop("replaced_at", None)
    return ConversationHandler.END


async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("item_name", None)
    context.user_data.pop("replaced_at", None)
    context.user_data.pop("update_item_name", None)
    context.user_data.pop("delete_item_name", None)
    await update.effective_message.reply_text("Добавление отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


async def show_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    items = db.get_items(chat_id)

    if not items:
        await update.effective_message.reply_text(
            "Для этого чата пока нет сохраненных предметов.", reply_markup=MAIN_KEYBOARD
        )
        return

    buttons = [
        [InlineKeyboardButton(text=item, callback_data=f"item::{item}")]
        for item in items
    ]
    await update.effective_message.reply_text(
        "Выберите предмет:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("item::"):
        await query.edit_message_text("Неизвестное действие.")
        return

    item_name = query.data.split("item::", 1)[1]
    chat_id = query.message.chat_id
    details = db.get_item_details(chat_id=chat_id, item_name=item_name)

    if not details:
        await query.edit_message_text(
            f"Запись для '{item_name}' не найдена (возможно, была удалена)."
        )
        return

    replaced_at, usage_days = details
    today = datetime.now(MOSCOW_TZ).date()
    due_date = calculate_due_date(replaced_at, usage_days)
    days_left = (due_date - today).days

    if days_left > 0:
        usage_left_text = f"Осталось: {days_left} дн."
    elif days_left == 0:
        usage_left_text = "Осталось: до конца дня"
    else:
        usage_left_text = f"Срок истек: {-days_left} дн. назад"

    await query.edit_message_text(
        (
            f"Предмет: {item_name}\n"
            f"Дата замены: {replaced_at.strftime('%d.%m.%Y')}\n"
            f"Срок использования: {usage_days} дн.\n"
            f"Дата окончания: {due_date.strftime('%d.%m.%Y')}\n"
            f"{usage_left_text}"
        )
    )


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Используйте кнопки меню или команду /start.", reply_markup=MAIN_KEYBOARD
    )


async def open_update_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    items = db.get_items(chat_id)

    if not items:
        await update.effective_message.reply_text(
            "Для этого чата пока нет сохраненных предметов.", reply_markup=MAIN_KEYBOARD
        )
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(text=item, callback_data=f"upd::{item}")]
        for item in items
    ]
    await update.effective_message.reply_text(
        "Выберите предмет, для которого нужно обновить дату замены:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return UPDATE_ITEM_SELECT


async def update_item_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("upd::"):
        await query.edit_message_text("Неизвестное действие.")
        return UPDATE_ITEM_SELECT

    item_name = query.data.split("upd::", 1)[1]
    context.user_data["update_item_name"] = item_name
    await query.edit_message_text(
        f"Введите новую дату замены для '{item_name}' в формате ДД.ММ.ГГГГ:"
    )
    return UPDATE_ITEM_DATE


async def update_item_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    item_name = context.user_data.get("update_item_name")
    date_raw = update.effective_message.text.strip()

    if not item_name:
        await update.effective_message.reply_text(
            "Не удалось определить предмет. Запустите обновление заново.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    try:
        replaced_at = parse_date(date_raw)
    except ValueError:
        await update.effective_message.reply_text(
            "Некорректная дата. Используйте формат ДД.ММ.ГГГГ (например, 02.05.2026):"
        )
        return UPDATE_ITEM_DATE

    updated = db.update_item_replaced_at(
        chat_id=chat_id,
        item_name=item_name,
        replaced_at=replaced_at,
    )
    if not updated:
        context.user_data.pop("update_item_name", None)
        await update.effective_message.reply_text(
            f"Предмет '{item_name}' не найден в этом чате.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    details = db.get_item_details(chat_id=chat_id, item_name=item_name)
    usage_days = details[1] if details else 0
    due_date = calculate_due_date(replaced_at, usage_days)
    context.user_data.pop("update_item_name", None)

    await update.effective_message.reply_text(
        (
            f"Дата замены обновлена:\n"
            f"- Предмет: {item_name}\n"
            f"- Новая дата: {replaced_at.strftime('%d.%m.%Y')}\n"
            f"- Дата окончания: {due_date.strftime('%d.%m.%Y')}"
        ),
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def send_expired_notifications(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    today = datetime.now(MOSCOW_TZ).date()
    expired_by_chat = db.get_expired_items_by_chat(today)

    for chat_id, items in expired_by_chat.items():
        lines = ["Напоминание: срок использования истек у следующих предметов:"]
        for item_name, replaced_at, usage_days in items:
            due_date = calculate_due_date(replaced_at, usage_days)
            lines.append(f"- {item_name} (истек: {due_date.strftime('%d.%m.%Y')})")
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


async def open_delete_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    items = db.get_items(chat_id)

    if not items:
        await update.effective_message.reply_text(
            "Для этого чата пока нет сохраненных предметов.", reply_markup=MAIN_KEYBOARD
        )
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(text=item, callback_data=f"del::{item}")]
        for item in items
    ]
    await update.effective_message.reply_text(
        "Выберите предмет для удаления:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return DELETE_ITEM_SELECT


async def delete_item_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("del::"):
        await query.edit_message_text("Неизвестное действие.")
        return DELETE_ITEM_SELECT

    item_name = query.data.split("del::", 1)[1]
    context.user_data["delete_item_name"] = item_name
    buttons = [
        [
            InlineKeyboardButton("Да, удалить", callback_data="del_confirm::yes"),
            InlineKeyboardButton("Нет", callback_data="del_confirm::no"),
        ]
    ]
    await query.edit_message_text(
        f"Удалить предмет '{item_name}'?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return DELETE_ITEM_CONFIRM


async def delete_item_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.application.bot_data["db"]
    query = update.callback_query
    await query.answer()

    action = query.data.split("del_confirm::", 1)[1]
    item_name = context.user_data.get("delete_item_name")
    chat_id = query.message.chat_id

    if action == "no":
        context.user_data.pop("delete_item_name", None)
        await query.edit_message_text("Удаление отменено.")
        await context.bot.send_message(chat_id=chat_id, text="Действие отменено.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    if not item_name:
        await query.edit_message_text("Не удалось определить предмет. Запустите удаление заново.")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Не удалось определить предмет. Запустите удаление заново.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    deleted = db.delete_item(chat_id=chat_id, item_name=item_name)
    context.user_data.pop("delete_item_name", None)
    if deleted:
        await query.edit_message_text(f"Предмет '{item_name}' удален.")
    else:
        await query.edit_message_text(
            f"Предмет '{item_name}' не найден (возможно, уже удален)."
        )

    await context.bot.send_message(chat_id=chat_id, text="Готово.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN в .env")

    app = Application.builder().token(token).build()
    app.bot_data["db"] = get_db()

    add_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & filters.Regex(f"^{BTN_ADD}$"), open_add_flow)
        ],
        states={
            ADD_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_name)],
            ADD_ITEM_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_date)],
            ADD_ITEM_USAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_usage)],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
        allow_reentry=True,
    )
    update_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & filters.Regex(f"^{BTN_UPDATE}$"), open_update_flow),
            CommandHandler("update_date", open_update_flow),
        ],
        states={
            UPDATE_ITEM_SELECT: [CallbackQueryHandler(update_item_pick, pattern=r"^upd::")],
            UPDATE_ITEM_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_item_date)],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
        allow_reentry=True,
    )
    delete_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & filters.Regex(f"^{BTN_DELETE}$"), open_delete_flow),
            CommandHandler("delete_item", open_delete_flow),
        ],
        states={
            DELETE_ITEM_SELECT: [CallbackQueryHandler(delete_item_pick, pattern=r"^del::")],
            DELETE_ITEM_CONFIRM: [
                CallbackQueryHandler(delete_item_confirm, pattern=r"^del_confirm::")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", show_items))
    app.add_handler(add_conv)
    app.add_handler(update_conv)
    app.add_handler(delete_conv)
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(f"^{BTN_SHOW}$"), show_items)
    )
    app.add_handler(CallbackQueryHandler(item_callback, pattern=r"^item::"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))
    app.job_queue.run_daily(
        callback=send_expired_notifications,
        time=time(hour=19, minute=0, tzinfo=MOSCOW_TZ),
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
