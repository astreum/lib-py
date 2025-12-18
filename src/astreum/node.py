"""Core Astreum Node implementation."""

from __future__ import annotations

import threading
import uuid
from typing import Dict

from astreum.communication.start import connect_to_network_and_verify
from astreum.communication.models.peer import (
    add_peer as peers_add_peer,
    replace_peer as peers_replace_peer,
    get_peer as peers_get_peer,
    remove_peer as peers_remove_peer,
)
from astreum.consensus.start import process_blocks_and_transactions
from astreum.machine import Expr, high_eval, low_eval, script_eval
from astreum.machine.models.environment import Env, env_get, env_set
from astreum.machine.models.expression import get_expr_list_from_storage
from astreum.storage.models.atom import get_atom_list_from_storage
from astreum.storage.actions.get import (
    _hot_storage_get,
    _cold_storage_get,
    _network_get,
    storage_get,
    local_get,
)
from astreum.storage.actions.set import (
    _hot_storage_set,
    _cold_storage_set,
    _network_set,
)
from astreum.storage.requests import add_atom_req, has_atom_req, pop_atom_req
from astreum.storage.setup import storage_setup
from astreum.utils.config import config_setup
from astreum.utils.logging import logging_setup


class Node:
    def __init__(self, config: dict = {}):
        self.config = config_setup(config=config)
        
        self.logger = logging_setup(self.config)

        self.logger.info("Starting Astreum Node")

        # Chain Configuration
        self.logger.info(f"Chain configured as: {self.config["chain"]} ({self.config["chain_id"]})")

        # Storage Setup
        storage_setup(self, config=self.config)

        # Machine Setup
        self.environments: Dict[uuid.UUID, Env] = {}
        self.machine_environments_lock = threading.RLock()
        self.is_connected = False
        self.latest_block_hash = None
        self.latest_block = None
        self.nonce_time_ms = 0  # rolling measurement of last nonce search duration
        
    connect = connect_to_network_and_verify
    validate = process_blocks_and_transactions

    low_eval = low_eval
    high_eval = high_eval
    script_eval = script_eval

    env_get = env_get
    env_set = env_set

    # Storage
    ## Get
    _hot_storage_get = _hot_storage_get
    _cold_storage_get = _cold_storage_get
    _network_get = _network_get

    ## Set
    _hot_storage_set = _hot_storage_set
    _cold_storage_set = _cold_storage_set
    _network_set = _network_set

    storage_get = storage_get
    local_get = local_get

    get_expr_list_from_storage = get_expr_list_from_storage
    get_atom_list_from_storage = get_atom_list_from_storage

    add_atom_req = add_atom_req
    has_atom_req = has_atom_req
    pop_atom_req = pop_atom_req

    add_peer = peers_add_peer
    replace_peer = peers_replace_peer
    get_peer = peers_get_peer
    remove_peer = peers_remove_peer
