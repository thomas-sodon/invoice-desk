"""Fast preprocess tests: allow-list, sanitize, render/resize, storage, EXIF, HEIC, text layer,
pHash and the native-PDF merge. A fake OCR engine keeps these quick; real OCR is in
test_preprocess_ocr.py."""

from __future__ import annotations

import io
from typing import Any

import imagehash
import numpy as np
import pymupdf
import pytest
from invoicedesk.contracts.preprocess import (
    MAX_BYTES,
    TARGET_LONG_EDGE,
    PreprocessedDocument,
    Preprocessor,
    RejectedInput,
)
from invoicedesk.preprocess import DefaultPreprocessor, build_preprocessor, render_key
from invoicedesk.preprocess import images as img_mod
from invoicedesk.preprocess.pdf import open_sanitized
from PIL import Image


def _pre(storage, ocr=None, **kw) -> DefaultPreprocessor:
    return DefaultPreprocessor(storage, ocr=ocr, **kw)


def test_builder_satisfies_protocol(storage, fx):
    pre = build_preprocessor(storage, ocr=fx.FakeOcr())
    assert isinstance(pre, Preprocessor)
    assert isinstance(pre, DefaultPreprocessor)


# ------------------------------------------------------------------ sniffing / allow-list


def test_sniff_magic_bytes(fx):
    pdf = fx.make_pdf()
    png = fx.pdf_to_png(pdf, dpi=40)
    assert img_mod.sniff_mime(pdf) == "application/pdf"
    assert img_mod.sniff_mime(png) == "image/png"
    assert img_mod.sniff_mime(fx.to_format(png, "JPEG")) == "image/jpeg"
    assert img_mod.sniff_mime(b"GIF89a....") is None
    assert img_mod.sniff_mime(b"\x00\x00\x00\x18ftypheic\x00\x00") == "image/heic"
    assert img_mod.sniff_mime(b" " * 2000 + b"%PDF-1.7") is None  # header too late


@pytest.mark.parametrize(
    ("data", "mime", "msg"),
    [
        (b"", "application/pdf", "empty"),
        (b"GIF89a" + b"\0" * 100, "image/png", "Only PDF"),
        (b"%PDF-1.7\n", "text/plain", "Only PDF"),
        (b"hello world", "application/octet-stream", "Only PDF"),
        (b"%PDF-1.7 garbage that is not a pdf", "application/pdf", "could not be read"),
        (b"\x89PNG\r\n\x1a\nnot really", "image/png", "could not be read"),
    ],
)
def test_rejections(storage, fx, data, mime, msg):
    with pytest.raises(RejectedInput, match=msg):
        _pre(storage, fx.FakeOcr()).preprocess(data, mime=mime, document_id="d1")
    assert storage.objects == {}


def test_oversized_rejected_before_parsing(storage, fx):
    data = b"%PDF-1.7\n" + b"0" * MAX_BYTES
    with pytest.raises(RejectedInput, match="limit is 10 MB"):
        _pre(storage, fx.FakeOcr()).preprocess(data, mime="application/pdf", document_id="d1")


def test_too_many_pages(storage, fx):
    with pytest.raises(RejectedInput, match="6 pages; the limit is 5"):
        _pre(storage, fx.FakeOcr()).preprocess(
            fx.make_pdf(pages=6), mime="application/pdf", document_id="d1"
        )


def test_encrypted_pdf_rejected(storage, fx):
    doc = pymupdf.open(stream=fx.make_pdf(), filetype="pdf")
    aes256: int = pymupdf.PDF_ENCRYPT_AES_256  # type: ignore[attr-defined]
    data = doc.tobytes(encryption=aes256, owner_pw="o", user_pw="u")
    with pytest.raises(RejectedInput, match="Password-protected"):
        _pre(storage, fx.FakeOcr()).preprocess(data, mime="application/pdf", document_id="d1")


def test_pixel_bomb_rejected(storage, fx):
    im = Image.new("1", (9000, 7000), 1)  # 63 MP, tiny file
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    with pytest.raises(RejectedInput, match="megapixels"):
        _pre(storage, fx.FakeOcr()).preprocess(buf.getvalue(), mime="image/png", document_id="d1")


