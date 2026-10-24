from astreum.communication.message_pow import (
    MAX_INLINE_MESSAGE_BYTES,
    MESSAGE_FRAMING_BYTES,
)
from astreum.communication.models.peer_info import PEER_INFO_BYTES
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.expression.expr import RESOLUTION_DYNAMIC, RESOLUTION_RADIX
from astreum.expression.shape import parse_shape

# STORAGE_PUT layout (after the 1-byte request code):
#   provider_count u8 (1 or 2) + provider_count * provider_payload (70B)
#   + count u16 + count * (expr_id 32B + payload_type 1B)
MAX_PUT_PROVIDERS = 2
BATCH_COUNT_BYTES = 2
BATCH_ENTRY_BYTES = 32 + 1


# STORAGE_GET layout (after the 1-byte request code):
#   count u16 + count * entry
# A plain entry is expr_id 32B + resolution 1B. A scoped entry sets the high
# bit of the resolution byte and is followed by the block_hash 32B whose
# provider holds the expr: it is routed by that hash. Resolution 0 (DYNAMIC)
# is followed by a shape: a varint length and that many shape bytes. The shape
# comes after the block_hash of a scoped entry. Resolution 4 (RADIX) is
# followed by a radix param: the key bit offset as a varint, the key length as a
# varint, and the key bytes. It comes after the block_hash of a scoped entry.
GET_HEADER_BYTES = 2
GET_ENTRY_BYTES = 32 + 1
SCOPED_GET_ENTRY_BYTES = GET_ENTRY_BYTES + 32
SCOPED_FLAG = 0x80


