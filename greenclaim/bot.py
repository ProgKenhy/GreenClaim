"""Telegram-бот GreenClaim (MVP): «свалка» и «вырубка» в Калининградской области.

Запуск:  python -m greenclaim.bot
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import config
from .geo import in_region, nearest_center, read_photo_meta, yandex_map_url
from .legal import LegalMatrix
from .pdf import ComplaintData, build_complaint_pdf
from .routing import resolve
from .storage import Storage

log = logging.getLogger("greenclaim")
router = Router()
matrix = LegalMatrix()
db = Storage(config.DB_PATH)


class Flow(StatesGroup):
    questions = State()
    photos = State()
    location = State()
    description = State()
    name = State()
    address = State()
    confirm = State()


# ---------- клавиатуры ----------

def kb(rows: list[tuple[str, str]], width: int = 1) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, data in rows:
        b.button(text=text, callback_data=data)
    b.adjust(width)
    return b.as_markup()


def scenario_kb() -> InlineKeyboardMarkup:
    return kb([(f"{s['emoji']} {s['title']}", f"sc:{k}") for k, s in matrix.scenarios.items()])


# ---------- старт и согласие ----------

WELCOME = (
    "🌿 <b>GreenClaim — твой карманный эко-юрист</b>\n\n"
    "Помогу за пару минут оформить обращение о нарушении: подберу нормы права "
    "и ведомство, соберу PDF с фото и координатами.\n\n"
    "<b>Данные.</b> Для обращения нужны ваши ФИО и адрес для ответа (требование 59-ФЗ). "
    "Они используются только для создания документа и <b>не сохраняются</b> на сервере. "
    "Сохраняется лишь обезличенная запись о нарушении (тип, координаты, время) "
    "для карты экологической обстановки.\n\n"
    "Продолжая, вы соглашаетесь на обработку этих данных."
)


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await _cleanup(m.chat.id)
    await state.clear()
    await m.answer(WELCOME, reply_markup=kb([("✅ Согласен(на), продолжить", "consent")]))


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await _cleanup(m.chat.id)
    await state.clear()
    await m.answer("Отменено. Чтобы начать заново, нажмите /start", reply_markup=ReplyKeyboardRemove())


@router.callback_query(F.data == "consent")
async def on_consent(c: CallbackQuery):
    await c.message.answer("Что случилось?", reply_markup=scenario_kb())
    await c.answer()


# ---------- сценарий и вопросы ----------

async def _ask_question(msg: Message, scenario: str, idx: int):
    q = matrix.questions(scenario)[idx]
    rows = [(o["label"], f"a:{idx}:{i}") for i, o in enumerate(q["options"])]
    await msg.answer(f"<b>Вопрос {idx + 1}/{len(matrix.questions(scenario))}.</b> {q['text']}", reply_markup=kb(rows))


@router.callback_query(F.data.startswith("sc:"))
async def on_scenario(c: CallbackQuery, state: FSMContext):
    scenario = c.data.split(":")[1]
    await state.set_data({"scenario": scenario, "answers": {}, "photos": [], "lat": None, "lon": None, "taken_at": None})
    await state.set_state(Flow.questions)
    await _ask_question(c.message, scenario, 0)
    await c.answer()


@router.callback_query(Flow.questions, F.data.startswith("a:"))
async def on_answer(c: CallbackQuery, state: FSMContext):
    _, qi, oi = c.data.split(":")
    qi, oi = int(qi), int(oi)
    data = await state.get_data()
    scenario = data["scenario"]
    qs = matrix.questions(scenario)
    answers = data["answers"]
    answers[qs[qi]["id"]] = qs[qi]["options"][oi]["value"]
    await state.update_data(answers=answers)
    await c.answer()
    if qi + 1 < len(qs):
        await _ask_question(c.message, scenario, qi + 1)
    else:
        await state.set_state(Flow.photos)
        await c.message.answer(
            "📸 Пришлите фото нарушения (до %d шт.).\n\n"
            "<b>Важно:</b> отправляйте снимок <b>как файл</b> (скрепка → «Файл»/«Документ»). "
            "Обычное фото Telegram сжимает и стирает GPS-координаты. "
            "Если координат в снимке нет, я попрошу геопозицию отдельно." % config.MAX_PHOTOS
        )


# ---------- фото и геопозиция ----------

def _user_dir(chat_id: int) -> Path:
    return config.UPLOAD_DIR / str(chat_id)


async def _cleanup(chat_id: int):
    shutil.rmtree(_user_dir(chat_id), ignore_errors=True)


async def _save_image(m: Message, state: FSMContext, file_id: str, ext: str):
    data = await state.get_data()
    photos: list[str] = data["photos"]
    if len(photos) >= config.MAX_PHOTOS:
        await m.answer(f"Максимум {config.MAX_PHOTOS} фото. Нажмите «Готово».")
        return
    d = _user_dir(m.chat.id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{len(photos) + 1}{ext}"
    await m.bot.download(file_id, destination=path)
    photos.append(str(path))
    meta = await asyncio.to_thread(read_photo_meta, path)
    upd: dict = {"photos": photos}
    note = "GPS в снимке не найден"
    if meta.lat is not None and data["lat"] is None:
        if in_region(meta.lat, meta.lon):
            upd.update(lat=meta.lat, lon=meta.lon)
            note = f"GPS найден: {meta.lat:.5f}, {meta.lon:.5f} ✅"
        else:
            note = "GPS найден, но вне Калининградской области ⚠️ — пришлите геопозицию вручную"
    elif data["lat"] is not None:
        note = "координаты уже есть ✅"
    if meta.taken_at and not data["taken_at"]:
        upd["taken_at"] = meta.taken_at.isoformat()
    await state.update_data(**upd)
    await m.answer(f"Фото {len(photos)} принято. {note}.", reply_markup=kb([("Готово →", "photos_done")]))


@router.message(Flow.photos, F.photo)
async def on_photo(m: Message, state: FSMContext):
    await _save_image(m, state, m.photo[-1].file_id, ".jpg")


@router.message(Flow.photos, F.document)
async def on_doc(m: Message, state: FSMContext):
    if not (m.document.mime_type or "").startswith("image/"):
        return await m.answer("Нужен файл-изображение (JPG/PNG/HEIC→JPG).")
    ext = Path(m.document.file_name or "x.jpg").suffix.lower() or ".jpg"
    await _save_image(m, state, m.document.file_id, ext)


@router.callback_query(Flow.photos, F.data == "photos_done")
async def on_photos_done(c: CallbackQuery, state: FSMContext):
    await c.answer()
    data = await state.get_data()
    if data["lat"] is not None:
        return await _to_description(c.message, state)
    await state.set_state(Flow.location)
    await c.message.answer(
        "📍 Пришлите место нарушения: нажмите кнопку ниже (если вы на месте) "
        "или скрепка → «Геопозиция» и выберите точку на карте.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить мою геопозицию", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True),
    )


async def _store_location(m: Message, state: FSMContext) -> bool:
    lat, lon = m.location.latitude, m.location.longitude
    if not in_region(lat, lon):
        await m.answer("Эта точка вне Калининградской области. Пока работаю только по региону: пришлите другую точку.")
        return False
    await state.update_data(lat=lat, lon=lon)
    await m.answer("Геопозиция получена ✅", reply_markup=ReplyKeyboardRemove())
    return True


@router.message(F.location, Flow.photos)
async def on_location_early(m: Message, state: FSMContext):
    if await _store_location(m, state):
        await m.answer("Теперь пришлите фото (или нажмите «Готово», если уже отправили).")


@router.message(F.location, Flow.location)
async def on_location(m: Message, state: FSMContext):
    if await _store_location(m, state):
        await _to_description(m, state)


async def _to_description(msg: Message, state: FSMContext):
    await state.set_state(Flow.description)
    await msg.answer(
        "✍️ Опишите ситуацию своими словами (что, с какого времени, есть ли техника, номера машин). "
        "Или пропустите.", reply_markup=kb([("Пропустить", "skip_desc")]))


# ---------- описание и данные заявителя ----------

@router.callback_query(Flow.description, F.data == "skip_desc")
async def skip_desc(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await _ask_name(c.message, state)


@router.message(Flow.description, F.text)
async def on_desc(m: Message, state: FSMContext):
    await state.update_data(description=m.text.strip()[:1500])
    await _ask_name(m, state)


async def _ask_name(msg: Message, state: FSMContext):
    await state.set_state(Flow.name)
    await msg.answer("Ваши <b>ФИО</b> полностью (по закону обращение должно быть подписано заявителем):")


@router.message(Flow.name, F.text)
async def on_name(m: Message, state: FSMContext):
    if len(m.text.split()) < 2:
        return await m.answer("Пожалуйста, укажите фамилию и имя (отчество при наличии).")
    await state.update_data(name=m.text.strip()[:200])
    await state.set_state(Flow.address)
    await m.answer("<b>Адрес для ответа</b>: почтовый адрес (с индексом) или e-mail:")


@router.message(Flow.address, F.text)
async def on_address(m: Message, state: FSMContext):
    await state.update_data(address=m.text.strip()[:300])
    await _show_summary(m, state)


# ---------- сводка и PDF ----------

def _build_context(data: dict):
    q = matrix.qualify(data["scenario"], data["answers"])
    adr = resolve(q, data["lat"], data["lon"])
    return q, adr


async def _show_summary(msg: Message, state: FSMContext):
    data = await state.get_data()
    q, adr = _build_context(data)
    city, km = nearest_center(data["lat"], data["lon"])
    lines = [
        f"<b>Проверьте данные</b>\n",
        f"📌 Нарушение: {q.violation_label}",
        f"🗺 <a href=\"{yandex_map_url(data['lat'], data['lon'])}\">Место на карте</a> (ближайший центр: {city}, ~{km:.0f} км)",
        "\n⚖️ <b>Применимые нормы:</b>",
        *[f"• {n.act}{', ' + n.article if n.article else ''}" for n in q.norms],
        "\n🏛 <b>Куда направить:</b>",
        *[f"{i}. {a.short}" for i, a in enumerate(adr, 1)],
    ]
    if q.flags:
        lines += ["", *q.flags]
    if q.needs_legal_check:
        lines += ["", "ℹ️ Часть норм подобрана предварительно; перед отправкой желательно проверить текст."]
    await state.set_state(Flow.confirm)
    await msg.answer("\n".join(lines), disable_web_page_preview=True,
                     reply_markup=kb([("📄 Сформировать PDF", "make"), ("↩️ Начать заново", "restart")]))


@router.callback_query(Flow.confirm, F.data == "restart")
async def on_restart(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await _cleanup(c.message.chat.id)
    await state.clear()
    await c.message.answer("Начнём заново:", reply_markup=scenario_kb())


@router.callback_query(Flow.confirm, F.data == "make")
async def on_make(c: CallbackQuery, state: FSMContext):
    await c.answer("Готовлю документ…")
    data = await state.get_data()
    q, adr = _build_context(data)
    city, _ = nearest_center(data["lat"], data["lon"])
    taken = datetime.fromisoformat(data["taken_at"]) if data.get("taken_at") else datetime.now()
    cd = ComplaintData(
        qualification=q, addressees=adr, lat=data["lat"], lon=data["lon"],
        applicant_name=data["name"], applicant_address=data["address"],
        description=data.get("description", ""), observed_at=taken,
        photo_paths=[Path(p) for p in data["photos"]], place_hint=f"вблизи г. {city}",
    )
    pdf_bytes = await asyncio.to_thread(build_complaint_pdf, cd)
    db.add_report(scenario=q.scenario, lat=cd.lat, lon=cd.lon, district=city, tags=q.tags,
                  norms=[n.id for n in q.norms], agencies=q.agency_ids)
    await c.message.answer_document(
        BufferedInputFile(pdf_bytes, filename="GreenClaim_obrashchenie.pdf"),
        caption="Готово! Ваше обращение 👆")
    await c.message.answer(
        "<b>Как отправить:</b>\n"
        "1. Распечатайте и подпишите (или подпишите электронно).\n"
        "2. Подайте через «Платформу обратной связи» на Госуслугах, на сайт или e-mail ведомства, либо почтой.\n"
        "3. Отправьте этот же файл каждому ведомству из списка.\n"
        "4. Ответ обязаны дать в течение 30 дней со дня регистрации (59-ФЗ).\n\n"
        "Спасибо, что заботитесь об окружающей среде 🌱  Новое обращение — /start")
    await _cleanup(c.message.chat.id)
    await state.clear()


# ---------- запуск ----------

async def main():
    logging.basicConfig(level=logging.INFO)
    if not config.BOT_TOKEN:
        raise SystemExit("Задайте BOT_TOKEN в окружении или .env")
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
