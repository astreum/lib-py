
def connect_node(astreum_node):
    """Initialize communication and consensus components, then load latest block state."""
    if astreum_node.is_connected:
        astreum_node.logger.debug("Node already connected; skipping communication setup")
        return

    astreum_node.logger.info("Starting communication and consensus setup")
    try:
        from astreum.communication import communication_setup  # type: ignore
        communication_setup(node=astreum_node, config=astreum_node.config)
        astreum_node.logger.info("Communication setup completed")
    except Exception as exc:
        astreum_node.logger.exception("Communication setup failed: %s", exc)
        return exc

    # Load latest_block_hash from config
    latest_block_hex = astreum_node.config.latest_block_hash
    verified_up_to_hex = astreum_node.config.verified_up_to

    if latest_block_hex and astreum_node.latest_block_hash is None:
        try:
            from astreum.utils.bytes import hex_to_bytes

            astreum_node.latest_block_hash = hex_to_bytes(
                latest_block_hex, expected_length=32
            )
            astreum_node.logger.debug("Loaded latest_block_hash override from config")
        except Exception as exc:
            astreum_node.logger.error("Invalid latest_block_hash in config: %s", exc)

    if verified_up_to_hex and getattr(astreum_node, "verified_up_to", None) is None:
        try:
            from astreum.utils.bytes import hex_to_bytes

            astreum_node.verified_up_to = hex_to_bytes(
                verified_up_to_hex, expected_length=32
            )
            astreum_node.logger.debug("Loaded verified_up_to override from config")
        except Exception as exc:
            astreum_node.logger.error("Invalid verified_up_to in config: %s", exc)

    if astreum_node.latest_block_hash and astreum_node.latest_block is None:
        try:
            from astreum.consensus.block.encoding.decode import get_block_from_storage
            astreum_node.latest_block = get_block_from_storage(astreum_node=astreum_node, block_hash=astreum_node.latest_block_hash)
            astreum_node.logger.info("Loaded latest block %s from storage", astreum_node.latest_block_hash.hex())
        except Exception as exc:
            astreum_node.logger.warning("Could not load latest block from storage: %s", exc)
