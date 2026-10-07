"""Платформо-независимый диалоговый движок GreenClaim.

Движок ничего не знает о мессенджерах: принимает события (текст, нажатие кнопки,
изображение, геопозиция) и возвращает список `Reply` (текст + кнопки + файл).
Адаптеры (max_bot.py, при желании и Telegram) только переводят события туда-сюда.
Это позволяет тестировать весь сценарий без сети.

Текст ответов — простой HTML (<b>, <a href>), его понимают MAX и Telegram.
Данные заявителя (ФИО, адрес) живут только в Session в памяти.
"""
from __future__ import annotations

import html
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import config
from .geo import in_region, nearest_center, read_photo_meta, yandex_map_url
from .legal import LegalMatrix
from .pdf import ComplaintData, build_complaint_pdf
from .routing import resolve
from .storage import Storage


# ---------- модель ответа ----------

@dataclass(frozen=True)
class Button:
    text: str
    payload: str = ""
    request_geo: bool = False  # кнопка «отправить геопозицию» (если платформа умеет)


@dataclass
class Document:
    data: bytes
    filename: str


@dataclass
class Reply:
    text: str = ""
    buttons: list[list[Button]] = field(default_factory=list)
    document: Document | None = None


def _kb(*rows: list[Button]) -> list[list[Button]]:
    return [list(r) for r in rows]


def _one(text: str, payload: str) -> list[list[Button]]:
    return [[Button(text, payload)]]


# ---------- состояние сессии ----------

(IDLE, QUESTIONS, PHOTOS, LOCATION, DESCRIPTION, NAME, ADDRESS, CONFIRM) = (
    "idle", "questions", "photos", "location", "description", "name", "address", "confirm")


@dataclass
class Session:
    state: str = IDLE
    scenario: str = ""
    qidx: int = 0
    answers: dict[str, str] = field(default_factory=dict)
    photos: list[str] = field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    taken_at: datetime | None = None
    description: str = ""
    name: str = ""
    address: str = ""


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

PHOTO_PROMPT = (
    "📸 Пришлите фото нарушения (до {n} шт.).\n\n"
    "<b>Важно:</b> отправляйте снимок <b>как файл</b> (скрепка → «Файл»). "
    "Обычное фото мессенджер сжимает и может стереть GPS-координаты. "
    "Если координат в снимке нет, я попрошу геопозицию отдельно."
)

HOWTO = (
    "<b>Как отправить:</b>\n"
    "1. Распечатайте и подпишите (или подпишите электронно).\n"
    "2. Подайте через «Платформу обратной связи» на Госуслугах, на сайт или e-mail ведомства, либо почтой.\n"
    "3. Отправьте этот же файл каждому ведомству из списка.\n"
    "4. Ответ обязаны дать в течение 30 дней со дня регистрации (59-ФЗ).\n\n"
    "Спасибо, что заботитесь об окружающей среде 🌱  Новое обращение — /start"
)


