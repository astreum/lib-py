
from pathlib import Path
from typing import Dict, Optional
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

DEFAULT_HOT_STORAGE_LIMIT = 1 << 30  # 1 GiB
DEFAULT_COLD_STORAGE_LIMIT = 10 << 30  # 10 GiB
DEFAULT_COLD_STORAGE_SCALE = "MB"
DEFAULT_PORT = 52780
DEFAULT_LOGGING_ENABLED = True
DEFAULT_LOGGING_RETENTION_DAYS = 7
DEFAULT_PEER_TIMEOUT_SECONDS = 15 * 60  # 15 minutes
DEFAULT_PEER_TIMEOUT_INTERVAL_SECONDS = 10  # 10 seconds
DEFAULT_BOOTSTRAP_RETRY_INTERVAL_SECONDS = 30  # 30 seconds
DEFAULT_STORAGE_INDEX_INTERVAL_SECONDS = 600  # 10 minutes
DEFAULT_SELF_LOOKUP_ROUNDS = 8  # most route requests in the join walk; 0 turns the walk off
DEFAULT_SELF_LOOKUP_INTERVAL_SECONDS = 900  # 15 minutes between walks; 0 turns the timer off
DEFAULT_STORAGE_REQUEST_MINIMUM_PRICE = 1
DEFAULT_STORAGE_REQUEST_PRICE_INTERVAL_SECONDS = 5.0
DEFAULT_STORAGE_FETCH_INTERVAL_SECONDS = 0.25
DEFAULT_STORAGE_FETCH_RETRIES = 8
DEFAULT_EXPR_REQUEST_TTL_SECONDS = 4.0
DEFAULT_LONG_TERM_STORAGE_INTERVAL_SECONDS = 5.0
DEFAULT_INCOMING_QUEUE_SIZE_LIMIT_BYTES = 64 * 1024 * 1024  # 64 MiB
DEFAULT_INCOMING_QUEUE_TIMEOUT_SECONDS = 1.0
DEFAULT_FAIR_USE_LIMIT_BYTES = 1 << 20  # 1 MiB
DEFAULT_FAIR_USE_RATIO = 0.5
DEFAULT_SEED = "bootstrap.astreum.org:52780"
DEFAULT_VERIFICATION_MAX_STALE_SECONDS = 10
DEFAULT_VERIFICATION_MAX_FUTURE_SKEW_SECONDS = 2
DEFAULT_MESSAGE_TIMESTAMP_WINDOW_SECONDS = 60
# 1280 (IPv6 minimum MTU) - 40 (IPv6 header) - 8 (UDP header): a datagram of
# this size is never IP-fragmented on any IPv4/IPv6 path.
DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES = 1232
DEFAULT_STORAGE_GET_BATCH_MAX_BYTES = 1232
# How often buffered STORAGE_GETs are batched and sent.
DEFAULT_STORAGE_REQUEST_FLUSH_INTERVAL_SECONDS = 0.25
# Most pages one STORAGE_FOUND reply may span (about 4 MiB at ~65 KB a page).
DEFAULT_STORAGE_FOUND_MAX_PAGES = 64


def _parse_int(name: str, raw, minimum: Optional[int] = None) -> int:
    """Convert a raw config value to an integer.

    Args:
        name: Config key name, used in error messages.
        raw: Raw value from the caller.
        minimum: ``None`` for no range check, ``0`` to require a non-negative
            value, or ``1`` to require a positive value.

    Returns:
        The integer value.

    Raises:
        ValueError: If ``raw`` is not an integer or is below ``minimum``.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer: {raw!r}") from exc
    if minimum == 0 and value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    if minimum == 1 and value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parse_float(name: str, raw, allow_zero: bool) -> float:
    """Convert a raw config value to a float.

    Args:
        name: Config key name, used in error messages.
        raw: Raw value from the caller.
        allow_zero: ``True`` to accept 0, ``False`` to require a positive value.

    Returns:
        The float value.

    Raises:
        ValueError: If ``raw`` is not a number or is out of range.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number: {raw!r}") from exc
    if allow_zero and value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    if not allow_zero and value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _parse_bool(name: str, raw) -> bool:
    """Check that a raw config value is a boolean.

    Args:
        name: Config key name, used in error messages.
        raw: Raw value from the caller.

    Returns:
        The boolean value.

    Raises:
        ValueError: If ``raw`` is not a ``bool``.
    """
    if not isinstance(raw, bool):
        raise ValueError(f"{name} must be a boolean")
    return raw


