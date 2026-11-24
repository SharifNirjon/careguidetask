from app.pipeline.text_utils import assess_text, clean_text


def test_nul_and_control_characters_are_stripped() -> None:
    out = clean_text("INV\x00-001\x07 total\x1b 10.00", max_chars=1000)
    assert out.text == "INV-001 total 10.00"
    assert "\x00" not in out.text


def test_line_breaks_are_kept_but_whitespace_collapsed() -> None:
    raw = "INVOICE   \r\n\r\n\r\n\r\nTotal\t\t 10.00   Due now"
    assert clean_text(raw, 1000).text == "INVOICE\n\nTotal 10.00\nDue now"


def test_nfkc_normalization() -> None:
    assert clean_text("ﬁnal １２３ ＥＵＲ", 1000).text == "final 123 EUR"


def test_truncation_is_recorded_as_warning() -> None:
    out = clean_text("x" * 50, max_chars=10)
    assert out.text == "x" * 10
    assert out.warnings[0]["code"] == "TEXT_TRUNCATED"
    assert out.warnings[0]["details"]["original_chars"] == 50


def test_garbage_detection() -> None:
    assert assess_text("(cid:12)(cid:13)(cid:14) foo").is_garbage(0.5, 0.02)
    assert assess_text("��� invoice total").is_garbage(0.5, 0.02)
    assert assess_text("@@## $$%% ^^&& **").is_garbage(0.5, 0.02)
    assert not assess_text("INVOICE INV-001 Total 270.00 EUR").is_garbage(0.5, 0.02)
