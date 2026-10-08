from __future__ import annotations

from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from astreum.expression import ZERO32
from astreum.utils.required import required_fields
from astreum.consensus.models.block import Block
from astreum.consensus.block.difficulty import calculate_block_difficulty
from astreum.consensus.block.utils.bits import count_leading_zero_bits


def _hex(value: Optional[bytes]) -> str:

    Args:
        value (Optional[bytes]): Description.

    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def verify_block_head(node: Any, block: Any) -> bool:
    """Verify block header fields, using attached previous block when available."""

    Args:
        node (Any): Description.
        block (Any): Description.

    base_required = [
        "expr_id",
        "previous_block_hash",
        "chain_id",
        "timestamp",
        "height",
    ]
    try:
        required_fields(block, base_required)
    except ValueError as exc:
        node.logger.debug("Block head verify failed %s block=%s", exc, _hex(None))
        return False

    block_hash = bytes(block.expr_id)
    prev_hash = block.previous_block_hash or ZERO32
    is_genesis = prev_hash == ZERO32
    previous_block: Optional[Block] = getattr(block, "previous_block", None)
    if not is_genesis and previous_block is None:
        node.logger.debug("Block head verify failed missing previous block=%s", _hex(prev_hash))
        return False

    if block.chain_id != node.config["chain_id"]:
        node.logger.debug(
            "Block head verify failed chain_id=%s expected=%s block=%s",
            block.chain_id,
            node.config["chain_id"],
            _hex(block_hash),
        )
        return False

    timestamp = block.timestamp
    if timestamp is None:
        node.logger.debug("Block head verify failed missing timestamp block=%s", _hex(block_hash))
        return False

    if is_genesis:
        if block.height not in (0,):
            node.logger.debug(
                "Block head verify failed genesis height=%s block=%s",
                block.height,
                _hex(block_hash),
            )
            return False
        return True

    extra_required = [
        "body_hash",
        "signature",
        "validator_public_key_bytes",
        "difficulty",
    ]
    try:
        required_fields(block, extra_required)
    except ValueError as exc:
        node.logger.debug("Block head verify failed %s block=%s", exc, _hex(block_hash))
        return False

    body_hash = block.body_hash
    signature = block.signature
    validator_public_key_bytes = block.validator_public_key_bytes
    if not body_hash or not signature or not validator_public_key_bytes:
        node.logger.debug("Block head verify failed missing body/signature/validator block=%s", _hex(block_hash))
        return False

    expected_height = (previous_block.height or 0) + 1
    if block.height != expected_height:
        node.logger.debug(
            "Block head verify failed height mismatch block=%s height=%s expected=%s",
            _hex(block_hash),
            block.height,
            expected_height,
        )
        return False

    previous_ts = previous_block.timestamp
    if previous_ts is not None and timestamp < previous_ts + 1:
        node.logger.debug("Block head verify failed timestamp block=%s ts=%s previous_ts=%s", _hex(block_hash), timestamp, previous_ts)
        return False

    try:
        pub = Ed25519PublicKey.from_public_bytes(validator_public_key_bytes)
        pub.verify(signature, body_hash)  # type: ignore[arg-type]
    except InvalidSignature:
        node.logger.debug("Block head verify failed signature block=%s", _hex(block_hash))
        return False
    except Exception:
        node.logger.debug("Block head verify failed signature error block=%s", _hex(block_hash))
        return False

    expected_diff = calculate_block_difficulty(
        previous_timestamp=previous_block.timestamp,
        current_timestamp=timestamp,
        previous_difficulty=previous_block.difficulty,
    )
    difficulty = block.difficulty
    if difficulty is None or difficulty != expected_diff:
        node.logger.debug("Block head verify failed difficulty block=%s diff=%s expected=%s", _hex(block_hash), difficulty, expected_diff)
        return False

    required_work = max(1, previous_block.difficulty or 1)
    if not block_hash:
        node.logger.debug("Block head verify failed missing hash block=%s", _hex(block_hash))
        return False
    if count_leading_zero_bits(block_hash) < required_work:
        node.logger.debug("Block head verify failed pow block=%s zeros=%s required=%s", _hex(block_hash), count_leading_zero_bits(block_hash), required_work)
        return False

    return True