def _setup_chain(config: Dict) -> None:
    """Normalize ``chain`` and ``chain_id``."""
    chain_str = config.get("chain")
    if chain_str not in {"main", "test"}:
        chain_str = None
    chain_id_raw = config.get("chain_id")
    if chain_id_raw is None:
        chain_id = 1 if chain_str == "main" else 0
    else:
        try:
            chain_id = int(chain_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"chain_id must be an integer: {chain_id_raw!r}"
            ) from exc
    if chain_str is None:
        chain_str = "main" if chain_id == 1 else "test"
    config["chain"] = chain_str
    config["chain_id"] = chain_id


def _setup_storage_limits(config: Dict) -> None:
    """Normalize hot and cold storage keys and create the cold directory."""
    config["hot_storage_limit"] = _parse_int(
        "hot_storage_limit",
        config.get(
            "hot_storage_limit",
            config.get("hot_storage_default_limit", DEFAULT_HOT_STORAGE_LIMIT),
        ),
    )
    config["cold_storage_limit"] = _parse_int(
        "cold_storage_limit",
        config.get("cold_storage_limit", DEFAULT_COLD_STORAGE_LIMIT),
    )

    cold_scale_raw = config.get("cold_storage_scale", DEFAULT_COLD_STORAGE_SCALE)
    if isinstance(cold_scale_raw, str):
        cold_scale = cold_scale_raw.strip().upper()
    else:
        raise ValueError("cold_storage_scale must be a string")
    scale_bytes = {"KB": 1_000, "MB": 1_000_000, "GB": 1_000_000_000}
    if cold_scale not in scale_bytes:
        raise ValueError("cold_storage_scale must be one of: KB, MB, GB")
    config["cold_storage_scale"] = cold_scale
    config["cold_storage_base_size"] = scale_bytes[cold_scale]

    cold_path_raw = config.get("cold_storage_path")
    if cold_path_raw:
        try:
            path_obj = Path(cold_path_raw)
            path_obj.mkdir(parents=True, exist_ok=True)
            config["cold_storage_path"] = str(path_obj)
        except OSError:
            config["cold_storage_path"] = None
    else:
        config["cold_storage_path"] = None


def _setup_logging(config: Dict) -> None:
    """Normalize logging keys."""
    config["logging_retention_days"] = _parse_int(
        "logging_retention_days",
        config.get(
            "logging_retention_days",
            config.get(
                "logging_retention",
                config.get("retention_days", DEFAULT_LOGGING_RETENTION_DAYS),
            ),
        ),
    )
    config["logging_enabled"] = _parse_bool(
        "logging_enabled", config.get("logging_enabled", DEFAULT_LOGGING_ENABLED)
    )


def _setup_network(config: Dict) -> None:
    """Normalize port, queue, peer, bootstrap and self lookup keys."""
    config["port"] = _parse_int("port", config.get("port", DEFAULT_PORT))
    if config["port"] < 0:
        raise ValueError("port must be 0 or a positive integer")

    config["incoming_queue_size_limit"] = _parse_int(
        "incoming_queue_size_limit",
        config.get("incoming_queue_size_limit", DEFAULT_INCOMING_QUEUE_SIZE_LIMIT_BYTES),
        0,
    )
    config["incoming_queue_timeout"] = _parse_float(
        "incoming_queue_timeout",
        config.get("incoming_queue_timeout", DEFAULT_INCOMING_QUEUE_TIMEOUT_SECONDS),
        True,
    )
    config["peer_timeout"] = _parse_int(
        "peer_timeout", config.get("peer_timeout", DEFAULT_PEER_TIMEOUT_SECONDS), 1
    )
    interval = _parse_int(
        "peer_timeout_interval",
        config.get("peer_timeout_interval", DEFAULT_PEER_TIMEOUT_INTERVAL_SECONDS),
        1,
    )
    config["peer_timeout_interval"] = interval
    config["verify_blockchain_interval"] = _parse_float(
        "verify_blockchain_interval",
        config.get("verify_blockchain_interval", interval),
        False,
    )
    config["bootstrap_retry_interval"] = _parse_int(
        "bootstrap_retry_interval",
        config.get("bootstrap_retry_interval", DEFAULT_BOOTSTRAP_RETRY_INTERVAL_SECONDS),
        1,
    )
    config["self_lookup_rounds"] = _parse_int(
        "self_lookup_rounds",
        config.get("self_lookup_rounds", DEFAULT_SELF_LOOKUP_ROUNDS),
        0,
    )
    config["self_lookup_interval"] = _parse_int(
        "self_lookup_interval",
        config.get("self_lookup_interval", DEFAULT_SELF_LOOKUP_INTERVAL_SECONDS),
        0,
    )


