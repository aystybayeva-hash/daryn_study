"""
Telegram-бот записи учеников в учебный центр «Daryn Study».
Тексты для родителей — на казахском языке.

Собирает по шагам (FSM):
  1. ФИО ученика
  2. Дата рождения ученика
  3. ФИО родителя (законного представителя)
  4. Телефон родителя
  5. Предмет (свободный текст)
  6. Согласие на обработку персональных данных

После согласия заявка записывается в Google Таблицу.

Требуется:
  - aiogram 3.x, режим polling
  - BOT_TOKEN                 — токен бота (переменная окружения)
  - GOOGLE_CREDENTIALS_FILE   — путь к JSON-ключу сервисного аккаунта Google
                                (по умолчанию "credentials.json" рядом со скриптом)
  - SPREADSHEET_ID            — ID Google Таблицы (из её URL)
  - WORKSHEET_NAME            — имя листа (по умолчанию "Заявки")

Все ошибки логируются в консоль.
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Optional

import gspread
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("enroll_bot")

# ---------------------------------------------------------------------------
# Настройки из окружения
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GOOGLE_CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE", os.path.join(BASE_DIR, "credentials.json")
)
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Заявки")

CENTER_NAME = "Daryn Study"

SHEET_HEADERS = [
    "Дата и время заявки",
    "ФИО ученика",
    "Дата рождения ученика",
    "ФИО родителя",
    "Телефон родителя",
    "Предмет",
    "Telegram username",
    "Telegram ID",
]

# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------
# Все тексты для родителей — на казахском языке.
BTN_ENROLL = "📝 Жазылу"
BTN_HELP = "❓ Көмек"
BTN_CANCEL = "❌ Бас тарту"
BTN_CONSENT_YES = "✅ Келісемін"
BTN_CONSENT_NO = "❌ Келіспеймін"

WELCOME_TEXT = (
    f"👋 Сәлеметсіз бе! Бұл «{CENTER_NAME}» оқу орталығына жазылу боты.\n\n"
    "Өтінім қалдыру үшін «📝 Жазылу» батырмасын басыңыз."
)

HELP_TEXT = (
    "🤖 <b>Қалай жазылуға болады:</b>\n\n"
    "1. «📝 Жазылу» батырмасын басыңыз\n"
    "2. Бірнеше сұраққа кезекпен жауап беріңіз (оқушының аты-жөні, туған күні, "
    "ата-ана деректері, телефон, пән)\n"
    "3. Жеке деректерді өңдеуге келісіміңізді растаңыз\n\n"
    "Кез келген уақытта /cancel командасы немесе «❌ Бас тарту» батырмасы "
    "арқылы өтінімнен бас тартуға болады."
)

CONSENT_TEXT = (
    "📋 <b>Өтінім деректерін тексеріңіз:</b>\n\n"
    "Оқушы: {child_name}\n"
    "Туған күні: {child_dob}\n"
    "Ата-ана: {parent_name}\n"
    "Ата-ананың телефоны: {parent_phone}\n"
    "Пән: {subject}\n\n"
    f"Өтінімді жіберу арқылы сіз көрсетілген жеке деректердің «{CENTER_NAME}» "
    "оқу орталығы тарапынан оқу процесін ұйымдастыру мақсатында өңделуіне "
    "келісім бересіз (ҚР «Дербес деректер және оларды қорғау туралы» "
    "заңына сәйкес).\n\n"
    "⚠️ Келісім мәтінін орталықтың заңгерімен келісіп, дербес деректерді "
    "өңдеу саясатын ашық қолжетімді ету ұсынылады."
)

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_ENROLL)], [KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True,
)

cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)

consent_inline_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text=BTN_CONSENT_YES, callback_data="consent_yes"),
            InlineKeyboardButton(text=BTN_CONSENT_NO, callback_data="consent_no"),
        ]
    ]
)

# ---------------------------------------------------------------------------
# Состояния анкеты (FSM)
# ---------------------------------------------------------------------------
class Registration(StatesGroup):
    child_name = State()
    child_dob = State()
    parent_name = State()
    parent_phone = State()
    subject = State()
    consent = State()


# ---------------------------------------------------------------------------
# Валидация
# ---------------------------------------------------------------------------
NAME_PATTERN = re.compile(r"^[а-яёa-z\-\s]+$", re.IGNORECASE)


def is_valid_full_name(text: str) -> bool:
    parts = text.strip().split()
    if len(parts) < 2:
        return False
    return all(NAME_PATTERN.match(part) for part in parts)


def is_valid_dob(text: str) -> Optional[datetime]:
    text = text.strip()
    try:
        dob = datetime.strptime(text, "%d.%m.%Y")
    except ValueError:
        return None

    today = datetime.now()
    if dob > today:
        return None
    age_years = (today - dob).days / 365.25
    if age_years > 100:
        return None
    return dob


def normalize_phone(text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text)

    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits

    if len(digits) != 11 or not digits.startswith("7"):
        return None

    return "+" + digits


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
_gs_client: Optional[gspread.Client] = None
_worksheet: Optional[gspread.Worksheet] = None


def init_google_sheet() -> None:
    """Подключается к Google Таблице и готовит лист с заголовками."""
    global _gs_client, _worksheet

    if not SPREADSHEET_ID:
        logger.error(
            "Переменная окружения SPREADSHEET_ID не задана — запись в таблицу "
            "работать не будет."
        )
        return

    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        logger.error(
            "Файл ключа сервисного аккаунта не найден: %s — запись в таблицу "
            "работать не будет.",
            GOOGLE_CREDENTIALS_FILE,
        )
        return

    try:
        _gs_client = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
        spreadsheet = _gs_client.open_by_key(SPREADSHEET_ID)

        try:
            _worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            _worksheet = spreadsheet.add_worksheet(
                title=WORKSHEET_NAME, rows=1000, cols=len(SHEET_HEADERS)
            )

        first_row = _worksheet.row_values(1)
        if first_row != SHEET_HEADERS:
            _worksheet.update("A1", [SHEET_HEADERS])

        logger.info("Подключение к Google Таблице установлено успешно.")
    except Exception:
        logger.exception("Не удалось подключиться к Google Таблице")
        _gs_client = None
        _worksheet = None


def save_registration(data: dict, username: str, user_id: int) -> bool:
    """Добавляет строку с заявкой в Google Таблицу. Возвращает успех операции."""
    if _worksheet is None:
        logger.error("Лист Google Таблицы недоступен, заявка не сохранена: %s", data)
        return False

    row = [
        datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        data["child_name"],
        data["child_dob"],
        data["parent_name"],
        data["parent_phone"],
        data["subject"],
        f"@{username}" if username else "—",
        str(user_id),
    ]

    try:
        _worksheet.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception:
        logger.exception("Ошибка при записи заявки в Google Таблицу: %s", data)
        return False


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_keyboard)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_keyboard)


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message) -> None:
    await cmd_help(message)


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cancel_registration(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Қазір жойылатын белсенді өтінім жоқ.", reply_markup=main_keyboard)
        return

    await state.clear()
    await message.answer("Өтінім тоқтатылды. Деректер сақталған жоқ.", reply_markup=main_keyboard)


# --- Шаг 1: старт анкеты -----------------------------------------------
@router.message(F.text == BTN_ENROLL)
@router.message(Command("enroll"))
async def start_registration(message: Message, state: FSMContext) -> None:
    await state.set_state(Registration.child_name)
    await message.answer(
        "Оқушының <b>толық аты-жөнін</b> енгізіңіз "
        "(мысалы: Иванов Иван Иванович):",
        reply_markup=cancel_keyboard,
    )


# --- Шаг 2: ФИО ученика ---------------------------------------------------
@router.message(StateFilter(Registration.child_name))
async def process_child_name(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not is_valid_full_name(text):
        await message.answer(
            "⚠️ Аты-жөні толық емес сияқты. Тегі мен атын (әкесінің атын да "
            "қосуға болады) әріптермен енгізіңіз, мысалы: Иванов Иван Иванович."
        )
        return

    await state.update_data(child_name=text)
    await state.set_state(Registration.child_dob)
    await message.answer(
        "Оқушының <b>туған күнін</b> КК.АА.ЖЖЖЖ форматында енгізіңіз:"
    )


# --- Шаг 3: дата рождения --------------------------------------------------
@router.message(StateFilter(Registration.child_dob))
async def process_child_dob(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    dob = is_valid_dob(text)
    if dob is None:
        await message.answer(
            "⚠️ Күнді тани алмадым. КК.АА.ЖЖЖЖ форматында енгізіңіз, "
            "мысалы: 15.03.2013."
        )
        return

    await state.update_data(child_dob=dob.strftime("%d.%m.%Y"))
    await state.set_state(Registration.parent_name)
    await message.answer(
        "<b>Ата-ананың (заңды өкілдің) аты-жөнін</b> енгізіңіз:"
    )


# --- Шаг 4: ФИО родителя ----------------------------------------------------
@router.message(StateFilter(Registration.parent_name))
async def process_parent_name(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not is_valid_full_name(text):
        await message.answer(
            "⚠️ Аты-жөні толық емес сияқты. Ата-ананың тегі мен атын енгізіңіз, "
            "мысалы: Иванова Мария Петровна."
        )
        return

    await state.update_data(parent_name=text)
    await state.set_state(Registration.parent_phone)
    await message.answer(
        "Байланысу үшін <b>ата-ананың телефонын</b> енгізіңіз "
        "(мысалы: +7 700 123-45-67):"
    )


# --- Шаг 5: телефон родителя -------------------------------------------------
@router.message(StateFilter(Registration.parent_phone))
async def process_parent_phone(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    phone = normalize_phone(text)
    if phone is None:
        await message.answer(
            "⚠️ Телефон нөмірін тани алмадым. Нөмірді енгізіңіз, "
            "мысалы: +7 700 123-45-67 немесе 8 700 123-45-67."
        )
        return

    await state.update_data(parent_phone=phone)
    await state.set_state(Registration.subject)
    await message.answer("Қандай <b>пәнге</b> жазылғыңыз келеді?")


# --- Шаг 6: предмет (свободный текст) ---------------------------------------
@router.message(StateFilter(Registration.subject))
async def process_subject(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("⚠️ Пән атауын мәтінмен жазыңыз.")
        return

    await state.update_data(subject=text)
    await state.set_state(Registration.consent)

    data = await state.get_data()
    await message.answer(
        CONSENT_TEXT.format(
            child_name=data["child_name"],
            child_dob=data["child_dob"],
            parent_name=data["parent_name"],
            parent_phone=data["parent_phone"],
            subject=data["subject"],
        ),
        reply_markup=consent_inline_keyboard,
    )


# --- Шаг 7: согласие на обработку ПД ----------------------------------------
@router.callback_query(StateFilter(Registration.consent), F.data == "consent_yes")
async def process_consent_yes(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    username = callback.from_user.username or ""
    user_id = callback.from_user.id

    saved = save_registration(data, username, user_id)

    await callback.message.edit_reply_markup(reply_markup=None)

    if saved:
        await callback.message.answer(
            "✅ Өтінім қабылданды! Біз көрсетілген телефон нөмірі арқылы "
            "сізбен байланысамыз.",
            reply_markup=main_keyboard,
        )
    else:
        await callback.message.answer(
            "⚠️ Өтінім толтырылды, бірақ сақтау кезінде қате шықты. "
            "Орталық әкімшісіне тікелей хабарласыңыз немесе өтінімді "
            "кейінірек қайта жіберіп көріңіз.",
            reply_markup=main_keyboard,
        )

    await state.clear()
    await callback.answer()


@router.callback_query(StateFilter(Registration.consent), F.data == "consent_no")
async def process_consent_no(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Жеке деректерді өңдеуге келісімсіз өтінім рәсімделмейді. "
        "Деректер сақталған жоқ.",
        reply_markup=main_keyboard,
    )
    await state.clear()
    await callback.answer()


# --- Заглушка вне анкеты -----------------------------------------------------
@router.message(StateFilter(None))
async def fallback(message: Message) -> None:
    await message.answer(
        "Түсінбей қалдым 🙂 Өтінім қалдыру үшін «📝 Жазылу» батырмасын "
        "басыңыз, немесе «❓ Көмек» — бұл қалай жұмыс істейтіні туралы "
        "толығырақ.",
        reply_markup=main_keyboard,
    )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error(
            "Переменная окружения BOT_TOKEN не задана. "
            "Установите её перед запуском: export BOT_TOKEN=... (Linux/macOS) "
            "или set BOT_TOKEN=... (Windows)."
        )
        raise SystemExit(1)

    init_google_sheet()

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запускается в режиме polling...")
    try:
        await dp.start_polling(bot)
    except Exception:
        logger.exception("Бот аварийно завершил работу")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
