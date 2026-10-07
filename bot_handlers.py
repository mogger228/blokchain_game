"""Telegram handlers: игрок, поиск, цепочка и защищённая админ-панель."""

from __future__ import annotations

import math
import uuid

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import service
import storage
from formatters import format_amount, format_block, split_chain


(
    TRANSFER_RECIPIENT,
    TRANSFER_AMOUNT,
    TRANSFER_CONFIRM,
    SEARCH_ID,
    ADMIN_RECIPIENT,
    ADMIN_AMOUNT,
    ADMIN_CUSTOM_AMOUNT,
    ADMIN_CONFIRM,
) = range(8)
TRANSFER_MESSAGE = 8

CANCEL = "flow:cancel"


def _admin_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.application.bot_data["admin_id"])


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return user is not None and user.id == _admin_id(context)


def _menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("💸 Создать транзакцию", callback_data="menu:transfer")],
        [
            InlineKeyboardButton("💰 Мой баланс", callback_data="menu:balance"),
            InlineKeyboardButton("🔎 Найти по ID", callback_data="menu:search"),
        ],
        [
            InlineKeyboardButton("⛓ Цепочка", callback_data="menu:chain"),
            InlineKeyboardButton("✅ Проверить", callback_data="menu:validate"),
        ],
        [InlineKeyboardButton("ℹ️ Правила", callback_data="menu:rules")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 Админ-панель", callback_data="admin:panel")])
    return InlineKeyboardMarkup(rows)


def _home_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:home")]])


def _cancel_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=CANCEL)]])


async def _edit_or_reply(update: Update, text: str, markup=None) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=markup)


def _clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        "transfer_recipient",
        "transfer_amount",
        "active_intent",
        "admin_recipient",
    ):
        context.user_data.pop(key, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_flow(context)
    if update.effective_chat is None or update.effective_chat.type != Chat.PRIVATE:
        await update.effective_message.reply_text("Откройте личный чат с ботом и отправьте /start.")
        return
    user = update.effective_user
    if user is None:
        return
    player = service.get_player(user.id)
    if player is None:
        if not user.username:
            text = "Для регистрации установите Telegram username и снова отправьте /start."
            await update.effective_message.reply_text(text)
            return
        try:
            result = service.register_player(user.id, user.username)
        except service.ServiceError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        player = result.player
        prefix = "Регистрация завершена. Стартовый баланс: $100.\n\n"
    else:
        prefix = ""
    balance = service.get_player_balance(user.id)
    await update.effective_message.reply_text(
        f"{prefix}Игрок: {player.account_name}\nБаланс: ${format_amount(balance.balance)}",
        reply_markup=_menu(_is_admin(update, context)),
    )


async def home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_flow(context)
    user = update.effective_user
    player = None if user is None else service.get_player(user.id)
    if player is None:
        await _edit_or_reply(update, "Сначала зарегистрируйтесь командой /start.", _home_button())
        return
    await _edit_or_reply(update, "Главное меню", _menu(_is_admin(update, context)))


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        result = service.get_player_balance(update.effective_user.id)
    except service.NotRegisteredError as exc:
        await _edit_or_reply(update, str(exc), _home_button())
        return
    await _edit_or_reply(
        update,
        f"Баланс {result.player.account_name}: ${format_amount(result.balance)}",
        _home_button(),
    )


async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Это учебная игра, не настоящая криптовалюта.\n"
        "Каждый игрок получает $100 один раз. Отправитель определяется по Telegram ID.\n"
        "Дополнительные монеты выпускает только администратор через защищённую панель."
    )
    await _edit_or_reply(update, text, _home_button())


async def show_validation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    valid, broken = service.check_chain()
    text = "Цепочка валидна." if valid else f"Цепочка повреждена на блоке #{broken}."
    await _edit_or_reply(update, text, _home_button())


async def show_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chunks = split_chain(service.get_chain())
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(chunks[0])
    for chunk in chunks[1:]:
        await query.message.reply_text(chunk)
    await query.message.reply_text("Конец цепочки.", reply_markup=_home_button())


async def begin_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_flow(context)
    await _edit_or_reply(update, "Введите неотрицательный ID транзакции:", _cancel_button())
    return SEARCH_ID


async def receive_search_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        block_id = int(update.effective_message.text.strip())
        if block_id < 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("ID должен быть неотрицательным целым числом.")
        return SEARCH_ID
    block = service.get_transaction(block_id)
    if block is None:
        await update.effective_message.reply_text(
            f"Транзакция #{block_id} не найдена.", reply_markup=_home_button()
        )
    else:
        text = format_block(block)
        if (
            _is_admin(update, context)
            and update.effective_chat is not None
            and update.effective_chat.type == Chat.PRIVATE
        ):
            message = storage.get_transaction_message(block.index)
            if message:
                text += f"\n\nСообщение отправителя:\n{message}"
        await update.effective_message.reply_text(text, reply_markup=_home_button())
    return ConversationHandler.END


