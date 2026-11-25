"""m4a(MP4) 파일의 색인(moov)을 음성 자료(mdat) 앞으로 옮깁니다.

휴대폰 녹음기는 녹음이 끝난 뒤에야 색인을 쓸 수 있어 파일 끝에 둡니다. 구글 Speech-to-Text v2는 그런 파일을
읽지 못했습니다(실측 2026-11-23: 57초 파일 9번 모두 "RecognitionAudio empty", 4초 파일은 60번에 3번).
색인을 앞으로 옮기면 음성 조각의 위치가 색인 길이만큼 밀리므로, 조각 위치 표(stco·co64)의 값도 그만큼 더합니다.
MP4가 아니거나, 색인이 이미 앞에 있거나, 구조를 읽을 수 없으면 받은 바이트를 그대로 돌려줍니다.
"""

import itertools
import struct
from collections.abc import Iterator

# 조각 위치 표까지 내려가는 길에 있는 상자들
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
OFFSET_TABLES = {b"stco": (">I", 4), b"co64": (">Q", 8)}
# 올라온 파일은 믿을 수 없는 자료다. 60초 녹음은 상자 수십 개, 깊이 5, 조각 수천 개 안쪽이다.
# 한도를 넘는 파일은 손대지 않는다. 겹친 상자로 재귀 한도를 넘기거나 상자 수로 서버를 붙잡는 것을 막는다.
MAX_BOXES = 1000
MAX_DEPTH = 8
MAX_OFFSETS = 100_000


def boxes(
    data: bytes | bytearray, start: int, end: int, visited: Iterator[int]
) -> Iterator[tuple[bytes, int, int, int]]:
    """[start, end) 안의 상자를 차례로 (종류, 시작, 머리 길이, 전체 길이)로 냅니다. 구조가 어긋나면 ValueError입니다.

    visited는 파일 전체에서 지금까지 본 상자 수를 세는 공용 계수기입니다.
    """
    position = start
    while position < end:
        if next(visited) > MAX_BOXES:
            raise ValueError("too many boxes")
        if end - position < 8:
            raise ValueError("box header is cut off")
        size, kind = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:  # 4GB를 넘는 상자는 길이를 뒤의 8바이트에 적는다
            if end - position < 16:
                raise ValueError("large box header is cut off")
            size, header = struct.unpack_from(">Q", data, position + 8)[0], 16
        elif size == 0:  # 파일 끝까지
            size = end - position
        if size < header or position + size > end:
            raise ValueError("box size is out of range")
        yield kind, position, header, size
        position += size


def shift_chunk_offsets(
    moov: bytearray, start: int, end: int, shift: int, visited: Iterator[int], depth: int = 1
) -> None:
    """색인 안의 조각 위치 표를 찾아 모든 값에 shift를 더합니다."""
    if depth > MAX_DEPTH:
        raise ValueError("boxes are nested too deeply")
    for kind, position, header, size in boxes(moov, start, end, visited):
        if kind in CONTAINERS:
            shift_chunk_offsets(moov, position + header, position + size, shift, visited, depth + 1)
        elif kind in OFFSET_TABLES:
            width, step = OFFSET_TABLES[kind]
            if size < header + 8:
                raise ValueError("offset table is cut off")
            count = struct.unpack_from(">I", moov, position + header + 4)[0]
            first = position + header + 8
            if count > MAX_OFFSETS or first + count * step > position + size:
                raise ValueError("offset table is too long")
            for at in range(first, first + count * step, step):
                struct.pack_into(width, moov, at, struct.unpack_from(width, moov, at)[0] + shift)


def faststart(data: bytes) -> bytes:
    """색인이 모든 음성 자료 뒤에 있는 MP4면 색인을 첫 음성 자료 앞으로 옮긴 사본을, 아니면 data 그대로 돌려줍니다."""
    try:
        visited = itertools.count(1)
        top = list(boxes(data, 0, len(data), visited))
        kinds = [kind for kind, *_ in top]
        if kinds.count(b"moov") != 1 or b"mdat" not in kinds:
            return data
        index = kinds.index(b"moov")
        # 색인 뒤에도 음성 자료가 있으면 조각 위치를 한 값으로 밀 수 없다. 색인이 이미 앞이면 할 일이 없다.
        if b"mdat" in kinds[index:] or b"mdat" not in kinds[:index]:
            return data
        _, start, header, size = top[index]
        moov = bytearray(data[start:start + size])
        shift_chunk_offsets(moov, header, size, size, visited)
        audio_start = top[kinds.index(b"mdat")][1]
        return data[:audio_start] + bytes(moov) + data[audio_start:start] + data[start + size:]
    except (ValueError, struct.error):  # struct.error: 더한 값이 32비트를 넘는 경우(4GB 이상 파일)
        return data
