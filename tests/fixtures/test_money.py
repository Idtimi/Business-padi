from app.money import to_kobo, fmt_naira


def test_to_kobo_whole_naira():
    assert to_kobo(42500) == 4250000


def test_to_kobo_with_kobo():
    assert to_kobo("19.99") == 1999


def test_fmt_whole_naira():
    assert fmt_naira(4250000) == "N42,500"


def test_fmt_with_kobo():
    assert fmt_naira(150) == "N1.50"


def test_roundtrip():
    assert fmt_naira(to_kobo(127500)) == "N127,500"