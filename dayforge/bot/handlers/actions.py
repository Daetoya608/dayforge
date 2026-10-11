"""Подтверждение действий системного агента."""

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from dayforge.bot.callbacks import ActionCb
from dayforge.bot.context import AppContext
from dayforge.bot.formatting import h
from dayforge.core.enums import PendingActionStatus
from dayforge.core.pending_actions import PendingActionError

router = Router(name="actions")


@router.callback_query(ActionCb.filter())
async def decide(query: CallbackQuery, callback_data: ActionCb, app: AppContext) -> None:
    message = query.message
    assert isinstance(message, Message)
    try:
        decision = await app.services.actions.decide(
            callback_data.action_id, confirm=callback_data.confirm
        )
    except PendingActionError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    except Exception as exc:
        await message.edit_reply_markup(reply_markup=None)
        await message.answer(f"Не получилось: <code>{h(exc)}</code>")
        await query.answer()
        return
    await message.edit_reply_markup(reply_markup=None)
    match decision.status:
        case PendingActionStatus.CONFIRMED:
            await message.answer(f"✅ {h(decision.result or 'Готово')}")
        case PendingActionStatus.EXPIRED:
            await message.answer("⌛ Истекло — действие не выполнено.")
        case _:
            await message.answer("Отменено.")
    await query.answer()