class Dialog:
    def __init__(self, matrix: LegalMatrix | None = None, db: Storage | None = None,
                 upload_dir: Path | None = None):
        self.matrix = matrix or LegalMatrix()
        self.db = db or Storage(config.DB_PATH)
        self.upload_dir = Path(upload_dir or config.UPLOAD_DIR)
        self.sessions: dict[str, Session] = {}

    # ----- служебное -----
    def _s(self, sid: str) -> Session:
        return self.sessions.setdefault(sid, Session())

    def _user_dir(self, sid: str) -> Path:
        return self.upload_dir / "".join(c for c in str(sid) if c.isalnum() or c in "-_")

    def reset(self, sid: str) -> None:
        shutil.rmtree(self._user_dir(sid), ignore_errors=True)
        self.sessions.pop(sid, None)

    # ----- входы -----
    def start(self, sid: str) -> list[Reply]:
        self.reset(sid)
        return [Reply(WELCOME, _one("✅ Согласен(на), продолжить", "consent"))]

    def cancel(self, sid: str) -> list[Reply]:
        self.reset(sid)
        return [Reply("Отменено. Чтобы начать заново, отправьте /start")]

    def on_callback(self, sid: str, payload: str) -> list[Reply]:
        s = self._s(sid)
        if payload == "consent":
            return [self._scenario_menu()]
        if payload.startswith("sc:"):
            key = payload[3:]
            if key not in self.matrix.scenarios:
                return []
            self.reset(sid)
            s = self._s(sid)
            s.scenario, s.state, s.qidx = key, QUESTIONS, 0
            return [self._question(s)]
        if payload.startswith("a:") and s.state == QUESTIONS:
            return self._answer(s, payload)
        if payload == "photos_done" and s.state == PHOTOS:
            return self._photos_done(s)
        if payload == "skip_desc" and s.state == DESCRIPTION:
            return self._ask_name(s)
        if payload == "restart" and s.state == CONFIRM:
            self.reset(sid)
            return [self._scenario_menu()]
        if payload == "make" and s.state == CONFIRM:
            return self._make(sid, s)
        return []  # устаревшая/повторная кнопка — молча игнорируем

    def on_text(self, sid: str, text: str) -> list[Reply]:
        s = self._s(sid)
        text = (text or "").strip()
        if not text:
            return []
        if s.state == DESCRIPTION:
            s.description = text[:1500]
            return self._ask_name(s)
        if s.state == NAME:
            if len(text.split()) < 2:
                return [Reply("Пожалуйста, укажите фамилию и имя (отчество при наличии).")]
            s.name, s.state = text[:200], ADDRESS
            return [Reply("<b>Адрес для ответа</b>: почтовый адрес (с индексом) или e-mail:")]
        if s.state == ADDRESS:
            s.address = text[:300]
            return self._summary(s)
        if s.state == IDLE:
            return [Reply("Нажмите /start, чтобы подготовить обращение.")]
        return []

    def on_image(self, sid: str, data: bytes, ext: str = ".jpg") -> list[Reply]:
        s = self._s(sid)
        if s.state != PHOTOS:
            return []
        if len(s.photos) >= config.MAX_PHOTOS:
            return [Reply(f"Максимум {config.MAX_PHOTOS} фото. Нажмите «Готово».", _one("Готово →", "photos_done"))]
        ext = ext.lower() if ext.lower() in {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff", ".bmp"} else ".jpg"
        d = self._user_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{len(s.photos) + 1}{ext}"
        path.write_bytes(data)
        try:
            with Image.open(path) as im:
                im.verify()
        except Exception:
            path.unlink(missing_ok=True)
            return [Reply("Не удалось прочитать изображение. Пришлите JPG, PNG или HEIC (лучше как файл).")]
        s.photos.append(str(path))
        meta = read_photo_meta(path)
        note = "GPS в снимке не найден"
        if meta.lat is not None and s.lat is None:
            if in_region(meta.lat, meta.lon):
                s.lat, s.lon = meta.lat, meta.lon
                note = f"GPS найден: {meta.lat:.5f}, {meta.lon:.5f} ✅"
            else:
                note = "GPS найден, но вне Калининградской области ⚠️ — пришлите геопозицию вручную"
        elif s.lat is not None:
            note = "координаты уже есть ✅"
        if meta.taken_at and not s.taken_at:
            s.taken_at = meta.taken_at
        rows = [[Button("Готово →", "photos_done")]]
        if s.lat is None:
            rows.append([Button("📍 Отправить геопозицию", request_geo=True)])
        return [Reply(f"Фото {len(s.photos)} принято. {note}.", rows)]

    def on_location(self, sid: str, lat: float, lon: float) -> list[Reply]:
        s = self._s(sid)
        if s.state not in (PHOTOS, LOCATION):
            return []
        if not in_region(lat, lon):
            return [Reply("Эта точка вне Калининградской области. Пока работаю только по региону: пришлите другую точку.")]
        s.lat, s.lon = lat, lon
        if s.state == LOCATION:
            return [Reply("Геопозиция получена ✅")] + self._ask_description(s)
        return [Reply("Геопозиция получена ✅ Пришлите фото или нажмите «Готово».", _one("Готово →", "photos_done"))]

    # ----- шаги -----
    def _scenario_menu(self) -> Reply:
        return Reply("Что случилось?", [[Button(f"{v['emoji']} {v['title']}", f"sc:{k}")]
                                         for k, v in self.matrix.scenarios.items()])

    def _question(self, s: Session) -> Reply:
        qs = self.matrix.questions(s.scenario)
        q = qs[s.qidx]
        rows = [[Button(o["label"], f"a:{s.qidx}:{i}")] for i, o in enumerate(q["options"])]
        return Reply(f"<b>Вопрос {s.qidx + 1}/{len(qs)}.</b> {q['text']}", rows)

    def _answer(self, s: Session, payload: str) -> list[Reply]:
        try:
            _, qi, oi = payload.split(":")
            qi, oi = int(qi), int(oi)
            qs = self.matrix.questions(s.scenario)
            opt = qs[qi]["options"][oi]
        except (ValueError, IndexError):
            return []
        if qi != s.qidx:  # двойной тап / старая кнопка
            return []
        s.answers[qs[qi]["id"]] = opt["value"]
        if qi + 1 < len(qs):
            s.qidx += 1
            return [self._question(s)]
        s.state = PHOTOS
        return [Reply(PHOTO_PROMPT.format(n=config.MAX_PHOTOS))]

    def _photos_done(self, s: Session) -> list[Reply]:
        if not s.photos:
            return [Reply("Нужно хотя бы одно фото. Пришлите снимок (лучше как файл).")]
        if s.lat is not None:
            return self._ask_description(s)
        s.state = LOCATION
        return [Reply(
            "📍 Пришлите место нарушения: нажмите кнопку ниже (если вы на месте) "
            "или отправьте геопозицию через скрепку и выберите точку на карте.",
            [[Button("📍 Отправить геопозицию", request_geo=True)]])]

    def _ask_description(self, s: Session) -> list[Reply]:
        s.state = DESCRIPTION
        return [Reply("✍️ Опишите ситуацию своими словами (что, с какого времени, есть ли техника, "
                      "номера машин). Или пропустите.", _one("Пропустить", "skip_desc"))]

    def _ask_name(self, s: Session) -> list[Reply]:
        s.state = NAME
        return [Reply("Ваши <b>ФИО</b> полностью (по закону обращение должно быть подписано заявителем):")]

    def _context(self, s: Session):
        q = self.matrix.qualify(s.scenario, s.answers)
        return q, resolve(q, s.lat, s.lon)

    def _summary(self, s: Session) -> list[Reply]:
        q, adr = self._context(s)
        city, km = nearest_center(s.lat, s.lon)
        e = html.escape
        lines = [
            "<b>Проверьте данные</b>\n",
            f"📌 Нарушение: {e(q.violation_label)}",
            f'🗺 <a href="{yandex_map_url(s.lat, s.lon)}">Место на карте</a> (ближайший центр: {e(city)}, ~{km:.0f} км)',
            "\n⚖️ <b>Применимые нормы:</b>",
            *[f"• {e(n.act)}{', ' + e(n.article) if n.article else ''}" for n in q.norms],
            "\n🏛 <b>Куда направить:</b>",
            *[f"{i}. {e(a.short)}" for i, a in enumerate(adr, 1)],
        ]
        if q.flags:
            lines += ["", *[e(f) for f in q.flags]]
        if q.needs_legal_check:
            lines += ["", "ℹ️ Часть норм подобрана предварительно; перед отправкой желательно проверить текст."]
        s.state = CONFIRM
        return [Reply("\n".join(lines), [[Button("📄 Сформировать PDF", "make")],
                                         [Button("↩️ Начать заново", "restart")]])]

    def _make(self, sid: str, s: Session) -> list[Reply]:
        q, adr = self._context(s)
        city, _ = nearest_center(s.lat, s.lon)
        cd = ComplaintData(
            qualification=q, addressees=adr, lat=s.lat, lon=s.lon,
            applicant_name=s.name, applicant_address=s.address, description=s.description,
            observed_at=s.taken_at or datetime.now(), photo_paths=[Path(p) for p in s.photos],
            place_hint=f"вблизи г. {city}",
        )
        pdf = build_complaint_pdf(cd)
        self.db.add_report(scenario=q.scenario, lat=s.lat, lon=s.lon, district=city, tags=q.tags,
                           norms=[n.id for n in q.norms], agencies=q.agency_ids)
        self.reset(sid)  # удаляем фото и персональные данные
        return [Reply("Готово! Ваше обращение 👇", document=Document(pdf, "GreenClaim_obrashchenie.pdf")),
                Reply(HOWTO)]