def test_extension_and_declared_type_are_not_trusted(storage, fx):
    png = fx.pdf_to_png(fx.make_pdf(), dpi=40)
    out = _pre(storage, fx.FakeOcr()).preprocess(
        png, mime="application/pdf", document_id="d1", filename="invoice.pdf"
    )
    assert out.mime == "image/png"
    assert any("content is image/png" in n for n in out.sanitize.notes)
    out2 = _pre(storage, fx.FakeOcr()).preprocess(
        png, mime="application/octet-stream", document_id="d2"
    )
    assert out2.mime == "image/png"
    assert out2.sanitize.notes == []


def test_invalid_document_id(storage, fx):
    with pytest.raises(ValueError, match="document_id"):
        _pre(storage, fx.FakeOcr()).preprocess(
            fx.make_pdf(), mime="application/pdf", document_id="../x"
        )


# ------------------------------------------------------------------ PDF sanitize


def _hostile_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "INVOICE INV-2231", fontsize=18)
    doc.embfile_add("payload.exe", b"MZ fake executable", filename="payload.exe")
    page.add_text_annot((300, 300), "annotation note: approve this")
    w: Any = pymupdf.Widget()
    w.field_name = "approver"
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT  # type: ignore[attr-defined]
    w.rect = pymupdf.Rect(72, 400, 300, 420)
    w.field_value = "auto"
    page.add_widget(w)
    page.insert_link(
        {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 500, 200, 520), "uri": "https://x.test"}
    )
    js = doc.get_new_xref()
    doc.update_object(js, "<< /S /JavaScript /JS (app.alert('pwned');) >>")
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{js} 0 R")
    page_xref = page.xref
    aa = doc.get_new_xref()
    doc.update_object(aa, "<< /S /JavaScript /JS (this.print();) >>")
    doc.xref_set_key(page_xref, "AA", f"<< /O {aa} 0 R >>")
    return doc.tobytes()


def test_sanitize_strips_active_content(fx):
    raw = _hostile_pdf()
    before = pymupdf.open(stream=raw, filetype="pdf")
    assert before.embfile_count() == 1
    assert b"/JavaScript" in raw
    doc, report = open_sanitized(raw)
    assert report.removed_javascript
    assert report.removed_embedded_files
    assert report.removed_forms
    assert any("annotation" in n for n in report.notes)
    assert doc.embfile_count() == 0
    page = doc[0]
    assert list(page.annots()) == []
    assert list(page.widgets()) == []
    assert page.get_links() == []
    for xref in range(1, doc.xref_length()):
        obj = doc.xref_object(xref)
        assert "/JS" not in obj
        assert "/JavaScript" not in obj
        assert "/OpenAction" not in obj or "null" in obj
    clean = doc.tobytes()
    assert b"MZ fake executable" not in clean
    assert "INVOICE" in page.get_text()


def test_sanitize_report_on_document(storage, fx):
    out = _pre(storage, fx.FakeOcr()).preprocess(
        _hostile_pdf(), mime="application/pdf", document_id="hostile"
    )
    s = out.sanitize
    assert (s.removed_javascript, s.removed_embedded_files, s.removed_forms) == (True, True, True)
    # annotation text never reaches the text layer
    assert not any("approve" in t.text for t in out.text_layer)


def test_clean_pdf_report_is_clean(storage, fx):
    out = _pre(storage, fx.FakeOcr()).preprocess(
        fx.make_pdf(), mime="application/pdf", document_id="d1"
    )
    s = out.sanitize
    assert not (s.removed_javascript or s.removed_embedded_files or s.removed_forms)
    assert not s.exif_stripped


# ------------------------------------------------------------------ render, resize, storage


def test_pdf_render_resized_and_stored(storage, fx):
    data = fx.make_pdf(pages=2)
    out = _pre(storage, fx.FakeOcr()).preprocess(
        data, mime="application/pdf", document_id="doc-1", storage_prefix="maple_row"
    )
    assert isinstance(out, PreprocessedDocument)
    assert [p.index for p in out.pages] == [0, 1]
    for p in out.pages:
        assert max(p.width, p.height) == TARGET_LONG_EDGE
        assert (p.width, p.height) == (1237, 1600)
        assert p.scale == pytest.approx(1600 / 792)
        assert p.storage_key == f"maple_row/renders/doc-1/page-{p.index}.png"
        png = storage.get(p.storage_key)
        im = Image.open(io.BytesIO(png))
        assert im.size == (p.width, p.height)
        import hashlib

        assert hashlib.sha256(png).hexdigest() == p.sha256
    import hashlib

    assert out.sha256 == hashlib.sha256(data).hexdigest()


