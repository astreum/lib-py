from astreum.communication.message_pow import (
    MAX_INLINE_MESSAGE_BYTES,
    MESSAGE_FRAMING_BYTES,
)
from astreum.communication.storage_request.code import StorageRequestCode

# STORAGE_PUT layout (after the 1-byte request code):
#   provider_payload (70B) + count u16 + count * (expr_id 32B + payload_type 1B)
PROVIDER_PAYLOAD_BYTES = 70
BATCH_HEADER_BYTES = PROVIDER_PAYLOAD_BYTES + 2
BATCH_ENTRY_BYTES = 32 + 1


def max_batch_entries(budget_bytes: int) -> int:
    """Return how many entries fit in one STORAGE_PUT datagram of at most
    *budget_bytes* on the wire (message framing and request code included)."""
    return (budget_bytes - MESSAGE_FRAMING_BYTES - 1 - BATCH_HEADER_BYTES) // BATCH_ENTRY_BYTES


class StorageRequest:
    code: StorageRequestCode
    data: bytes
    expr_id: bytes
    payload_type: int | None
    entries: list[tuple[bytes, int]] | None

    def __init__(
        self,
        code: StorageRequestCode,
        data: bytes = b"",
        expr_id: bytes = None,
        payload_type: int | None = None,
        entries: list[tuple[bytes, int]] | None = None,
    ):
        self.code = code
        self.data = data
        self.expr_id = expr_id
        self.payload_type = payload_type
        self.entries = entries

    def to_bytes(self):
        if self.code == StorageRequestCode.STORAGE_PUT:
            return self._put_to_bytes()
        if self.payload_type is not None:
            payload = bytes([self.payload_type]) + self.data
        else:
            payload = self.data
        return bytes([self.code.value]) + self.expr_id + payload

    def _put_to_bytes(self) -> bytes:
        if len(self.data) != PROVIDER_PAYLOAD_BYTES:
            raise ValueError(
                f"STORAGE_PUT provider payload must be {PROVIDER_PAYLOAD_BYTES} bytes"
            )
        if not self.entries:
            raise ValueError("STORAGE_PUT requires at least one entry")
        if len(self.entries) > 0xFFFF:
            raise ValueError("STORAGE_PUT entry count exceeds u16")
        parts = [
            bytes([self.code.value]),
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
        # need at least 1 byte for type + 32 bytes for hash
        if len(data) < 1 + 32:
            raise ValueError(f"Too short for StorageRequest ({len(data)} bytes)")

        type_val = data[0]
        try:
            req_type = StorageRequestCode(type_val)
        except ValueError:
            raise ValueError(f"Unknown StorageRequestCode: {type_val!r}")

        if req_type == StorageRequestCode.STORAGE_PUT:
            return cls._put_from_bytes(data)

        expr_id_bytes = data[1:33]
        payload = data[33:]
        if req_type == StorageRequestCode.STORAGE_GET:
            if payload:
                payload_type = payload[0]
                payload = payload[1:]
            else:
                payload_type = None
            return cls(req_type, payload, expr_id_bytes, payload_type=payload_type)
        return cls(req_type, payload, expr_id_bytes)

    @classmethod
    def _put_from_bytes(cls, data: bytes) -> "StorageRequest":
        if len(data) > MAX_INLINE_MESSAGE_BYTES:
            raise ValueError(
                f"STORAGE_PUT too large ({len(data)} > {MAX_INLINE_MESSAGE_BYTES})"
            )
        if len(data) < 1 + BATCH_HEADER_BYTES:
            raise ValueError(f"STORAGE_PUT too short ({len(data)} bytes)")
        provider_payload = data[1 : 1 + PROVIDER_PAYLOAD_BYTES]
        count = int.from_bytes(data[1 + PROVIDER_PAYLOAD_BYTES : 1 + BATCH_HEADER_BYTES], "big")
        if count < 1:
            raise ValueError("STORAGE_PUT count must be at least 1")
        body = data[1 + BATCH_HEADER_BYTES :]
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
