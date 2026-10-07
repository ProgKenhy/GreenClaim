import io
from pathlib import Path

import pytest
from PIL import Image

from greenclaim.dialog import Dialog
from tests.test_core import make_photo

SID = "42"


def jpeg_bytes(tmp_path, with_gps=True) -> bytes:
    return make_photo(tmp_path / "x.jpg", with_gps=with_gps).read_bytes()


def texts(replies):
    return "\n".join(r.text for r in replies)


def payloads(replies):
    return [b.payload for r in replies for row in r.buttons for b in row if b.payload]


def new_dialog(tmp_path):
    return Dialog(db=__import__("greenclaim.storage", fromlist=["Storage"]).Storage(tmp_path / "d.db"),
                  upload_dir=tmp_path / "up")


def run_to_photos(d, scenario="dump", answers=(0, 0, 0)):
    d.start(SID)
    d.on_callback(SID, "consent")
    rep = d.on_callback(SID, f"sc:{scenario}")
    for i, a in enumerate(answers):
        rep = d.on_callback(SID, f"a:{i}:{a}")
    return rep


def test_full_flow_with_gps_in_photo(tmp_path):
    d = new_dialog(tmp_path)
    start = d.start(SID)
    assert "consent" in payloads(start)
    menu = d.on_callback(SID, "consent")
    assert {"sc:dump", "sc:logging"} <= set(payloads(menu))
    rep = run_to_photos(d)
    assert "как файл" in texts(rep)

    rep = d.on_image(SID, jpeg_bytes(tmp_path), ".jpg")
    assert "GPS найден" in texts(rep) and "photos_done" in payloads(rep)
    assert not any(b.request_geo for r in rep for row in r.buttons for b in row)  # гео уже есть

    rep = d.on_callback(SID, "photos_done")
    assert "Опишите" in texts(rep) and "skip_desc" in payloads(rep)
    rep = d.on_text(SID, "Свалка растёт вторую неделю")
    assert "ФИО" in texts(rep)
    assert "фамилию" in texts(d.on_text(SID, "Иванова"))        # валидация ФИО
    rep = d.on_text(SID, "Иванова Мария Петровна")
    assert "Адрес" in texts(rep)
    rep = d.on_text(SID, "236000, г. Калининград, ул. Тестовая, 1")
    t = texts(rep)
    assert "Применимые нормы" in t and "КоАП РФ, ст. 8.2" in t and "Росприроднадзор" in t
    assert "make" in payloads(rep)

    rep = d.on_callback(SID, "make")
    doc = rep[0].document
    assert doc and doc.data.startswith(b"%PDF") and doc.filename.endswith(".pdf")
    assert "Как отправить" in texts(rep)

    # приватность: сессия и фото удалены, в БД обезличенная запись
    assert SID not in d.sessions
    assert not (tmp_path / "up" / SID).exists()
    assert d.db.count() == 1


def test_flow_without_gps_uses_location(tmp_path):
    d = new_dialog(tmp_path)
    run_to_photos(d, "logging", (0, 0, 1))
    rep = d.on_image(SID, jpeg_bytes(tmp_path, with_gps=False), ".jpg")
    assert "GPS в снимке не найден" in texts(rep)
    assert any(b.request_geo for r in rep for row in r.buttons for b in row)

    rep = d.on_callback(SID, "photos_done")           # без координат -> просим геопозицию
    assert "геопозицию" in texts(rep).lower()
    rep = d.on_location(SID, 55.75, 37.62)            # Москва — вне региона
    assert "вне Калининградской" in texts(rep)
    rep = d.on_location(SID, 54.71, 20.45)
    assert "Опишите" in texts(rep)

    d.on_callback(SID, "skip_desc")
    d.on_text(SID, "Петров Пётр")
    rep = d.on_text(SID, "petrov@example.com")
    assert "Лесной кодекс" in texts(rep) and "ст. 260" in texts(rep)      # лес + много деревьев
    assert any("112" in r.text for r in rep)                              # флаг про полицию


def test_stale_and_wrong_state_events_ignored(tmp_path):
    d = new_dialog(tmp_path)
    assert d.on_callback(SID, "make") == []
    assert d.on_image(SID, b"x") == [] and d.on_location(SID, 54.7, 20.5) == []
    run_to_photos(d, answers=(0,))                    # остановились на вопросе 2
    assert d.on_callback(SID, "a:0:1") == []          # повторное нажатие на уже отвеченный вопрос
    assert d.on_callback(SID, "a:1:99") == []         # несуществующий вариант
    assert d.on_callback(SID, "a:zzz") == []
    assert d.on_callback(SID, "sc:nonexistent") == []
    assert "/start" in texts(d.on_text("other", "привет"))


def test_bad_image_rejected(tmp_path):
    d = new_dialog(tmp_path)
    run_to_photos(d)
    rep = d.on_image(SID, b"definitely not an image", ".jpg")
    assert "Не удалось прочитать" in texts(rep)
    assert d.sessions[SID].photos == []
    assert "хотя бы одно фото" in texts(d.on_callback(SID, "photos_done"))


def test_photo_limit_and_path_traversal(tmp_path):
    d = new_dialog(tmp_path)
    run_to_photos(d)
    data = jpeg_bytes(tmp_path)
    for _ in range(3):
        d.on_image(SID, data, ".jpg")
    assert "Максимум" in texts(d.on_image(SID, data, ".jpg"))
    # расширение из имени файла не должно давать выход из папки
    d2 = new_dialog(tmp_path)
    d2.start("../evil")
    assert d2._user_dir("../evil").parent == tmp_path / "up"


def test_restart_and_cancel(tmp_path):
    d = new_dialog(tmp_path)
    run_to_photos(d)
    d.on_image(SID, jpeg_bytes(tmp_path), ".jpg")
    d.on_callback(SID, "photos_done"); d.on_callback(SID, "skip_desc")
    d.on_text(SID, "Иванов Иван"); d.on_text(SID, "a@b.ru")
    assert "sc:dump" in payloads(d.on_callback(SID, "restart"))
    assert SID not in d.sessions
    assert "Отменено" in texts(d.cancel(SID))