def test_default_prefix_and_visitor_prefix(storage, fx):
    pre = _pre(storage, fx.FakeOcr(), key_prefix="tenant_x")
    out = pre.preprocess(fx.make_pdf(), mime="application/pdf", document_id="a")
    assert out.pages[0].storage_key == "tenant_x/renders/a/page-0.png"
    out = pre.preprocess(
        fx.make_pdf(), mime="application/pdf", document_id="b", storage_prefix="visitor/s123"
    )
    assert out.pages[0].storage_key == "visitor/s123/renders/b/page-0.png"
    assert render_key("t/", "d", 3) == "t/renders/d/page-3.png"


@pytest.mark.parametrize(
    ("size", "expected"),
    [((4000, 3000), (1600, 1200)), ((300, 400), (1200, 1600)), ((1600, 900), (1600, 900))],
)
def test_raster_resize_exact_long_edge(storage, fx, size, expected):
    im = Image.new("RGB", size, "white")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = _pre(storage, fx.FakeOcr()).preprocess(buf.getvalue(), mime="image/png", document_id="r")
    p = out.pages[0]
    assert (p.width, p.height) == expected
    assert p.scale == pytest.approx(1600 / max(size))


def test_landscape_pdf(storage, fx):
    doc = pymupdf.open()
    doc.new_page(width=792, height=612).insert_text((72, 72), "Landscape invoice", fontsize=14)
    out = _pre(storage, fx.FakeOcr()).preprocess(
        doc.tobytes(), mime="application/pdf", document_id="l"
    )
    assert (out.pages[0].width, out.pages[0].height) == (1600, 1237)


def test_rgba_png_composited_on_white(storage, fx):
    im = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = _pre(storage, fx.FakeOcr()).preprocess(buf.getvalue(), mime="image/png", document_id="a")
    page = Image.open(io.BytesIO(storage.get(out.pages[0].storage_key)))
    assert page.mode == "RGB"
    assert page.getpixel((10, 10)) == (255, 255, 255)


# ------------------------------------------------------------------ EXIF / HEIC


def _jpeg_with_exif(orientation: int) -> bytes:
    im = Image.new("RGB", (800, 400), "white")
    im.paste((0, 0, 0), (0, 0, 100, 100))  # black square top-left in stored orientation
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "PhoneMaker"  # Make
    exif[0x0131] = "Firmware 1.0"  # Software
    gps = {1: "N", 2: (37.0, 46.0, 30.0), 3: "W", 4: (122.0, 25.0, 9.0)}
    exif[0x8825] = gps
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes(), quality=95)
    return buf.getvalue()


def test_exif_orientation_applied_and_metadata_stripped(storage, fx):
    data = _jpeg_with_exif(6)  # rotate 90 CW for display
    assert Image.open(io.BytesIO(data)).getexif()
    out = _pre(storage, fx.FakeOcr()).preprocess(data, mime="image/jpeg", document_id="e")
    p = out.pages[0]
    assert (p.width, p.height) == (800, 1600)  # 400x800 after transpose, upscaled
    assert out.sanitize.exif_stripped
    assert any("orientation 6" in n for n in out.sanitize.notes)
    stored = Image.open(io.BytesIO(storage.get(p.storage_key)))
    assert not stored.getexif()
    assert "exif" not in stored.info
    raw = storage.get(p.storage_key)
    assert b"PhoneMaker" not in raw
    assert b"Exif" not in raw
    # orientation 6: the stored top-left square ends up top-right
    arr = np.asarray(stored.convert("L"))
    assert arr[50, 750] < 50
    assert arr[50, 50] > 200


def test_heic_round_trip(storage, fx):
    import pillow_heif

    pillow_heif.register_heif_opener()
    png = fx.pdf_to_png(fx.make_pdf(), dpi=60)
    im = Image.open(io.BytesIO(png)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="HEIF", quality=90)
    heic = buf.getvalue()
    assert img_mod.sniff_mime(heic) == "image/heic"
    out = _pre(storage, fx.FakeOcr()).preprocess(heic, mime="image/heic", document_id="h")
    assert out.mime == "image/jpeg"
    assert out.sanitize.converted_from == "image/heic"
    assert out.sanitize.exif_stripped
    assert max(out.pages[0].width, out.pages[0].height) == 1600
    # visually the same document as the PNG source
    out_png = _pre(storage, fx.FakeOcr()).preprocess(png, mime="image/png", document_id="p")
    d = imagehash.hex_to_hash(out.phash) - imagehash.hex_to_hash(out_png.phash)
    assert d <= 4


