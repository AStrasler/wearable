"""Recognize malformed frames rejected by the external WHOOP packet parser.

The reverse-engineering scripts consume arbitrary BLE notification fragments. The
parser reports bad framing and checksums with plain ``Exception`` messages, so
keep those expected wire errors at the callback boundary while re-raising bugs.
"""

import asyncio
import struct


def is_malformed_packet_error(error: Exception) -> bool:
    """Match only parser framing errors, not callback/application bugs."""
    # The external parser uses plain Exception for framing/CRC failures and
    # struct.error for truncated frames. Exact classes keep a ValueError or
    # IndexError from unrelated callback code from being silently discarded.
    if error.__class__ is struct.error:
        return True
    return error.__class__ is Exception and str(error).startswith("invalid packet ")


class CallbackFailure:
    """Carry an unexpected synchronous BLE callback error back into main()."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self._event = asyncio.Event()

    def report(self, error: Exception) -> bool:
        """Return True for malformed input; otherwise retain a fatal error."""
        if is_malformed_packet_error(error):
            return True
        if self.error is None:
            self.error = error
            self._event.set()
        return False

    def raise_if_failed(self) -> None:
        if self.error is not None:
            raise RuntimeError("BLE notification callback failed") from self.error

    async def sleep(self, seconds: float) -> None:
        """Sleep normally, but wake promptly if a BLE callback fails."""
        self.raise_if_failed()
        try:
            await asyncio.wait_for(self._event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        self.raise_if_failed()

    async def get(self, queue: asyncio.Queue, timeout: float):
        """Wait for a callback result or fail immediately on callback error."""
        self.raise_if_failed()
        queue_task = asyncio.create_task(queue.get())
        error_task = asyncio.create_task(self._event.wait())
        done, pending = await asyncio.wait(
            (queue_task, error_task),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.raise_if_failed()
        if queue_task in done:
            return queue_task.result()
        raise asyncio.TimeoutError
