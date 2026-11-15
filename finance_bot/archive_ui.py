"""IFT-only Telegram controls for the existing Apps Script month archive."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ConversationHandler

from finance_bot.archive_client import ArchiveClient, ArchiveError

ARCHIVE_BUTTON = 'Архивация месяца'


def enabled(settings):
    return bool(getattr(settings, 'month_archive_enabled', False))


def _authorized(update, context):
    settings = context.application.bot_data['settings']
    return (enabled(settings) and update.effective_user is not None
            and update.effective_user.id == settings.allowed_telegram_user_id
            and update.effective_chat is not None and update.effective_chat.type == 'private'
            and (update.callback_query is None or update.callback_query.from_user.id == update.effective_user.id))


def client(context):
    data = context.application.bot_data
    if 'month_archive_client' not in data:
        s = data['settings']
        data['month_archive_client'] = ArchiveClient(url=s.month_archive_url, key=s.month_archive_key,
            owner=s.allowed_telegram_user_id, spreadsheet=s.spreadsheet_id,
            directory=s.category_change_state_dir)
    return data['month_archive_client']


async def _send(update, text, buttons=()):
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in buttons]) if buttons else None
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        except BadRequest as error:
            if 'message is not modified' not in str(error).lower():
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def _show(update, result):
    rid, status = result['request_id'], result['status']
    name = result.get('name') or result.get('period', '')
    action = lambda verb: f'arc:{verb}:{rid}'
    if status == 'preview':
        verb = 'Заменить архив' if result.get('replace') else 'Сохранить архив'
        await _send(update, f'{verb} за {name}?\nТраты пока не будут очищены.',
                    [[(verb, action('archive'))], [('Отмена', action('cancel'))]])
    elif status == 'archived':
        await _send(update, f'Архив за {name} сохранён и проверен.\nОчистить траты и перейти к следующему месяцу?',
            [[('Очистить траты и перейти к следующему месяцу', action('clear'))], [('Оставить траты', action('cancel'))]])
    elif status == 'complete':
        await _send(update, f'Архив за {name} сохранён. Траты очищены, период переключён.\n{result.get("url", "")}')
    elif status == 'kept':
        await _send(update, f'Архив за {name} сохранён, траты не очищены. Период не изменён.\n{result.get("url", "")}')
    elif status == 'cancelled':
        await _send(update, 'Архивация отменена. Данные не изменены.')
    elif status == 'stale':
        await _send(update, 'Данные изменились. Траты не очищены. Откройте /archive_month для нового подтверждения.')
    elif status == 'not_found':
        await _send(update, 'Запрос не найден. Не повторяйте запись вслепую; проверьте таблицу и откройте /archive_month.')
    else:
        text = ('Требуется проверка незавершённой архивации. Дальнейшие записи могут быть заблокированы.'
                if status == 'recovery_required' else 'Архивация ещё не завершена. Проверить результат того же запроса?')
        await _send(update, f'{text}\nЗапрос: {rid}', [[('Проверить статус', action('status'))]])


async def _error(update, context, error, request_id=None):
    messages = {
        'invalid_source': 'Не удалось подготовить архив: проверьте строки, период и состояние таблицы.',
        'unauthorized': 'Не удалось подтвердить подключение к ИФТ-макросу. Проверьте ключ и настройки web app.',
        'protocol_mismatch': 'Версия ИФТ-макроса не соответствует боту. Проверьте deployment web app.',
        'journal_invalid': 'Сохранённый запрос повреждён. Нужна проверка журнала; новый запрос не отправлен.',
    }
    text = messages.get(error.code, 'Не удалось подтвердить результат. Не повторяйте архивацию вслепую; проверьте статус того же запроса.')
    try:
        rid = request_id or (client(context)._load() if error.code != 'journal_invalid' else None)
    except ArchiveError:
        rid = None
    buttons = [[('Проверить статус', f'arc:status:{rid}')]] if rid else []
    if rid and error.code == 'invalid_source':
        buttons.append([('Отменить запрос', f'arc:cancel:{rid}')])
    await _send(update, text, buttons)


async def start_archive(update, context):
    if not _authorized(update, context):
        return ConversationHandler.END
    from finance_bot import bot, category_ui
    had_flow = any(key in context.user_data for key in bot._FLOW_KEYS)
    await category_ui.leave_categories(update, context)
    bot._forget_flow(context)
    if had_flow:
        await update.effective_message.reply_text('Неподтверждённый ввод отменён. Открываю архивацию месяца.')
    try:
        await _show(update, await client(context).begin())
    except ArchiveError as error:
        await _error(update, context, error)
    return ConversationHandler.END


async def archive_callback(update, context):
    query = update.callback_query
    if not _authorized(update, context):
        if query:
            await query.answer('Недоступно', show_alert=True)
        return
    await query.answer()
    _, action, rid = query.data.split(':', 2)
    try:
        await _show(update, await client(context).action(action, rid))
    except ArchiveError as error:
        await _error(update, context, error, rid)
