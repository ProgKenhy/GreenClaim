import io
import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from greenclaim.geo import in_region, nearest_center, read_photo_meta
from greenclaim.legal import LegalMatrix
from greenclaim.pdf import ComplaintData, build_complaint_pdf
from greenclaim.routing import resolve
from greenclaim.storage import Storage

M = LegalMatrix()


def make_photo(path: Path, with_gps=True):
    img = Image.new("RGB", (1600, 1200), (60, 120, 60))
    exif = Image.Exif()
    if with_gps:
        exif[0x8825] = {1: "N", 2: (54.0, 46.0, 12.0), 3: "E", 4: (20.0, 36.0, 14.4)}
    exif.get_ifd(0x8769)[0x9003] = "2026:09:20 14:35:10"
    img.save(path, "JPEG", exif=exif)
    return path


# ---- юридическая матрица ----
def test_dump_forest_hazard_unknown_offender():
    q = M.qualify("dump", {"land": "forest", "waste": "hazard", "offender": "unknown"})
    ids = {n.id for n in q.norms}
    assert {"koap_8_2", "koap_8_31", "uk_247"} <= ids
    assert q.agency_ids[0] == "rpn" and q.agency_ids[-1] == "prosecutor"
    assert "forest" in q.agency_ids and "rpn_health" in q.agency_ids
    assert any("112" in f for f in q.flags)
    assert any("установить лицо" in r for r in q.requests)


def test_dump_water_adds_water_code():
    q = M.qualify("dump", {"land": "water", "waste": "household", "offender": "unknown"})
    assert {"koap_8_13", "vk_65"} <= {n.id for n in q.norms}


def test_logging_forest_large_has_criminal_flag():
    q = M.qualify("logging", {"land": "forest", "permit": "none", "scale": "many"})
    assert {"koap_8_28", "uk_260"} <= {n.id for n in q.norms}
    assert q.agency_ids[0] == "forest" and q.agency_ids[-1] == "prosecutor"


def test_logging_forest_small_no_criminal():
    q = M.qualify("logging", {"land": "forest", "permit": "none", "scale": "few"})
    assert "uk_260" not in {n.id for n in q.norms}


def test_logging_city_goes_to_municipality():
    q = M.qualify("logging", {"land": "city", "permit": "none", "scale": "few"})
    assert "municipality" in q.agency_ids and "koap_8_28" not in {n.id for n in q.norms}


def test_no_duplicates_and_every_norm_exists():
    for r in M.rules:
        for n in r.get("norms", []):
            assert n in M.norms
    q = M.qualify("dump", {"land": "unknown", "waste": "mixed", "offender": "person"})
    assert len(q.norms) == len({n.id for n in q.norms})


def test_every_option_combination_qualifies():
    import itertools
    for sc in M.scenarios:
        qs = M.questions(sc)
        for combo in itertools.product(*[[o["value"] for o in q["options"]] for q in qs]):
            res = M.qualify(sc, {q["id"]: v for q, v in zip(qs, combo)})
            assert res.norms and res.agency_ids and res.requests


# ---- гео ----
def test_exif_gps_roundtrip(tmp_path):
    meta = read_photo_meta(make_photo(tmp_path / "a.jpg"))
    assert meta.lat == pytest.approx(54.77, abs=1e-3)
    assert meta.lon == pytest.approx(20.6040, abs=1e-3)
    assert meta.taken_at == datetime(2026, 9, 20, 14, 35, 10)


def test_no_gps_and_garbage(tmp_path):
    assert read_photo_meta(make_photo(tmp_path / "b.jpg", with_gps=False)).lat is None
    bad = tmp_path / "c.jpg"
    bad.write_bytes(b"not an image")
    assert read_photo_meta(bad).lat is None


def test_region_and_nearest():
    assert in_region(54.71, 20.45) and not in_region(55.75, 37.62)
    city, km = nearest_center(54.77, 20.60)
    assert city == "Гурьевск" and km < 3


