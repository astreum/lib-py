"""Core Astreum Node implementation."""

from __future__ import annotations

from threading import Lock
from typing import Optional

from astreum.communication.util import get_bootstrap_peers
from astreum.storage.setup import setup_storage
from astreum.utils.config import config_setup
from astreum.utils.logging import logging_setup


class Node:
    def __init__(self, config: Optional[dict] = None):
        """Initialize an Astreum node with the given configuration.

        Args:
            config: Configuration dictionary for node settings. When ``None``,
                the default settings are used. The dictionary does not change.
                ``node.config`` holds the checked settings as a ``NodeConfig``.

        Returns: None
        """
        self.config = config_setup(config=config)
        self.bootstrap_peers = get_bootstrap_peers(self)
        
        self.logger = logging_setup(self.config)

        self.logger.info("Starting Astreum Node")

        # Chain Configuration
        self.logger.info(f"Chain configured as: {self.config.chain} ({self.config.chain_id})")

        # Storage Setup
        setup_storage(self, config=self.config)

        # Machine Setup
        self.is_connected = False
        self.latest_block_hash = None
        self.latest_block = None
        self.latest_block_lock = Lock()