async def begin_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_flow(context)
    if service.get_player(update.effective_user.id) is None:
        await _edit_or_reply(update, "Сначала зарегистрируйтесь через /start.", _home_button())
        return ConversationHandler.END
    await _edit_or_reply(update, "Введите username получателя:", _cancel_button())
    return TRANSFER_RECIPIENT


async def receive_transfer_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        username = service.normalize_username(update.effective_message.text)
        recipient = service.get_player_by_username(username)
        if recipient is None:
            raise service.PlayerNotFoundError("Получатель ещё не зарегистрирован.")
    except service.ServiceError as exc:
        await update.effective_message.reply_text(str(exc))
        return TRANSFER_RECIPIENT
    context.user_data["transfer_recipient"] = username
    await update.effective_message.reply_text("Введите сумму перевода:", reply_markup=_cancel_button())
    return TRANSFER_AMOUNT


def _parse_amount(text: str) -> float:
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError as exc:
        raise service.ValidationError("Сумма должна быть числом.") from exc
    if not math.isfinite(value) or value <= 0:
        raise service.ValidationError("Сумма должна быть положительным конечным числом.")
    return value


async def receive_transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = _parse_amount(update.effective_message.text)
    except service.ValidationError as exc:
        await update.effective_message.reply_text(str(exc))
        return TRANSFER_AMOUNT
    context.user_data["transfer_amount"] = amount
    await update.effective_message.reply_text(
        f"Добавьте сообщение получателю (до {service.MAX_TRANSFER_MESSAGE_LENGTH} символов) "
        "или нажмите «Без сообщения».",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Без сообщения", callback_data="tx:skip_message")],
            [InlineKeyboardButton("Отмена", callback_data=CANCEL)],
        ]),
    )
    return TRANSFER_MESSAGE


async def receive_transfer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        message = service.normalize_transfer_message(update.effective_message.text)
        if not message:
            raise service.ValidationError("Введите сообщение или нажмите «Без сообщения».")
    except service.ValidationError as exc:
        await update.effective_message.reply_text(str(exc))
        return TRANSFER_MESSAGE
    return await _prepare_transfer_confirmation(update, context, message)


async def skip_transfer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _prepare_transfer_confirmation(update, context, "")


async def _prepare_transfer_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str,
) -> int:
    amount = context.user_data["transfer_amount"]
    key = uuid.uuid4().hex
    intent = {
        "kind": "transfer",
        "key": key,
        "recipient": context.user_data["transfer_recipient"],
        "amount": amount,
        "message": message,
    }
    context.user_data["active_intent"] = intent
    markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Подтвердить", callback_data=f"tx:confirm:{key}"),
            InlineKeyboardButton("Отмена", callback_data=CANCEL),
        ]]
    )
    text = f"Перевести ${format_amount(amount)} пользователю @{intent['recipient']}?"
    text += f"\n\nСообщение:\n{message}" if message else "\nБез сообщения."
    await _edit_or_reply(update, text, markup)
    return TRANSFER_CONFIRM


async def confirm_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key = query.data.rsplit(":", 1)[-1]
    intent = context.user_data.get("active_intent")
    if not intent or intent.get("kind") != "transfer" or intent.get("key") != key:
        await query.edit_message_text("Это действие устарело.", reply_markup=_home_button())
        return ConversationHandler.END
    try:
        block = service.create_player_transaction(
            update.effective_user.id, intent["recipient"], intent["amount"], key,
            message=intent.get("message", ""),
        )
    except service.ServiceError as exc:
        _clear_flow(context)
        await query.edit_message_text(str(exc), reply_markup=_home_button())
        return ConversationHandler.END
    _clear_flow(context)
    await query.edit_message_text(
        f"Транзакция #{block.index} создана.\n{block.sender} → {block.recipient}: "
        f"${format_amount(block.amount)}\nHash: {block.hash}",
        reply_markup=_home_button(),
    )
    return ConversationHandler.END


def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        raise service.ForbiddenError("Недостаточно прав.")


def _admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 Пользователи и балансы", callback_data="admin:users:0")],
            [InlineKeyboardButton("➕ Начислить", callback_data="admin:grant")],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:home")],
        ]
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        _require_admin(update, context)
    except service.ForbiddenError as exc:
        await _edit_or_reply(update, str(exc), _home_button())
        return
    await _edit_or_reply(update, "Админ-панель", _admin_panel_keyboard())


