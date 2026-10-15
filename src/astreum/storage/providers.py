from __future__ import annotations

from typing import Optional



def provider_id_for_payload(node, payload: bytes) -> int:
    """Return the provider id for a payload, inserting if new.

    Args:
        node: A Node instance with the ``storage_providers`` registry.
        payload: The encoded provider payload bytes.

    Returns:
        The provider id (index into ``node.storage_providers``).
    """
    for idx, existing in enumerate(node.storage_providers):
        if existing == payload:
            return idx
    node.storage_providers.append(payload)
    return len(node.storage_providers) - 1



def provider_payload_for_id(node, provider_id: int) -> Optional[bytes]:
    """Return the provider payload for a provider id, or None.

    Args:
        node: A Node instance with the ``storage_providers`` registry.
        provider_id: The provider id to look up.

    Returns:
        The encoded provider payload bytes, or ``None`` if the id is
        not a valid index into the registry.
    """
    if not isinstance(provider_id, int) or provider_id < 0:
        return None
    try:
        return node.storage_providers[provider_id]
    except IndexError:
        return None


def index_provider(node, expr_id: bytes, provider_id: int) -> None:
    """Add a provider to the index entry of ``expr_id``.

    The entry is a tuple of up to ``PROVIDERS_PER_RECORD`` provider ids,
    nearest to ``expr_id`` first. The index keeps one id per provider storage
    key. A new id from the same storage key replaces the old id. The whole
    tuple is replaced on each update, so a reader on another thread never sees
    a tuple change in place.

    Args:
        node: A Node instance with ``storage_index`` and ``storage_providers``.
        expr_id: The 32-byte content hash that the provider advertised.
        provider_id: The id of the provider payload in ``node.storage_providers``.

    Returns:
        None. A provider id with no payload, or a payload that does not
        decode, is dropped.
    """
    from astreum.communication.storage_response.storage_provider import (
        decode_storage_provider,
    )
    from astreum.communication.util import xor_distance
    from astreum.consensus.constants import PROVIDERS_PER_RECORD

    payload = provider_payload_for_id(node, provider_id)
    if payload is None:
        return
    try:
        key = decode_storage_provider(payload)[0]
    except ValueError:
        return

    kept: list[tuple[int, int]] = []
    for existing_id in node.storage_index.get(expr_id, ()):
        existing_payload = provider_payload_for_id(node, existing_id)
        if existing_payload is None:
            continue
        try:
            existing_key = decode_storage_provider(existing_payload)[0]
        except ValueError:
            continue
        if existing_key != key:
            kept.append((xor_distance(expr_id, existing_key), existing_id))
    kept.append((xor_distance(expr_id, key), provider_id))
    kept.sort(key=lambda item: item[0])
    node.storage_index[expr_id] = tuple(i for _, i in kept[:PROVIDERS_PER_RECORD])