def _setup_storage_requests(config: Dict) -> None:
    """Normalize storage index, price, fetch, provider and batch keys."""
    config["storage_index_interval"] = _parse_int(
        "storage_index_interval",
        config.get("storage_index_interval", DEFAULT_STORAGE_INDEX_INTERVAL_SECONDS),
        1,
    )
    config["storage_request_minimum_price"] = _parse_int(
        "storage_request_minimum_price",
        config.get("storage_request_minimum_price", DEFAULT_STORAGE_REQUEST_MINIMUM_PRICE),
        0,
    )
    config["storage_request_price_interval"] = _parse_float(
        "storage_request_price_interval",
        config.get(
            "storage_request_price_interval", DEFAULT_STORAGE_REQUEST_PRICE_INTERVAL_SECONDS
        ),
        False,
    )
    storage_fetch_interval = _parse_float(
        "storage_fetch_interval",
        config.get("storage_fetch_interval", DEFAULT_STORAGE_FETCH_INTERVAL_SECONDS),
        True,
    )
    config["storage_fetch_interval"] = storage_fetch_interval
    storage_fetch_retries = _parse_int(
        "storage_fetch_retries",
        config.get("storage_fetch_retries", DEFAULT_STORAGE_FETCH_RETRIES),
        0,
    )
    config["storage_fetch_retries"] = storage_fetch_retries

    expr_request_ttl_raw = config.get("expr_request_ttl")
    if expr_request_ttl_raw is None:
        expr_request_ttl = 2.0 * storage_fetch_retries * storage_fetch_interval
        if expr_request_ttl <= 0:
            expr_request_ttl = DEFAULT_EXPR_REQUEST_TTL_SECONDS
    else:
        if isinstance(expr_request_ttl_raw, bool) or not isinstance(
            expr_request_ttl_raw, (int, float)
        ):
            raise ValueError(
                f"expr_request_ttl must be a number: {expr_request_ttl_raw!r}"
            )
        expr_request_ttl = float(expr_request_ttl_raw)
        if expr_request_ttl <= 0:
            raise ValueError("expr_request_ttl must be a positive number")
    config["expr_request_ttl"] = expr_request_ttl

    config["is_storage_provider"] = _parse_bool(
        "is_storage_provider", config.get("is_storage_provider", False)
    )
    config["long_term_storage_interval"] = _parse_float(
        "long_term_storage_interval",
        config.get(
            "long_term_storage_interval", DEFAULT_LONG_TERM_STORAGE_INTERVAL_SECONDS
        ),
        False,
    )

    from astreum.communication.message_pow import MAX_UDP_DATAGRAM_BYTES
    from astreum.communication.storage_request.model import (
        max_batch_entries,
        max_get_entries,
    )

    batch_budget_raw = config.get(
        "storage_put_batch_max_bytes", DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES
    )
    if isinstance(batch_budget_raw, bool):
        raise ValueError(
            f"storage_put_batch_max_bytes must be an integer: {batch_budget_raw!r}"
        )
    batch_budget = _parse_int("storage_put_batch_max_bytes", batch_budget_raw)
    if batch_budget > MAX_UDP_DATAGRAM_BYTES:
        raise ValueError(
            f"storage_put_batch_max_bytes must be at most {MAX_UDP_DATAGRAM_BYTES}"
        )
    if max_batch_entries(batch_budget) < 2:
        raise ValueError(
            "storage_put_batch_max_bytes too small to hold at least 2 entries"
        )
    config["storage_put_batch_max_bytes"] = batch_budget

    get_budget_raw = config.get(
        "storage_get_batch_max_bytes", DEFAULT_STORAGE_GET_BATCH_MAX_BYTES
    )
    if isinstance(get_budget_raw, bool):
        raise ValueError(
            f"storage_get_batch_max_bytes must be an integer: {get_budget_raw!r}"
        )
    get_budget = _parse_int("storage_get_batch_max_bytes", get_budget_raw)
    if get_budget > MAX_UDP_DATAGRAM_BYTES:
        raise ValueError(
            f"storage_get_batch_max_bytes must be at most {MAX_UDP_DATAGRAM_BYTES}"
        )
    if max_get_entries(get_budget) < 2:
        raise ValueError(
            "storage_get_batch_max_bytes too small to hold at least 2 entries"
        )
    config["storage_get_batch_max_bytes"] = get_budget

    flush_interval_raw = config.get(
        "storage_request_flush_interval",
        DEFAULT_STORAGE_REQUEST_FLUSH_INTERVAL_SECONDS,
    )
    if isinstance(flush_interval_raw, bool):
        raise ValueError(
            f"storage_request_flush_interval must be a number: {flush_interval_raw!r}"
        )
    config["storage_request_flush_interval"] = _parse_float(
        "storage_request_flush_interval", flush_interval_raw, False
    )

    found_pages_raw = config.get(
        "storage_found_max_pages", DEFAULT_STORAGE_FOUND_MAX_PAGES
    )
    if isinstance(found_pages_raw, bool) or not isinstance(found_pages_raw, int):
        raise ValueError(
            f"storage_found_max_pages must be an integer: {found_pages_raw!r}"
        )
    if found_pages_raw <= 0:
        raise ValueError("storage_found_max_pages must be a positive integer")
    if found_pages_raw > 0xFFFF:
        raise ValueError("storage_found_max_pages must be at most 65535")
    config["storage_found_max_pages"] = found_pages_raw