# ---- PDF ----
@pytest.fixture()
def complaint(tmp_path):
    photo = make_photo(tmp_path / "p.jpg")
    q = M.qualify("dump", {"land": "forest", "waste": "hazard", "offender": "unknown"})
    return ComplaintData(
        qualification=q, addressees=resolve(q, 54.77, 20.60), lat=54.77, lon=20.60,
        applicant_name="Иванова Мария Петровна", applicant_address="236000, г. Калининград, ул. Тестовая, 1",
        description="Свалка растёт вторую неделю, ездят грузовики.", observed_at=datetime(2026, 9, 20, 14, 35),
        photo_paths=[photo, photo], place_hint="вблизи г. Гурьевск",
    )


def test_pdf_is_fast_and_valid(complaint):
    build_complaint_pdf(complaint)  # прогрев шрифтов
    t = time.perf_counter()
    pdf = build_complaint_pdf(complaint)
    dt = time.perf_counter() - t
    assert pdf.startswith(b"%PDF") and len(pdf) > 5_000
    assert dt < 3.0, f"PDF за {dt:.2f}с (цель паспорта проекта: ≤3 с)"


def test_pdf_text_content(complaint, tmp_path):
    out = tmp_path / "o.pdf"
    out.write_bytes(build_complaint_pdf(complaint))
    import subprocess
    txt = subprocess.run(["pdftotext", "-layout", str(out), "-"], capture_output=True, text=True).stdout
    for needle in ("ст. 8.2", "Иванова Мария Петровна", "59-ФЗ", "54.770000", "Росприроднадзора"):
        assert needle in txt, needle


# ---- хранилище ----
def test_storage_no_personal_data(tmp_path):
    db = Storage(tmp_path / "t.db")
    db.add_report(scenario="dump", lat=54.7, lon=20.5, district="Калининград",
                  tags={"forest"}, norms=["koap_8_2"], agencies=["rpn"])
    assert db.count() == 1
    gj = db.export_geojson("dump")
    assert gj["features"][0]["geometry"]["coordinates"] == [20.5, 54.7]
    cols = {r[1] for r in __import__("sqlite3").connect(tmp_path / "t.db").execute("PRAGMA table_info(reports)")}
    assert not ({"name", "address", "fio", "user_id", "chat_id"} & cols)


# ---- шрифты (регрессия: «отсутствует шрифт» при создании PDF) ----
def test_fonts_bundled_in_project():
    from greenclaim.config import ROOT
    for f in ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf"):
        assert (ROOT / "fonts" / f).stat().st_size > 100_000


def test_pdf_works_without_system_fonts_and_from_other_cwd(tmp_path):
    """Системные папки со шрифтами недоступны, рабочая папка — чужая: PDF всё равно собирается."""
    import os
    from greenclaim import pdf as pdfmod
    q = M.qualify("dump", {"land": "forest", "waste": "hazard", "offender": "unknown"})
    data = ComplaintData(qualification=q, addressees=resolve(q, 54.77, 20.60), lat=54.77, lon=20.60,
                         applicant_name="Иванова Мария Петровна", applicant_address="г. Калининград")
    saved = (pdfmod._FONT_DIRS, pdfmod._fonts_ready, os.getcwd())
    try:
        pdfmod._FONT_DIRS = [pdfmod.ROOT / "fonts"]      # только шрифты проекта
        pdfmod._fonts_ready = False
        os.chdir(tmp_path)
        assert build_complaint_pdf(data).startswith(b"%PDF")
    finally:
        os.chdir(saved[2])
        pdfmod._FONT_DIRS, pdfmod._fonts_ready = saved[0], saved[1]


def test_missing_fonts_give_clear_error(tmp_path):
    from greenclaim import pdf as pdfmod
    saved = (pdfmod._FONT_DIRS, pdfmod._fonts_ready)
    try:
        pdfmod._FONT_DIRS, pdfmod._fonts_ready = [tmp_path], False
        with pytest.raises(RuntimeError) as e:
            pdfmod._register_fonts()
        assert "fonts" in str(e.value)
    finally:
        pdfmod._FONT_DIRS, pdfmod._fonts_ready = saved