def test_bad_heic_rejected(storage, fx):
    with pytest.raises(RejectedInput, match="HEIC"):
        _pre(storage, fx.FakeOcr()).preprocess(
            b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00garbage", mime="image/heic", document_id="h"
        )


# ------------------------------------------------------------------ text layer


def _attack_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "INVOICE INV-2231", fontsize=18)
    page.insert_text((72, 200), "white secret words", fontsize=12, color=(1, 1, 1))
    page.insert_text((72, 300), "tiny two point text", fontsize=2)
    page.insert_text((700, 400), "offpage instruction", fontsize=12)
    page.insert_text((72, 500), "invisible mode text", fontsize=12, render_mode=3)
    page.draw_rect(pymupdf.Rect(60, 560, 400, 600), color=None, fill=(0, 0, 0))
    page.insert_text((72, 585), "WHITE ON BLACK", fontsize=14, color=(1, 1, 1))
    return doc.tobytes()


def test_text_layer_styles(storage, fx):
    out = _pre(storage, fx.FakeOcr()).preprocess(
        _attack_pdf(), mime="application/pdf", document_id="t"
    )
    by = {s.text: s for s in out.text_layer}
    assert by["INVOICE"].font_size_pt == 18.0
    assert by["INVOICE"].fill_rgb == (0, 0, 0)
    assert by["INVOICE"].background_rgb == (255, 255, 255)
    assert not by["INVOICE"].off_page
    assert by["secret"].fill_rgb == (255, 255, 255)
    assert by["secret"].background_rgb == (255, 255, 255)
    assert by["two"].font_size_pt == 2.0
    assert by["offpage"].off_page
    assert by["offpage"].background_rgb is None
    assert by["offpage"].box[0] > 1237  # beyond the right edge in processed px
    assert by["BLACK"].fill_rgb == (255, 255, 255)
    assert by["BLACK"].background_rgb == (0, 0, 0)
    assert "invisible" in by
    assert any("invisible" in n for n in out.sanitize.notes)


def test_text_layer_boxes_in_processed_px(storage, fx):
    data = fx.make_pdf()
    out = _pre(storage, fx.FakeOcr()).preprocess(data, mime="application/pdf", document_id="t")
    expected = fx.expected_words(data)
    got = [(s.text, s.box) for s in out.text_layer]
    assert [t for t, _ in got] == [t for t, _ in expected]
    for (_, gb), (_, eb) in zip(got, expected, strict=True):
        assert all(abs(a - b) <= 2 for a, b in zip(gb, eb, strict=True))


def test_rotated_pdf_text_layer_lands_on_ink(storage, fx):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "ROTATED HEADER", fontsize=20)
    page.set_rotation(90)
    out = _pre(storage, fx.FakeOcr()).preprocess(
        doc.tobytes(), mime="application/pdf", document_id="rot"
    )
    p = out.pages[0]
    assert (p.width, p.height) == (1600, 1237)
    arr = np.asarray(Image.open(io.BytesIO(storage.get(p.storage_key))).convert("L"))
    for s in out.text_layer:
        x0, y0, x1, y1 = s.box
        assert arr[y0:y1, x0:x1].min() < 60, s.text


# ------------------------------------------------------------------ native merge with fake OCR


def _shift(b, d):
    return (b[0] + d, b[1] + 4, b[2] + d, b[3] - 3)


def test_native_pdf_uses_corroborated_text_layer_words(storage, fx):
    data = _attack_pdf()
    known = dict(fx.expected_words(data))
    # OCR "sees" the visible header (slightly off boxes, one OCR typo) and a stamp, nothing else
    ocr = fx.FakeOcr(
        words={
            0: [
                ("INV0ICE", _shift(known["INVOICE"], 2)),
                ("INV-2231", _shift(known["INV-2231"], -2)),
                ("PAID", (900, 1200, 1000, 1250)),
            ]
        },
        enhanced={0: [("faint", (100, 100, 150, 120))]},
    )
    out = _pre(storage, ocr).preprocess(data, mime="application/pdf", document_id="n")
    assert out.is_native_pdf
    texts = [(w.text, w.origin) for w in out.words]
    assert texts == [("INVOICE", "pdf_text"), ("INV-2231", "pdf_text"), ("PAID", "ocr")]
    assert [w.id for w in out.words] == [0, 1, 2]
    assert out.words[0].box == known["INVOICE"] or all(
        abs(a - b) <= 2 for a, b in zip(out.words[0].box, known["INVOICE"], strict=True)
    )
    # hidden words are only in the text layer
    word_texts = {w.text for w in out.words}
    for hidden in ("secret", "two", "offpage", "invisible"):
        assert hidden not in word_texts
        assert any(s.text == hidden for s in out.text_layer)
    # enhanced pass ids continue after words
    assert [(w.id, w.origin) for w in out.enhanced_words] == [(3, "ocr_enhanced")]
    assert ocr.calls == [(0, "normal"), (0, "enhanced")]