def _varint_bytes(value: int) -> bytes:
    """Encode a non-negative integer as a little-endian base 128 varint.

    Args:
        value (int): The integer to encode.

    Returns:
        bytes: The varint. One byte covers 0 to 127.
    """
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a varint from ``data`` at ``pos``.

    Args:
        data (bytes): The bytes to read from.
        pos (int): The offset of the first varint byte.

    Returns:
        tuple[int, int]: The value and the offset after the varint.

    Raises:
        ValueError: If the varint is cut short or longer than 3 bytes.
    """
    value = 0
    for index in range(3):
        if pos + index >= len(data):
            raise ValueError("STORAGE_GET truncated shape length")
        byte = data[pos + index]
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return value, pos + index + 1
    raise ValueError("STORAGE_GET shape length is too long")


def encode_radix_param(key_bit_offset: int, key: bytes) -> bytes:
    """Encode the param of a RADIX entry.

    Args:
        key_bit_offset (int): The number of key bits that earlier nodes matched.
        key (bytes): The key to walk to.

    Returns:
        bytes: The offset varint, the key length varint and the key.

    Raises:
        ValueError: If the offset is negative or past the end of the key.
    """
    if not 0 <= key_bit_offset <= len(key) * 8:
        raise ValueError("radix key bit offset is outside the key")
    return _varint_bytes(key_bit_offset) + _varint_bytes(len(key)) + key


def read_radix_param(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """Read the param of a RADIX entry from ``data`` at ``pos``.

    Args:
        data (bytes): The bytes to read from.
        pos (int): The offset of the first param byte.

    Returns:
        tuple[int, bytes, int]: The key bit offset, the key and the offset
        after the param.

    Raises:
        ValueError: If the param is cut short, or the offset is past the end
            of the key.
    """
    key_bit_offset, pos = _read_varint(data, pos)
    length, pos = _read_varint(data, pos)
    if pos + length > len(data):
        raise ValueError("STORAGE_GET truncated radix key")
    key = bytes(data[pos : pos + length])
    if key_bit_offset > length * 8:
        raise ValueError("radix key bit offset is outside the key")
    return key_bit_offset, key, pos + length


def parse_radix_param(param: bytes) -> tuple[int, bytes]:
    """Decode the param of a RADIX entry.

    Args:
        param (bytes): The encoded param.

    Returns:
        tuple[int, bytes]: The key bit offset and the key.

    Raises:
        ValueError: If the param is malformed or has bytes left over.
    """
    key_bit_offset, key, pos = read_radix_param(param, 0)
    if pos != len(param):
        raise ValueError("radix param has bytes left over")
    return key_bit_offset, key


def get_entry_size(entry: tuple) -> int:
    """Return the encoded size in bytes of one STORAGE_GET entry.

    Args:
        entry (tuple): ``(expr_id, resolution, block_hash, shape)``. A
            shorter tuple is taken as unscoped and shapeless. For a RADIX
            entry the last field holds the encoded radix param.

    Returns:
        int: 33 for a plain entry, plus 32 if scoped, plus the length varint
        and the shape bytes if the entry carries a shape, or the param bytes
        if it is a RADIX entry.
    """
    block_hash = entry[2] if len(entry) > 2 else None
    shape = entry[3] if len(entry) > 3 else b""
    size = GET_ENTRY_BYTES
    if block_hash is not None:
        size += 32
    if entry[1] == RESOLUTION_DYNAMIC:
        size += len(_varint_bytes(len(shape))) + len(shape)
    elif entry[1] == RESOLUTION_RADIX:
        size += len(shape)
    return size


def max_get_entry_bytes(shape_max_bytes: int, radix_key_max_bytes: int = 0) -> int:
    """Return the size of the longest STORAGE_GET entry.

    Args:
        shape_max_bytes (int): The longest shape that is allowed.
        radix_key_max_bytes (int): The longest radix key that is allowed.

    Returns:
        int: The size of a scoped entry with a shape of that length, or of a
        scoped RADIX entry with a key of that length, whichever is longer.
    """
    shaped = SCOPED_GET_ENTRY_BYTES + len(_varint_bytes(shape_max_bytes)) + shape_max_bytes
    radix = (
        SCOPED_GET_ENTRY_BYTES
        + len(_varint_bytes(radix_key_max_bytes * 8))
        + len(_varint_bytes(radix_key_max_bytes))
        + radix_key_max_bytes
    )
    return max(shaped, radix)


def get_body_budget(budget_bytes: int) -> int:
    """Return how many entry bytes fit in one STORAGE_GET datagram.

    Args:
        budget_bytes (int): The maximum byte budget for the datagram.

    Returns:
        int: The budget less the message framing, the request code and the
        entry count.
    """
    return budget_bytes - MESSAGE_FRAMING_BYTES - 1 - GET_HEADER_BYTES


def batch_header_bytes(providers: int = 1) -> int:
    """Return the STORAGE_PUT header size after the request code.

    Args:
        providers: The number of provider payloads in the request.

    Returns:
        int: The count of provider bytes plus the count fields.
    """
    return 1 + providers * PEER_INFO_BYTES + BATCH_COUNT_BYTES


def max_batch_entries(budget_bytes: int, providers: int = 1) -> int:
    """Return how many entries fit in one STORAGE_PUT datagram.

    Args:
        budget_bytes (int): The maximum byte budget for the datagram.
        providers: The number of provider payloads in the request.

    Returns:
        int: The number of entries that fit.
    """
    return (
        budget_bytes - MESSAGE_FRAMING_BYTES - 1 - batch_header_bytes(providers)
    ) // BATCH_ENTRY_BYTES


class StorageRequest:
    code: StorageRequestCode
    data: bytes
    expr_id: bytes
    payload_type: int | None
    entries: list[tuple] | None

    def __init__(
        self,
        code: StorageRequestCode,
        data: bytes = b"",
        expr_id: bytes = None,
        payload_type: int | None = None,
        entries: list[tuple] | None = None,
    ):
        """Initialize a StorageRequest.

        *entries* are ``(expr_id, payload_type)`` for STORAGE_PUT. For
        STORAGE_GET they are ``(expr_id, resolution, block_hash | None,
        shape)``. A shorter tuple is taken as unscoped and shapeless.

        Args:
            code (StorageRequestCode): The request type code.
            data (bytes): The provider payload for STORAGE_PUT. Defaults to b"".
            expr_id (bytes): The expression ID. Defaults to None.
            payload_type (int | None): The payload type. Defaults to None.
            entries (list[tuple] | None): The entry list. Defaults to None.

        Returns:
            None
        """
        self.code = code
        self.data = data
        self.expr_id = expr_id
        self.payload_type = payload_type
        if code == StorageRequestCode.STORAGE_GET and entries is not None:
            entries = [
                (
                    e[0],
                    e[1],
                    e[2] if len(e) > 2 else None,
                    e[3] if len(e) > 3 else b"",
                )
                for e in entries
            ]
        self.entries = entries

    def to_bytes(self):
        """Encode the storage request to bytes.

        Args:
            None

        Returns:
            bytes: The encoded request bytes.
        """
        if self.code == StorageRequestCode.STORAGE_PUT:
            return self._put_to_bytes()
        if self.code == StorageRequestCode.STORAGE_GET:
            return self._get_to_bytes()
        if self.code == StorageRequestCode.STORAGE_INDEX_REQUEST:
            return bytes([self.code.value])
        raise ValueError(f"Cannot encode StorageRequestCode {self.code!r}")

    def _get_to_bytes(self) -> bytes:
        """Encode a STORAGE_GET request to bytes.

        Args:
            None

        Returns:
            bytes: The encoded STORAGE_GET bytes.
        """
        if not self.entries:
            raise ValueError("STORAGE_GET requires at least one entry")
        if len(self.entries) > 0xFFFF:
            raise ValueError("STORAGE_GET entry count exceeds u16")
        parts = [
            bytes([self.code.value]),
            len(self.entries).to_bytes(2, "big"),
        ]
        for expr_id, resolution, block_hash, shape in self.entries:
            if len(expr_id) != 32:
                raise ValueError("STORAGE_GET expr_id must be 32 bytes")
            if resolution == RESOLUTION_DYNAMIC:
                parse_shape(shape)
            elif resolution == RESOLUTION_RADIX:
                parse_radix_param(shape)
            elif shape:
                raise ValueError("STORAGE_GET shape needs resolution 0")
            tail = b""
            if resolution == RESOLUTION_DYNAMIC:
                tail = _varint_bytes(len(shape)) + shape
            elif resolution == RESOLUTION_RADIX:
                tail = shape
            if block_hash is None:
                parts.append(expr_id + bytes([resolution]) + tail)
                continue
            if len(block_hash) != 32:
                raise ValueError("STORAGE_GET block_hash must be 32 bytes")
            parts.append(
                expr_id + bytes([resolution | SCOPED_FLAG]) + block_hash + tail
            )
        body = b"".join(parts)
        if len(body) > MAX_INLINE_MESSAGE_BYTES:
            raise ValueError(
                f"STORAGE_GET too large ({len(body)} > {MAX_INLINE_MESSAGE_BYTES})"
            )
        return body

    def _put_to_bytes(self) -> bytes:
        """Encode a STORAGE_PUT request to bytes.

        Args:
            None

        Returns:
            bytes: The encoded STORAGE_PUT bytes.
        """
        providers, remainder = divmod(len(self.data), PEER_INFO_BYTES)
        if remainder or not 1 <= providers <= MAX_PUT_PROVIDERS:
            raise ValueError(
                f"STORAGE_PUT provider data must be 1 to {MAX_PUT_PROVIDERS} "
                f"payloads of {PEER_INFO_BYTES} bytes"
            )
        if not self.entries:
            raise ValueError("STORAGE_PUT requires at least one entry")
        if len(self.entries) > 0xFFFF:
            raise ValueError("STORAGE_PUT entry count exceeds u16")
        parts = [
            bytes([self.code.value, providers]),
            self.data,
            len(self.entries).to_bytes(2, "big"),
        ]
        for expr_id, payload_type in self.entries:
            if len(expr_id) != 32:
                raise ValueError("STORAGE_PUT expr_id must be 32 bytes")
            parts.append(expr_id + bytes([payload_type]))
        body = b"".join(parts)
        if len(body) > MAX_INLINE_MESSAGE_BYTES:
            raise ValueError(
                f"STORAGE_PUT too large ({len(body)} > {MAX_INLINE_MESSAGE_BYTES})"
            )
        return body

    @classmethod
    def from_bytes(cls, data: bytes) -> "StorageRequest":
        """Decode a storage request from bytes.

        Args:
            data (bytes): The encoded request bytes.

        Returns:
            StorageRequest: The decoded storage request.
        """
        if len(data) < 1:
            raise ValueError("Too short for StorageRequest (0 bytes)")

        type_val = data[0]
        try:
            req_type = StorageRequestCode(type_val)
        except ValueError:
            raise ValueError(f"Unknown StorageRequestCode: {type_val!r}")

        if req_type == StorageRequestCode.STORAGE_PUT:
            return cls._put_from_bytes(data)

        if req_type == StorageRequestCode.STORAGE_GET:
            return cls._get_from_bytes(data)

        if req_type == StorageRequestCode.STORAGE_INDEX_REQUEST:
            if len(data) != 1:
                raise ValueError(
                    f"STORAGE_INDEX_REQUEST must be 1 byte ({len(data)} bytes)"
                )
            return cls(code=req_type)
        raise ValueError(f"Unsupported StorageRequestCode: {req_type!r}")

    @classmethod
    def _get_from_bytes(cls, data: bytes) -> "StorageRequest":
        """Decode a STORAGE_GET request from bytes.

        Args:
            data (bytes): The encoded STORAGE_GET bytes.

        Returns:
            StorageRequest: The decoded storage request.
        """
        if len(data) > MAX_INLINE_MESSAGE_BYTES:
            raise ValueError(
                f"STORAGE_GET too large ({len(data)} > {MAX_INLINE_MESSAGE_BYTES})"
            )
        if len(data) < 1 + GET_HEADER_BYTES:
            raise ValueError(f"STORAGE_GET too short ({len(data)} bytes)")
        count = int.from_bytes(data[1 : 1 + GET_HEADER_BYTES], "big")
        if count < 1:
            raise ValueError("STORAGE_GET count must be at least 1")
        body = data[1 + GET_HEADER_BYTES :]
        entries: list[tuple[bytes, int, bytes | None, bytes]] = []
        pos = 0
        for _ in range(count):
            if pos + GET_ENTRY_BYTES > len(body):
                raise ValueError(
                    f"STORAGE_GET length mismatch (count={count}, body={len(body)} bytes)"
                )
            expr_id = bytes(body[pos : pos + 32])
            resolution = body[pos + 32]
            pos += GET_ENTRY_BYTES
            block_hash = None
            if resolution & SCOPED_FLAG:
                if pos + 32 > len(body):
                    raise ValueError("STORAGE_GET truncated scoped entry")
                resolution &= ~SCOPED_FLAG
                block_hash = bytes(body[pos : pos + 32])
                pos += 32
            shape = b""
            if resolution == RESOLUTION_DYNAMIC:
                length, pos = _read_varint(body, pos)
                if pos + length > len(body):
                    raise ValueError("STORAGE_GET truncated shape")
                shape = bytes(body[pos : pos + length])
                pos += length
                parse_shape(shape)
            elif resolution == RESOLUTION_RADIX:
                start = pos
                _, _, pos = read_radix_param(body, pos)
                shape = bytes(body[start:pos])
            entries.append((expr_id, resolution, block_hash, shape))
        if pos != len(body):
            raise ValueError(
                f"STORAGE_GET length mismatch (count={count}, body={len(body)} bytes)"
            )
        return cls(StorageRequestCode.STORAGE_GET, entries=entries)

    @classmethod
    def _put_from_bytes(cls, data: bytes) -> "StorageRequest":
        """Decode a STORAGE_PUT request from bytes.

        Args:
            data (bytes): The encoded STORAGE_PUT bytes.

        Returns:
            StorageRequest: The decoded storage request.
        """
        if len(data) > MAX_INLINE_MESSAGE_BYTES:
            raise ValueError(
                f"STORAGE_PUT too large ({len(data)} > {MAX_INLINE_MESSAGE_BYTES})"
            )
        if len(data) < 2:
            raise ValueError(f"STORAGE_PUT too short ({len(data)} bytes)")
        providers = data[1]
        if not 1 <= providers <= MAX_PUT_PROVIDERS:
            raise ValueError(f"STORAGE_PUT provider count must be 1 to {MAX_PUT_PROVIDERS}")
        header_end = 1 + batch_header_bytes(providers)
        if len(data) < header_end:
            raise ValueError(f"STORAGE_PUT too short ({len(data)} bytes)")
        provider_end = 2 + providers * PEER_INFO_BYTES
        provider_payload = data[2:provider_end]
        count = int.from_bytes(data[provider_end:header_end], "big")
        if count < 1:
            raise ValueError("STORAGE_PUT count must be at least 1")
        body = data[header_end:]
        if len(body) != count * BATCH_ENTRY_BYTES:
            raise ValueError(
                f"STORAGE_PUT length mismatch (count={count}, body={len(body)} bytes)"
            )
        entries = [
            (bytes(body[i : i + 32]), body[i + 32])
            for i in range(0, len(body), BATCH_ENTRY_BYTES)
        ]
        return cls(
            StorageRequestCode.STORAGE_PUT,
            provider_payload,
            entries=entries,
        )
