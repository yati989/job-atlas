import os

from app.process_status import process_id_is_alive


def test_process_id_is_alive_rejects_invalid_identifiers():
    assert process_id_is_alive(None) is False
    assert process_id_is_alive(0) is False
    assert process_id_is_alive(-1) is False
    assert process_id_is_alive(True) is False


def test_process_id_is_alive_recognizes_current_process():
    assert process_id_is_alive(os.getpid()) is True
