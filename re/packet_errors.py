"""Recognize malformed frames rejected by the external WHOOP packet parser.

The reverse-engineering scripts consume arbitrary BLE notification fragments. The
parser reports bad framing and checksums with plain ``Exception`` messages, so
keep those expected wire errors at the callback boundary while re-raising bugs.
"""

import struct


def is_malformed_packet_error(error: Exception) -> bool:
    """Return whether ``error`` is a known invalid or incomplete wire frame."""
    if isinstance(error, (IndexError, ValueError, struct.error)):
        return True
    return str(error).startswith("invalid packet ")
