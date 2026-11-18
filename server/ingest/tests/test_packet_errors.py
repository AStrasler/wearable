import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "re"))
from packet_errors import is_malformed_packet_error


def test_known_malformed_wire_frame_errors_are_classified():
    assert is_malformed_packet_error(struct.error("unpack requires a buffer"))
    assert is_malformed_packet_error(Exception("invalid packet data crc32"))


def test_unexpected_exceptions_are_not_swallowed_as_wire_errors():
    assert not is_malformed_packet_error(IndexError("callback bug"))
    assert not is_malformed_packet_error(ValueError("unknown packet type"))
    assert not is_malformed_packet_error(RuntimeError("callback bug"))
    assert not is_malformed_packet_error(Exception("storage failed"))


def test_callback_failure_is_carried_back_to_async_task():
    import asyncio
    from packet_errors import CallbackFailure

    async def scenario():
        failure = CallbackFailure()
        queue = asyncio.Queue()
        failure.report(OSError("capture storage failed"))
        try:
            await failure.get(queue, timeout=1)
        except RuntimeError as exc:
            assert isinstance(exc.__cause__, OSError)
        else:
            raise AssertionError("callback failure should stop the async task")

    asyncio.run(scenario())
