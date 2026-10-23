from app.i18n import t


def test_unknown_language_falls_back_to_english():
    assert t("xx", "save") == "Save"


def test_pidgin_differs_from_english():
    assert t("pcm", "saved") != t("en", "saved")