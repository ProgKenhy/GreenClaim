"""GreenClaim в мессенджере MAX (библиотека maxapi, polling).

Запуск:  python -m greenclaim.max_bot
Токен:   MAX_BOT_TOKEN (платформа «MAX для партнёров» → Чат-боты → Интеграция → Получить токен)

Адаптер тонкий: вся логика диалога в greenclaim/dialog.py.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from maxapi import Bot, Dispatcher
from maxapi.enums.format import Format
from maxapi.types import (BotStarted, CallbackButton, Command, MessageCallback,
                          MessageCreated, RequestGeoLocationButton)
from maxapi.types.input_media import InputMediaBuffer
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from . import config
from .dialog import Button, Dialog, Reply

log = logging.getLogger("greenclaim.max")

dialog = Dialog()
dp = Dispatcher()
_bot: Bot | None = None

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff", ".bmp"}
_MAX_DOWNLOAD = 25 * 1024 * 1024  # защита памяти: фото больше 25 МБ не читаем


# ---------- безопасное извлечение полей события ----------

def _dig(obj: Any, *paths: str) -> Any:
    """Первый непустой атрибут из путей вида 'message.recipient.chat_id'."""
    for path in paths:
        cur = obj
        for part in path.split("."):
            cur = getattr(cur, part, None)
            if cur is None:
                break
        if cur is not None:
            return cur
    return None


def _sid(event: Any) -> str:
    cid = _dig(event, "chat_id", "message.recipient.chat_id", "message.recipient.user_id")
    if cid is None:
        raise ValueError("Не удалось определить chat_id события")
    return str(cid)


def _atype(att: Any) -> str:
    t = getattr(att, "type", None)
    return str(getattr(t, "value", t) or "").lower()


# ---------- отправка ----------

def _keyboard(rows: list[list[Button]]):
    b = InlineKeyboardBuilder()
    for row in rows:
        btns = [RequestGeoLocationButton(text=x.text) if x.request_geo
                else CallbackButton(text=x.text, payload=x.payload) for x in row]
        b.row(*btns)
    return b.as_markup()


Sender = Callable[..., Awaitable[Any]]


async def _send_with_retry(send: Sender, **kw: Any) -> None:
    """Файл после загрузки обрабатывается на стороне MAX; пока он не готов,
    API отвечает ошибкой attachment.not.ready — повторяем с паузой."""
    for attempt in range(6):
        try:
            await send(**kw)
            return
        except Exception as exc:  # noqa: BLE001
            if "not.ready" in str(exc).lower() and attempt < 5:
                await asyncio.sleep(1.0 + attempt)
                continue
            raise


async def deliver(send: Sender, replies: list[Reply]) -> None:
    for r in replies:
        atts: list[Any] = []
        if r.document is not None:
            media = InputMediaBuffer(r.document.data, filename=r.document.filename, type="file")
            atts.append(await _bot.upload_media(media))
        if r.buttons:
            atts.append(_keyboard(r.buttons))
        kw: dict[str, Any] = {"text": r.text or "\u200b", "format": Format.HTML}
        if atts:
            kw["attachments"] = atts
        await _send_with_retry(send, **kw)


async def _download(url: str) -> bytes | None:
    async with aiohttp.ClientSession() as http:
        async with http.get(url) as resp:
            if resp.status != 200:
                log.warning("download %s -> HTTP %s", url.split("?")[0], resp.status)
                return None
            if (resp.content_length or 0) > _MAX_DOWNLOAD:
                return None
            return await resp.read()


# ---------- обработчики ----------

@dp.bot_started()
async def on_bot_started(event: BotStarted):
    sid = _sid(event)
    replies = await asyncio.to_thread(dialog.start, sid)
    await deliver(lambda **kw: event.bot.send_message(chat_id=event.chat_id, **kw), replies)


@dp.message_created(Command("start"))
async def on_start(event: MessageCreated):
    await deliver(event.message.answer, await asyncio.to_thread(dialog.start, _sid(event)))


@dp.message_created(Command("cancel"))
async def on_cancel(event: MessageCreated):
    await deliver(event.message.answer, await asyncio.to_thread(dialog.cancel, _sid(event)))


@dp.message_callback()
async def on_callback(event: MessageCallback):
    payload = _dig(event, "callback.payload") or ""
    replies = await asyncio.to_thread(dialog.on_callback, _sid(event), str(payload))
    await deliver(event.message.answer, replies)


@dp.message_created()
async def on_message(event: MessageCreated):
    sid = _sid(event)
    body = event.message.body
    atts = list(getattr(body, "attachments", None) or [])
    replies: list[Reply] = []

    handled = False
    for att in atts:
        kind = _atype(att)
        if kind == "location":
            lat, lon = getattr(att, "latitude", None), getattr(att, "longitude", None)
            if lat is not None and lon is not None:
                replies += await asyncio.to_thread(dialog.on_location, sid, float(lat), float(lon))
                handled = True
        elif kind in ("image", "file"):
            name = _dig(att, "filename", "payload.filename") or ""
            ext = Path(str(name)).suffix.lower() or ".jpg"
            if kind == "file" and ext not in _IMAGE_EXT:
                replies.append(Reply("Нужен файл-изображение (JPG, PNG или HEIC)."))
                handled = True
                continue
            url = _dig(att, "payload.url")
            data = await _download(str(url)) if url else None
            if data is None:
                replies.append(Reply("Не удалось получить файл. Попробуйте отправить ещё раз."))
            else:
                replies += await asyncio.to_thread(dialog.on_image, sid, data, ext)
            handled = True

    if not handled:
        text = (getattr(body, "text", None) or "").strip()
        if text and not text.startswith("/"):
            replies += await asyncio.to_thread(dialog.on_text, sid, text)

    if replies:
        await deliver(event.message.answer, replies)


# ---------- запуск ----------

async def main() -> None:
    global _bot
    logging.basicConfig(level=logging.INFO)
    if not config.MAX_BOT_TOKEN:
        raise SystemExit("Задайте MAX_BOT_TOKEN в окружении или .env")
    _bot = Bot(config.MAX_BOT_TOKEN)
    await dp.start_polling(_bot)


if __name__ == "__main__":
    asyncio.run(main())
