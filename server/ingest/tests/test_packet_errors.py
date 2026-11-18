import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "re"))
from packet_errors import is_malformed_packet_error


def test_known_malformed_wire_frame_errors_are_classified():
    assert is_malformed_packet_error(IndexError("short frame"))
    assert is_malformed_packet_error(ValueError("unknown packet type"))
    assert is_malformed_packet_error(struct.error("unpack requires a buffer"))
    assert is_malformed_packet_error(Exception("invalid packet data crc32"))


def test_unexpected_exceptions_are_not_swallowed_as_wire_errors():
    assert not is_malformed_packet_error(RuntimeError("callback bug"))
    assert not is_malformed_packet_error(Exception("storage failed"))