def _players_keyboard(page, selecting: bool) -> InlineKeyboardMarkup:
    rows = []
    if selecting:
        for item in page.items:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"@{item.player.username} · ${format_amount(item.balance)}",
                        callback_data=f"grant:user:{item.player.username}",
                    )
                ]
            )
    nav = []
    prefix = "grant:page" if selecting else "admin:users"
    if page.page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}:{page.page - 1}"))
    if page.page + 1 < page.total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}:{page.page + 1}"))
    if nav:
        rows.append(nav)
    if selecting:
        rows.append([InlineKeyboardButton("Отмена", callback_data=CANCEL)])
    else:
        rows.append([InlineKeyboardButton("⬅️ Админ-панель", callback_data="admin:panel")])
    return InlineKeyboardMarkup(rows)


async def show_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        _require_admin(update, context)
        page_number = int(update.callback_query.data.rsplit(":", 1)[-1])
        page = service.list_players_with_balances(page_number)
    except (service.ServiceError, ValueError) as exc:
        await _edit_or_reply(update, str(exc), _home_button())
        return
    lines = [f"Игроки — страница {page.page + 1}/{page.total_pages}"]
    if not page.items:
        lines.append("Пока нет зарегистрированных игроков.")
    else:
        lines.extend(
            f"@{item.player.username}: ${format_amount(item.balance)}" for item in page.items
        )
    await _edit_or_reply(update, "\n".join(lines), _players_keyboard(page, False))


async def begin_admin_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_flow(context)
    try:
        _require_admin(update, context)
        page = service.list_players_with_balances(0)
    except service.ServiceError as exc:
        await _edit_or_reply(update, str(exc), _home_button())
        return ConversationHandler.END
    await _edit_or_reply(
        update,
        "Выберите игрока кнопкой или отправьте его username сообщением:",
        _players_keyboard(page, True),
    )
    return ADMIN_RECIPIENT


async def admin_grant_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        _require_admin(update, context)
        page_number = int(update.callback_query.data.rsplit(":", 1)[-1])
        page = service.list_players_with_balances(page_number)
    except (service.ServiceError, ValueError) as exc:
        await _edit_or_reply(update, str(exc), _home_button())
        return ConversationHandler.END
    await _edit_or_reply(
        update,
        "Выберите игрока кнопкой или отправьте его username сообщением:",
        _players_keyboard(page, True),
    )
    return ADMIN_RECIPIENT


def _grant_amount_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("$10", callback_data="grant:amount:10"),
                InlineKeyboardButton("$50", callback_data="grant:amount:50"),
                InlineKeyboardButton("$100", callback_data="grant:amount:100"),
            ],
            [InlineKeyboardButton("Другая сумма", callback_data="grant:amount:custom")],
            [InlineKeyboardButton("Отмена", callback_data=CANCEL)],
        ]
    )


async def _set_admin_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE, value: str) -> int:
    try:
        _require_admin(update, context)
        username = service.normalize_username(value)
        if service.get_player_by_username(username) is None:
            raise service.PlayerNotFoundError("Игрок не найден.")
    except service.ServiceError as exc:
        if update.callback_query:
            await _edit_or_reply(update, str(exc), _cancel_button())
        else:
            await update.effective_message.reply_text(str(exc))
        return ADMIN_RECIPIENT
    context.user_data["admin_recipient"] = username
    if update.callback_query:
        await _edit_or_reply(update, f"Начисление для @{username}. Выберите сумму:", _grant_amount_keyboard())
    else:
        await update.effective_message.reply_text(
            f"Начисление для @{username}. Выберите сумму:", reply_markup=_grant_amount_keyboard()
        )
    return ADMIN_AMOUNT


async def receive_admin_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _set_admin_recipient(update, context, update.effective_message.text)


async def select_admin_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _set_admin_recipient(update, context, update.callback_query.data.split(":", 2)[-1])


async def choose_admin_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        _require_admin(update, context)
    except service.ForbiddenError as exc:
        await _edit_or_reply(update, str(exc), _home_button())
        return ConversationHandler.END
    raw = update.callback_query.data.rsplit(":", 1)[-1]
    if raw == "custom":
        await _edit_or_reply(update, "Введите положительную сумму:", _cancel_button())
        return ADMIN_CUSTOM_AMOUNT
    return await _prepare_admin_confirmation(update, context, float(raw))


async def receive_custom_admin_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        _require_admin(update, context)
        amount = _parse_amount(update.effective_message.text)
    except service.ServiceError as exc:
        await update.effective_message.reply_text(str(exc))
        return ADMIN_CUSTOM_AMOUNT
    return await _prepare_admin_confirmation(update, context, amount)