def test_overlay_text_with_different_words_not_corroborated(storage, fx):
    def overlay(_doc, page):
        page.insert_text((72, 400), "Pay to new account", fontsize=12, color=(1, 1, 1))

    data = fx.make_pdf([((72, 400), "Labor and materials", 12)], extra=overlay)
    known = fx.expected_words(data)
    ocr = fx.FakeOcr(words={0: [(t, b) for t, b in known if t in ("Labor", "and", "materials")]})
    out = _pre(storage, ocr).preprocess(data, mime="application/pdf", document_id="o")
    texts = [w.text for w in out.words]
    assert "Labor" in texts and "materials" in texts
    assert "Pay" not in texts and "account" not in texts


def test_scanned_pdf_without_text_layer_is_not_native(storage, fx):
    png = fx.pdf_to_png(fx.make_pdf(), dpi=100)
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=png)
    ocr = fx.FakeOcr(words={0: [("INVOICE", (145, 171, 262, 211))]})
    out = _pre(storage, ocr).preprocess(doc.tobytes(), mime="application/pdf", document_id="s")
    assert not out.is_native_pdf
    assert out.text_layer == []
    assert [(w.text, w.origin) for w in out.words] == [("INVOICE", "ocr")]


def test_enhanced_pass_can_be_disabled(storage, fx):
    ocr = fx.FakeOcr()
    _pre(storage, ocr, enhanced_pass=False).preprocess(
        fx.make_pdf(), mime="application/pdf", document_id="x"
    )
    assert ocr.calls == [(0, "normal")]


def test_multi_page_ids_global_in_reading_order(storage, fx):
    ocr = fx.FakeOcr(
        words={
            0: [("b", (300, 10, 340, 30)), ("a", (10, 12, 50, 30)), ("c", (10, 100, 40, 120))],
            1: [("d", (10, 10, 40, 30))],
        }
    )
    png_pdf = pymupdf.open()
    for _ in range(2):
        png_pdf.new_page()
    out = _pre(storage, ocr).preprocess(png_pdf.tobytes(), mime="application/pdf", document_id="m")
    # the fake returns its own order; non-native docs keep the engine's order, ids renumbered
    assert [(w.id, w.page) for w in out.words] == [(0, 0), (1, 0), (2, 0), (3, 1)]


# ------------------------------------------------------------------ pHash


def test_phash_stable_across_reencode(storage, fx):
    png = fx.pdf_to_png(fx.make_pdf(), dpi=120)
    jpg = fx.to_format(png, "JPEG", quality=70)
    pre = _pre(storage, fx.FakeOcr())
    a = pre.preprocess(png, mime="image/png", document_id="a").phash
    a2 = pre.preprocess(png, mime="image/png", document_id="a2").phash
    b = pre.preprocess(jpg, mime="image/jpeg", document_id="b").phash
    assert a == a2
    assert len(a) == 16
    assert imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b) <= 2
    other = fx.make_pdf([((72, 600), "Completely different layout and content here", 24)])
    c = pre.preprocess(other, mime="application/pdf", document_id="c").phash
    assert imagehash.hex_to_hash(a) - imagehash.hex_to_hash(c) > 6  # R4 threshold is <= 6


def test_phash_pdf_vs_its_scan_is_close(storage, fx):
    data = fx.make_pdf()
    pre = _pre(storage, fx.FakeOcr())
    a = pre.preprocess(data, mime="application/pdf", document_id="a").phash
    b = pre.preprocess(fx.pdf_to_png(data, dpi=100), mime="image/png", document_id="b").phash
    assert imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b) <= 6
