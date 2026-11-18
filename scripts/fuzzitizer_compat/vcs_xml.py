"""Read coverage XML produced by both older and current VCS releases."""

from contextlib import contextmanager
import gzip
import re

import zstandard


def coverage_bits(value, cov_type):
    """Expand VCS's hexadecimal bit vectors, preserving every leading nibble."""
    if cov_type == "assert":
        return value
    if value.startswith("0x"):
        digits = value[2:]
        if not re.fullmatch(r"[0-9a-fA-F]+", digits):
            raise ValueError(f"invalid hexadecimal VCS coverage vector: {value!r}")
        return "".join(f"{int(digit, 16):04b}" for digit in digits)
    if any(bit not in "01" for bit in value):
        raise ValueError(f"invalid binary VCS coverage vector: {value!r}")
    return value


@contextmanager
def open_xml(path, mode="rb"):
    if mode != "rb":
        raise ValueError("VCS XML must be opened in binary read mode")
    with open(path, "rb") as source:
        magic = source.read(4)
        source.seek(0)
        if magic.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=source) as decoded:
                yield decoded
        elif magic == b"\x28\xb5\x2f\xfd":
            with zstandard.ZstdDecompressor().stream_reader(source) as decoded:
                yield decoded
        else:
            yield source