def _setup_verification(config: Dict) -> None:
    """Normalize fair use, verification and message timestamp keys."""
    config["fair_use_limit"] = _parse_int(
        "fair_use_limit", config.get("fair_use_limit", DEFAULT_FAIR_USE_LIMIT_BYTES), 0
    )
    config["fair_use_ratio"] = _parse_float(
        "fair_use_ratio", config.get("fair_use_ratio", DEFAULT_FAIR_USE_RATIO), True
    )
    config["verification_max_stale_seconds"] = _parse_int(
        "verification_max_stale_seconds",
        config.get(
            "verification_max_stale_seconds", DEFAULT_VERIFICATION_MAX_STALE_SECONDS
        ),
        0,
    )
    config["verification_max_future_skew"] = _parse_int(
        "verification_max_future_skew",
        config.get(
            "verification_max_future_skew", DEFAULT_VERIFICATION_MAX_FUTURE_SKEW_SECONDS
        ),
        0,
    )
    config["message_timestamp_window"] = _parse_int(
        "message_timestamp_window",
        config.get("message_timestamp_window", DEFAULT_MESSAGE_TIMESTAMP_WINDOW_SECONDS),
        0,
    )


def _setup_seeds(config: Dict) -> None:
    """Normalize seed and verified head keys."""
    if "default_seeds" in config:
        raise ValueError("default_seeds is no longer supported; use default_seed")

    if "default_seed" in config:
        default_seed_raw = config["default_seed"]
    else:
        default_seed_raw = DEFAULT_SEED

    if default_seed_raw is None:
        config["default_seed"] = None
    elif isinstance(default_seed_raw, str):
        config["default_seed"] = default_seed_raw
    else:
        raise ValueError("default_seed must be a string or None")

    additional_seeds_raw = config.get("additional_seeds", [])
    if isinstance(additional_seeds_raw, (list, tuple)):
        config["additional_seeds"] = list(additional_seeds_raw)
    else:
        raise ValueError("additional_seeds must be a list of strings")

    verified_up_to_raw = config.get("verified_up_to")
    if verified_up_to_raw in (None, ""):
        config["verified_up_to"] = None
    elif isinstance(verified_up_to_raw, str):
        config["verified_up_to"] = verified_up_to_raw
    else:
        raise ValueError("verified_up_to must be a hex string or None")


