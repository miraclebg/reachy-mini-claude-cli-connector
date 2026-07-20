import json
import os
import socket
import tempfile
import time

from beacon import Beacon, beacon_payload, default_server_name, local_ip, server_id


def test_server_id_is_created_then_stable():
    d = tempfile.mkdtemp()
    p = os.path.join(d, ".server_id")
    a = server_id(p)
    assert a and len(a) >= 8
    assert os.path.exists(p)
    assert server_id(p) == a  # stable across calls


def test_beacon_payload_wire_shape():
    raw = beacon_payload("abc123", "studio-mac", "http://10.0.0.5:8080")
    obj = json.loads(raw.decode())
    # This literal shape is the cross-venv contract with reachy_app/discovery.py.
    assert obj == {
        "reachy_connector": 1,
        "id": "abc123",
        "name": "studio-mac",
        "url": "http://10.0.0.5:8080",
    }


def test_beacon_payload_carries_no_secret():
    raw = beacon_payload("abc123", "studio-mac", "http://10.0.0.5:8080").decode().lower()
    for forbidden in ("token", "secret", "password", "authorization"):
        assert forbidden not in raw


def test_default_server_name_and_local_ip():
    assert default_server_name()          # never empty
    ip = local_ip()
    assert ip.count(".") == 3             # dotted quad


def test_beacon_broadcasts_on_the_wire():
    # Bind a listener first, then run one beacon tick at it.
    port = 48999
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", port))
    rx.settimeout(3.0)
    b = Beacon(lambda: beacon_payload("id1", "n1", "http://127.0.0.1:8080"),
               port=port, interval_s=0.2)
    b.start()
    try:
        data, _addr = rx.recvfrom(2048)
        obj = json.loads(data.decode())
        assert obj["reachy_connector"] == 1 and obj["id"] == "id1"
    finally:
        b.stop()
        rx.close()


def test_beacon_stop_is_clean():
    b = Beacon(lambda: beacon_payload("i", "n", "http://127.0.0.1:8080"),
               port=48998, interval_s=0.05)
    b.start()
    time.sleep(0.15)
    b.stop()
    assert not b.is_alive()


def test_whoami_payload_shape_and_no_secret():
    from beacon import whoami_payload
    p = whoami_payload("abc123", "studio-mac")
    assert p["id"] == "abc123"
    assert p["name"] == "studio-mac"
    assert "version" in p
    # the robot cross-checks this id against the beacon's claimed id
    assert set(p) == {"id", "name", "version"}
    blob = json.dumps(p).lower()
    for forbidden in ("token", "secret", "password"):
        assert forbidden not in blob
