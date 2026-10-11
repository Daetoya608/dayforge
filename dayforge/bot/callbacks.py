"""Данные inline-кнопок (лимит Telegram — 64 байта на callback_data)."""

from aiogram.filters.callback_data import CallbackData


class PlanCb(CallbackData, prefix="plan"):
    action: str  # approve, edit, reject, file, arch
    plan_id: int


class AnswerCb(CallbackData, prefix="ans"):
    question_id: int
    index: int
    option: int


class ContinueCb(CallbackData, prefix="cont"):
    project_id: int  # 0 — все проекты с незавершённым
    now: bool


class ActionCb(CallbackData, prefix="act"):
    action_id: int
    confirm: bool


class ProjectCb(CallbackData, prefix="prj"):
    action: str  # status, plan, report, pause, resume, next, replan_restart
    project_id: int


class NextCb(CallbackData, prefix="next"):
    action: str  # toggle, start
    project_id: int


class PriorityCb(CallbackData, prefix="prio"):
    project_id: int
    up: bool


class FailedTaskCb(CallbackData, prefix="fail"):
    action: str  # replan, retry, cancel
    task_id: int


class ProposalCb(CallbackData, prefix="prop"):
    action: str  # replan, dismiss
    key: str


class CalendarCb(CallbackData, prefix="cal"):
    action: str  # month, day, toggle, off, template, weekday, back
    year: int = 0
    month: int = 0
    day: int = 0
    project_id: int = 0
    weekday: int = -1


class ShortageCb(CallbackData, prefix="short"):
    project_id: int
    days: int  # 0 — открыть календарь


class SettingsCb(CallbackData, prefix="set"):
    key: str


class NewCb(CallbackData, prefix="new"):
    # idea_done, stack_auto, prio, agents_default, weekday, weekdays_done, name_ok, confirm, cancel
    action: str
    value: int = 0
