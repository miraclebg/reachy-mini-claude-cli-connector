from movement import parse_move, wants_move


def test_parse_named_routine():
    spec, cleaned = parse_move("[MOVE look_left]")
    assert spec == "look_left"
    assert cleaned == ""


def test_parse_keyframes_json():
    spec, cleaned = parse_move('[MOVE][{"yaw": 20, "dur": 0.3}, {"yaw": 0, "dur": 0.4}][/MOVE]')
    assert isinstance(spec, list) and spec[0]["yaw"] == 20
    assert cleaned == ""


def test_parse_none_when_no_marker():
    spec, cleaned = parse_move("здрасти, как си?")
    assert spec is None
    assert cleaned == "здрасти, как си?"


def test_parse_leaves_look_marker():
    spec, cleaned = parse_move("[MOVE look_left][LOOK]")
    assert spec == "look_left"
    assert cleaned == "[LOOK]"


def test_parse_strips_marker_keeps_speech():
    spec, cleaned = parse_move("Хайде! [MOVE nod]")
    assert spec == "nod"
    assert cleaned == "Хайде!"


def test_parse_bad_json_returns_none_spec():
    spec, cleaned = parse_move("[MOVE][not json[/MOVE]")
    assert spec is None
    assert cleaned == ""


def test_wants_move():
    assert wants_move("[MOVE nod]")
    assert wants_move('[MOVE][{"yaw":1}][/MOVE]')
    assert not wants_move("no markers here")