async def _prepare_admin_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float
) -> int:
    key = uuid.uuid4().hex
    intent = {
        "kind": "admin_grant",
        "key": key,
        "recipient": context.user_data["admin_recipient"],
        "amount": amount,
    }
    context.user_data["active_intent"] = intent
    markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Подтвердить", callback_data=f"grant:confirm:{key}"),
            InlineKeyboardButton("Отмена", callback_data=CANCEL),
        ]]
    )
    text = f"Начислить ${format_amount(amount)} игроку @{intent['recipient']}?"
    if update.callback_query:
        await _edit_or_reply(update, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return ADMIN_CONFIRM


async def confirm_admin_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        _require_admin(update, context)
    except service.ForbiddenError as exc:
        _clear_flow(context)
        await query.edit_message_text(str(exc), reply_markup=_home_button())
        return ConversationHandler.END
    key = query.data.rsplit(":", 1)[-1]
    intent = context.user_data.get("active_intent")
    if not intent or intent.get("kind") != "admin_grant" or intent.get("key") != key:
        await query.edit_message_text("Это действие устарело.", reply_markup=_home_button())
        return ConversationHandler.END
    try:
        block = service.create_admin_grant(
            update.effective_user.id,
            intent["recipient"],
            intent["amount"],
            key,
            _admin_id(context),
        )
    except service.ServiceError as exc:
        _clear_flow(context)
        await query.edit_message_text(str(exc), reply_markup=_home_button())
        return ConversationHandler.END
    _clear_flow(context)
    await query.edit_message_text(
        f"Начисление записано транзакцией #{block.index}: "
        f"${format_amount(block.amount)} → {block.recipient}",
        reply_markup=_home_button(),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_flow(context)
    await _edit_or_reply(update, "Действие отменено.", _menu(_is_admin(update, context)))
    return ConversationHandler.END


async def expired_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query.data.startswith("grant:"):
        try:
            _require_admin(update, context)
        except service.ForbiddenError as exc:
            await _edit_or_reply(update, str(exc), _home_button())
            return
    await _edit_or_reply(update, "Это действие устарело.", _home_button())


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await start(update, context)
    return ConversationHandler.END


def register_handlers(application) -> None:
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", restart),
            CallbackQueryHandler(begin_transfer, pattern=r"^menu:transfer$"),
            CallbackQueryHandler(begin_search, pattern=r"^menu:search$"),
            CallbackQueryHandler(begin_admin_grant, pattern=r"^admin:grant$"),
        ],
        states={
            TRANSFER_RECIPIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_transfer_recipient)],
            TRANSFER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_transfer_amount)],
            TRANSFER_MESSAGE: [
                CallbackQueryHandler(skip_transfer_message, pattern=r"^tx:skip_message$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_transfer_message),
            ],
            TRANSFER_CONFIRM: [CallbackQueryHandler(confirm_transfer, pattern=r"^tx:confirm:[0-9a-f]{32}$")],
            SEARCH_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search_id)],
            ADMIN_RECIPIENT: [
                CallbackQueryHandler(admin_grant_page, pattern=r"^grant:page:\d+$"),
                CallbackQueryHandler(select_admin_recipient, pattern=r"^grant:user:[A-Za-z0-9_]{5,32}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_admin_recipient),
            ],
            ADMIN_AMOUNT: [CallbackQueryHandler(choose_admin_amount, pattern=r"^grant:amount:(10|50|100|custom)$")],
            ADMIN_CUSTOM_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_admin_amount)],
            ADMIN_CONFIRM: [CallbackQueryHandler(confirm_admin_grant, pattern=r"^grant:confirm:[0-9a-f]{32}$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
            CallbackQueryHandler(cancel, pattern=r"^menu:home$"),
        ],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(home, pattern=r"^menu:home$"))
    application.add_handler(CallbackQueryHandler(show_balance, pattern=r"^menu:balance$"))
    application.add_handler(CallbackQueryHandler(show_chain, pattern=r"^menu:chain$"))
    application.add_handler(CallbackQueryHandler(show_validation, pattern=r"^menu:validate$"))
    application.add_handler(CallbackQueryHandler(show_rules, pattern=r"^menu:rules$"))
    application.add_handler(CallbackQueryHandler(admin_panel, pattern=r"^admin:panel$"))
    application.add_handler(CallbackQueryHandler(show_admin_users, pattern=r"^admin:users:\d+$"))
    application.add_handler(
        CallbackQueryHandler(expired_action, pattern=r"^(tx|grant):confirm:[0-9a-f]{32}$")
    )
