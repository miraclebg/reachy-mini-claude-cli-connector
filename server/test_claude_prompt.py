from claude_client import _augment_prompt


def test_no_image_unchanged():
    assert _augment_prompt("как си?", None, False) == "как си?"


def test_image_path_appended():
    out = _augment_prompt("какво виждаш?", "camera_view.jpg", False)
    assert "camera_view.jpg" in out
    assert "Read tool" in out
    assert out.startswith("какво виждаш?")


def test_camera_failed_note():
    out = _augment_prompt("какво виждаш?", None, True)
    assert "no image" in out.lower()
    assert out.startswith("какво виждаш?")


def test_image_path_takes_precedence_over_failed():
    out = _augment_prompt("q", "camera_view.jpg", True)
    assert "camera_view.jpg" in out
