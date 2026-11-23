"""m4a(MP4)의 색인(moov)을 음성 자료(mdat) 앞으로 옮기는 동작을 검증합니다.

실측(2026-11-23): 구글 Speech-to-Text v2는 색인이 파일 끝에 있는 m4a를 읽지 못했습니다(57초 파일 9번 모두 거절,
4초 파일은 60번에 3번 거절). 색인을 앞에 두면 같은 음성이 모두 변환됐습니다. 휴대폰 녹음기는 색인을 끝에 씁니다.
"""

import struct

import pytest

from app.mp4_faststart import faststart


def atom(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def chunk_table(kind: bytes, offsets: list[int]) -> bytes:
    """stco(32비트)·co64(64비트): 음성 조각이 파일의 몇 번째 바이트에서 시작하는지 적은 표입니다."""
    width = ">I" if kind == b"stco" else ">Q"
    return atom(kind, b"\x00\x00\x00\x00" + struct.pack(">I", len(offsets)) + b"".join(struct.pack(width, o) for o in offsets))


def recording(table_kind: bytes = b"stco") -> tuple[bytes, list[int]]:
    """녹음기가 만든 파일처럼 ftyp, mdat, moov 순서입니다. 조각 표는 mdat 안의 두 지점을 가리킵니다."""
    ftyp = atom(b"ftyp", b"M4A \x00\x00\x00\x00")
    mdat = atom(b"mdat", b"first-chunk|second-chunk")
    offsets = [len(ftyp) + 8, len(ftyp) + 8 + len(b"first-chunk|")]
    table = atom(b"stbl", atom(b"stsd", b"\x00" * 8) + chunk_table(table_kind, offsets))
    moov = atom(b"moov", atom(b"mvhd", b"\x00" * 12) + atom(b"trak", atom(b"mdia", atom(b"minf", table))))
    return ftyp + mdat + moov, offsets


def table_offsets(data: bytes, kind: bytes) -> list[int]:
    width, size = (">I", 4) if kind == b"stco" else (">Q", 8)
    start = data.index(kind) + 4
    count = struct.unpack_from(">I", data, start + 4)[0]
    return [struct.unpack_from(width, data, start + 8 + index * size)[0] for index in range(count)]


@pytest.mark.parametrize("kind", [b"stco", b"co64"])
def test_moves_the_index_before_the_audio_and_keeps_chunk_offsets_pointing_at_the_same_bytes(kind):
    original, offsets = recording(kind)

    moved = faststart(original)

    assert moved.index(b"ftyp") < moved.index(b"moov") < moved.index(b"mdat")
    assert len(moved) == len(original)
    new_offsets = table_offsets(moved, kind)
    assert [moved[o:o + 5] for o in new_offsets] == [original[o:o + 5] for o in offsets] == [b"first", b"secon"]


def test_leaves_a_file_whose_index_is_already_in_front_alone():
    already = faststart(recording()[0])

    assert faststart(already) is already


@pytest.mark.parametrize("data", [
    b"",
    b"\x1a\x45\xdf\xa3\x9fB\x86\x81\x01webm-opus-audio",                 # webm
    b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00",                      # wav
    atom(b"ftyp", b"M4A ") + atom(b"mdat", b"audio"),                     # 색인이 없는 파일
    recording()[0][:-6],                                                  # 끝이 잘린 파일
    atom(b"ftyp", b"M4A ") + atom(b"mdat", b"a") + atom(b"moov", atom(b"trak", b"\x00\x00\x00\x09stc")),  # 색인 안이 깨진 파일
], ids=["empty", "webm", "wav", "no_index", "truncated", "broken_index"])
def test_leaves_other_formats_and_unreadable_files_alone(data):
    assert faststart(data) is data


def hostile(moov_payload: bytes = b"", extra_top: bytes = b"") -> bytes:
    """올린 사람이 꾸민 파일입니다. 색인이 끝에 있어 옮기기 대상이 되도록 만듭니다."""
    return atom(b"ftyp", b"M4A ") + extra_top + atom(b"mdat", b"audio") + atom(b"moov", moov_payload)


def nested(kind: bytes, depth: int) -> bytes:
    payload = b""
    for _ in range(depth):
        payload = atom(kind, payload)
    return payload


@pytest.mark.parametrize("data", [
    hostile(moov_payload=nested(b"trak", 1200)),                                   # 재귀 한도를 넘기려는 겹침
    hostile(moov_payload=nested(b"trak", 9)),                                      # 실제 깊이(5)를 넘는 겹침
    hostile(extra_top=atom(b"free") * 2000),                                       # 상자 수로 시간을 끌려는 파일
    hostile(moov_payload=atom(b"trak", atom(b"free") * 2000)),                     # 색인 안에 숨긴 많은 상자
    hostile(moov_payload=atom(b"trak", chunk_table(b"stco", [100] * 200_000))),    # 지나치게 긴 조각 표
], ids=["recursion_bomb", "too_deep", "too_many_boxes", "too_many_inner_boxes", "huge_offset_table"])
def test_leaves_hostile_files_alone_instead_of_crashing_or_stalling(data):
    """올라온 음성은 믿을 수 없는 자료입니다. 한도를 넘는 구조는 손대지 않고 구글의 판정(400)에 맡깁니다."""
    assert faststart(data) is data


def test_leaves_a_file_with_audio_after_the_index_alone():
    """색인 뒤에도 음성 자료가 있으면 조각 위치를 한 값으로 밀 수 없습니다. 손대지 않고 구글의 판정에 맡깁니다."""
    data = recording()[0] + atom(b"mdat", b"more-audio")

    assert faststart(data) is data
