from __future__ import annotations

import hashlib
import os

import pytest

from infra.media_store import MediaStore, _safe_room
from infra.store import Store


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem semantics only")
def test_windows_room_storage_key_avoids_forbidden_and_case_aliases():
    first = _safe_room("tui:group:Arkham")
    second = _safe_room("tui:group:arkham")

    assert first.startswith("room-")
    assert second.startswith("room-")
    assert first != second
    assert not any(char in first for char in '<>:"/\\|?*')
    assert not any(char in second for char in '<>:"/\\|?*')


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem semantics only")
async def test_windows_media_round_trip_accepts_protocol_room_ids(tmp_path):
    store = MediaStore(Store(), tmp_path)
    room = "tui:group:arkham"
    data = b"\x89PNG\r\n\x1a\nwindows-media"

    record = await store.register_blob(
        room=room,
        data=data,
        mime="image/png",
        name="handout.png",
        uploader="keeper",
    )

    loaded_record, loaded = await store.read_bytes(room, record.hash)
    assert loaded_record == record
    assert loaded == data

    expected_dir = tmp_path / "media" / f"room-{hashlib.sha256(room.encode('utf-8')).hexdigest()}"
    assert expected_dir.is_dir()
