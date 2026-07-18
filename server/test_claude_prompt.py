from claude_client import _augment_prompt, VOICE_SYSTEM_PROMPT


def test_prompt_documents_movement():
    p = VOICE_SYSTEM_PROMPT
    assert "[MOVE" in p              # marker taught
    assert "look_left" in p          # a named routine listed
    assert "[/MOVE]" in p            # keyframe block form taught


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