def _setup_keys(config: Dict) -> None:
    """Load or generate secret keys and derive public keys.

    Reads and may write ``storage.txt`` in the user data directory.
    """
    validation_secret_key_str_raw = config.get("validation_secret_key_str")
    if validation_secret_key_str_raw and isinstance(validation_secret_key_str_raw, str):
        secret_bytes = bytes.fromhex(validation_secret_key_str_raw.strip())
        config["validation_secret_key"] = ed25519.Ed25519PrivateKey.from_private_bytes(
            secret_bytes
        )

    validation_secret_key_raw = config.get("validation_secret_key")
    if validation_secret_key_raw in (None, ""):
        config["validation_secret_key"] = None
    elif isinstance(validation_secret_key_raw, ed25519.Ed25519PrivateKey):
        config["validation_secret_key"] = validation_secret_key_raw
    elif isinstance(validation_secret_key_raw, str):
        validation_secret_key = validation_secret_key_raw.strip()
        try:
            validation_secret_key_bytes = bytes.fromhex(validation_secret_key)
        except ValueError as exc:
            raise ValueError("validation_secret_key must be hex-encoded bytes") from exc
        try:
            config["validation_secret_key"] = ed25519.Ed25519PrivateKey.from_private_bytes(
                validation_secret_key_bytes
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "validation_secret_key must be a valid raw Ed25519 private key (32-byte hex)"
            ) from exc
    else:
        raise ValueError(
            "validation_secret_key must be a hex string, Ed25519 private key, or None"
        )

    storage_secret_key_raw = config.get("storage_secret_key")

    if storage_secret_key_raw is None or storage_secret_key_raw == "":
        from astreum.utils.data import ensure_data_dir
        key_path = ensure_data_dir() / "accounts" / "storage.txt"
        if key_path.exists():
            storage_secret_key_raw = key_path.read_text().strip()
            if storage_secret_key_raw.startswith("0x") or storage_secret_key_raw.startswith("0X"):
                storage_secret_key_raw = storage_secret_key_raw[2:]
        else:
            key = ed25519.Ed25519PrivateKey.generate()
            key_path.write_text("0x" + key.private_bytes_raw().hex())
            config["storage_secret_key"] = key

    if isinstance(storage_secret_key_raw, ed25519.Ed25519PrivateKey):
        config["storage_secret_key"] = storage_secret_key_raw
    elif isinstance(storage_secret_key_raw, str):
        secret_key_str = storage_secret_key_raw.strip()
        try:
            storage_secret_key_bytes = bytes.fromhex(secret_key_str)
        except ValueError as exc:
            raise ValueError("storage_secret_key must be hex-encoded bytes") from exc
        try:
            config["storage_secret_key"] = ed25519.Ed25519PrivateKey.from_private_bytes(
                storage_secret_key_bytes
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "storage_secret_key must be a valid raw Ed25519 private key (32-byte hex)"
            ) from exc
    elif config.get("storage_secret_key") is None:
        raise ValueError(
            "storage_secret_key must be a hex string or Ed25519PrivateKey"
        )

    storage_public_key = config["storage_secret_key"].public_key()
    config["storage_public_key"] = storage_public_key
    config["storage_public_key_bytes"] = storage_public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    validation_public_key = (
        config["validation_secret_key"].public_key()
        if config.get("validation_secret_key") is not None
        else None
    )
    config["validation_public_key"] = validation_public_key
    config["validation_public_key_bytes"] = (
        validation_public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if validation_public_key is not None
        else None
    )


def config_setup(config: Optional[Dict] = None):
    """
    Normalize configuration values before the node starts.

    Args:
        config: Configuration dictionary to normalize. Modified in place. When
            ``None``, a new dictionary is used.

    Returns:
        The normalized configuration dictionary.
    """
    if config is None:
        config = {}
    _setup_chain(config)
    _setup_storage_limits(config)
    _setup_logging(config)
    _setup_network(config)
    _setup_storage_requests(config)
    _setup_verification(config)
    _setup_seeds(config)
    _setup_keys(config)
    return config
