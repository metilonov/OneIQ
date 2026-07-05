import os
import re
import html
import json
import asyncio
from pathlib import Path

from dotenv import load_dotenv, set_key, unset_key
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from openai import OpenAI


# ================= НАСТРОЙКИ =================

ADMIN_ID = 8938864725  # сюда вставь свой Telegram ID

ENV_PATH = Path(".env")
USERS_PATH = Path("users_access.json")

load_dotenv(dotenv_path=ENV_PATH)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ================= ДОСТУП ПОЛЬЗОВАТЕЛЕЙ =================

def load_users():
    if not USERS_PATH.exists():
        return {}

    try:
        with open(USERS_PATH, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {}


def save_users(users):
    with open(USERS_PATH, "w", encoding="utf-8") as file:
        json.dump(users, file, ensure_ascii=False, indent=4)


def get_user_status(user_id: int) -> str:
    users = load_users()
    return users.get(str(user_id), "new")


def set_user_status(user_id: int, status: str):
    users = load_users()
    users[str(user_id)] = status
    save_users(users)


def user_link(user: types.User) -> str:
    name = html.escape(user.full_name)

    if user.username:
        return f'<a href="https://t.me/{user.username}">{name}</a>'

    return f'<a href="tg://user?id={user.id}">{name}</a>'


async def request_access(message: types.Message):
    user = message.from_user

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Разрешить доступ",
                    callback_data=f"allow_user:{user.id}",
                ),
                InlineKeyboardButton(
                    text="❌ Запретить",
                    callback_data=f"deny_user:{user.id}",
                ),
            ]
        ]
    )

    await bot.send_message(
        ADMIN_ID,
        (
            f"🆕 {user_link(user)} хочет написать сообщение.\n\n"
            f"ID: <code>{user.id}</code>"
        ),
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    await message.answer("⏳ Заявка отправлена администратору. Ожидай разрешения.")


# ================= OPENAI КЛЮЧИ =================

def is_valid_openai_key(key: str) -> bool:
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        return False

    return key.startswith("sk-")


def load_api_keys():
    keys_count = int(os.getenv("OPENAI_KEYS_COUNT", "0"))
    keys = []

    for i in range(1, keys_count + 1):
        key = os.getenv(f"OPENAI_API_KEY_{i}")

        if key:
            key = key.strip()

            if is_valid_openai_key(key):
                keys.append(key)

    return keys


def save_api_keys(keys):
    old_count = int(os.getenv("OPENAI_KEYS_COUNT", "0"))

    for i in range(1, old_count + 1):
        unset_key(str(ENV_PATH), f"OPENAI_API_KEY_{i}")
        os.environ.pop(f"OPENAI_API_KEY_{i}", None)

    for index, key in enumerate(keys, start=1):
        set_key(str(ENV_PATH), f"OPENAI_API_KEY_{index}", key)
        os.environ[f"OPENAI_API_KEY_{index}"] = key

    set_key(str(ENV_PATH), "OPENAI_KEYS_COUNT", str(len(keys)))
    os.environ["OPENAI_KEYS_COUNT"] = str(len(keys))


def add_api_key(new_key: str):
    keys = load_api_keys()

    if new_key in keys:
        return False, "Такой ключ уже есть."

    keys.append(new_key)
    save_api_keys(keys)

    return True, f"Ключ добавлен. Всего ключей: {len(keys)}"


def delete_api_key_by_number(key_number: int):
    keys = load_api_keys()

    if key_number < 1 or key_number > len(keys):
        return False, "Ключ с таким номером не найден."

    keys.pop(key_number - 1)
    save_api_keys(keys)

    return True, f"Ключ №{key_number} удалён. Осталось ключей: {len(keys)}"


def remove_bad_key(bad_key):
    keys = load_api_keys()

    if bad_key in keys:
        keys.remove(bad_key)
        save_api_keys(keys)

    return keys


def is_quota_error(error_text: str) -> bool:
    error_text = error_text.lower()

    quota_keywords = [
        "insufficient_quota",
        "quota",
        "billing",
        "rate limit",
        "rate_limit",
        "429",
        "exceeded",
    ]

    return any(word in error_text for word in quota_keywords)


# ================= ФОРМАТИРОВАНИЕ HTML =================

def format_to_telegram_html(text: str) -> str:
    code_blocks = []

    def save_code_block(match):
        code = match.group(1)
        placeholder = f"___CODE_BLOCK_{len(code_blocks)}___"
        code_blocks.append(f"<pre><code>{html.escape(code)}</code></pre>")
        return placeholder

    text = re.sub(
        r"```(?:\w+)?\n?(.*?)```",
        save_code_block,
        text,
        flags=re.DOTALL,
    )

    text = html.escape(text)

    text = re.sub(r"^### (.*)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.*)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.*)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"^- (.*)$", r"• \1", text, flags=re.MULTILINE)

    for index, block in enumerate(code_blocks):
        text = text.replace(f"___CODE_BLOCK_{index}___", block)

    return text


async def send_long_html(chat_id: int, text: str):
    lines = text.split("\n")
    chunk = ""

    for line in lines:
        if len(chunk) + len(line) + 1 > 3900:
            await bot.send_message(
                chat_id,
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            chunk = ""

        chunk += line + "\n"

    if chunk.strip():
        await bot.send_message(
            chat_id,
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def notify_admin(error_text: str):
    print("OPENAI ERROR:", error_text)

    try:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Ошибка OpenAI API:\n\n<code>{html.escape(error_text)}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# ================= OPENAI =================

async def ask_openai(user_text: str) -> str:
    while True:
        keys = load_api_keys()

        if not keys:
            return "❌ Все OpenAI API-ключи удалены или израсходовали квоту."

        current_key = keys[0]
        client = OpenAI(api_key=current_key)

        try:
            response = client.responses.create(
                model=OPENAI_MODEL,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "Ты умный Telegram-ассистент. "
                            "Отвечай на русском языке. "
                            "Обычный текст пиши без Markdown-разметки. "
                            "Если отправляешь код, обязательно оборачивай его в тройные кавычки ```код```. "
                            "Для списков используй символ •. "
                            "Отвечай понятно, аккуратно и полезно."
                        ),
                    },
                    {
                        "role": "user",
                        "content": user_text,
                    },
                ],
                store=True,
            )

            return response.output_text or "⚠️ Нейросеть не вернула ответ."

        except Exception as e:
            error_text = str(e)

            if is_quota_error(error_text):
                remove_bad_key(current_key)
                continue

            await notify_admin(error_text)
            return "⚠️ Произошла ошибка при обращении к нейросети. Администратор уведомлён."


# ================= КОМАНДЫ =================

@dp.message(CommandStart())
async def start(message: types.Message):
    user_id = message.from_user.id

    if user_id == ADMIN_ID:
        set_user_status(user_id, "allowed")
        await message.answer(
            "👑 Привет, админ.\n\n"
            "Команды:\n"
            "/key sk-... — добавить OpenAI API ключ\n"
            "/keys — количество ключей\n"
            "/openkey — показать все ключи\n"
            "/delete key 1 — удалить ключ по номеру"
        )
        return

    status = get_user_status(user_id)

    if status == "allowed":
        await message.answer("✅ Доступ разрешён. Можешь писать сообщение.")
        return

    if status == "denied":
        await message.answer("❌ Доступ запрещён администратором.")
        return

    set_user_status(user_id, "pending")
    await request_access(message)


@dp.message(Command("key"))
async def add_key_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split(maxsplit=1)

    try:
        await message.delete()
    except Exception:
        pass

    if len(parts) < 2:
        await message.answer("Использование:\n/key sk-...")
        return

    new_key = parts[1].strip()

    if not is_valid_openai_key(new_key):
        await message.answer("❌ Неверный формат ключа. Ключ должен начинаться с sk- и быть ASCII.")
        return

    success, text = add_api_key(new_key)

    if success:
        await message.answer(f"✅ {text}")
    else:
        await message.answer(f"⚠️ {text}")


@dp.message(Command("keys"))
async def keys_count_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(f"🔑 Активных OpenAI API-ключей: {len(load_api_keys())}")


@dp.message(Command("openkey"))
async def open_keys_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    keys = load_api_keys()

    if not keys:
        await message.answer(
            "<i>Активных ключей бота OneIQ нет.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    text = "<i>Активные ключи бота OneIQ:</i>\n\n"

    for index, key in enumerate(keys, start=1):
        text += (
            f"<i>{index}.</i>\n"
            f"<code>{html.escape(key)}</code>\n\n"
        )

    await send_long_html(message.chat.id, text)


@dp.message(Command("delete"))
async def delete_key_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 3 or parts[1].lower() != "key":
        await message.answer(
            "<i>Использование:</i>\n<code>/delete key 1</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        key_number = int(parts[2])
    except ValueError:
        await message.answer(
            "<i>Номер ключа должен быть числом.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    success, result_text = delete_api_key_by_number(key_number)

    if success:
        await message.answer(
            f"<i>✅ {html.escape(result_text)}</i>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            f"<i>❌ {html.escape(result_text)}</i>",
            parse_mode=ParseMode.HTML,
        )


# ================= CALLBACKS =================

@dp.callback_query(F.data.startswith("allow_user:"))
async def allow_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    set_user_status(user_id, "allowed")

    try:
        await callback.message.delete()
    except Exception:
        pass

    await bot.send_message(user_id, "✅ Администратор разрешил тебе доступ. Теперь можешь писать.")
    await callback.answer("Доступ разрешён.")


@dp.callback_query(F.data.startswith("deny_user:"))
async def deny_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    set_user_status(user_id, "denied")

    try:
        await callback.message.delete()
    except Exception:
        pass

    await bot.send_message(user_id, "❌ Администратор запретил тебе доступ.")
    await callback.answer("Доступ запрещён.")


# ================= СООБЩЕНИЯ =================

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id

    if user_id != ADMIN_ID:
        status = get_user_status(user_id)

        if status == "new":
            set_user_status(user_id, "pending")
            await request_access(message)
            return

        if status == "pending":
            await message.answer("⏳ Твоя заявка ещё на рассмотрении.")
            return

        if status == "denied":
            await message.answer("❌ Доступ запрещён администратором.")
            return

    if not message.text:
        await message.answer("⚠️ Отправь текстовое сообщение.")
        return

    wait_msg = await message.answer("⏳ Думаю...")

    answer = await ask_openai(message.text)
    answer = format_to_telegram_html(answer)

    try:
        await wait_msg.delete()
    except Exception:
        pass

    for i in range(0, len(answer), 4096):
        await message.answer(
            answer[i:i + 4096],
            parse_mode=ParseMode.HTML,
        )


async def main():
    print("Бот запущен.")
    print(f"Админ ID: {ADMIN_ID}")
    print(f"Модель: {OPENAI_MODEL}")
    print(f"Активных OpenAI API-ключей: {len(load_api_keys())}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())