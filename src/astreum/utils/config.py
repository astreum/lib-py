
from pathlib import Path
from typing import Dict
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
DEFAULT_STORAGE_REQUEST_MINIMUM_PRICE = 1
DEFAULT_STORAGE_REQUEST_PRICE_INTERVAL_SECONDS = 5.0
DEFAULT_STORAGE_FETCH_INTERVAL_SECONDS = 0.25
DEFAULT_STORAGE_FETCH_RETRIES = 8
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


def config_setup(config: Dict = {}):
    """
    Normalize configuration values before the node starts.
    """
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

    hot_limit_raw = config.get(
        "hot_storage_limit", config.get("hot_storage_default_limit", DEFAULT_HOT_STORAGE_LIMIT)
    )
    try:
        config["hot_storage_limit"] = int(hot_limit_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"hot_storage_limit must be an integer: {hot_limit_raw!r}"
        ) from exc

    cold_limit_raw = config.get("cold_storage_limit", DEFAULT_COLD_STORAGE_LIMIT)
    try:
        config["cold_storage_limit"] = int(cold_limit_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"cold_storage_limit must be an integer: {cold_limit_raw!r}"
        ) from exc

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

    retention_raw = config.get(
        "logging_retention_days",
        config.get("logging_retention", config.get("retention_days", DEFAULT_LOGGING_RETENTION_DAYS)),
    )
    try:
        config["logging_retention_days"] = int(retention_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"logging_retention_days must be an integer: {retention_raw!r}"
        ) from exc

    logging_enabled_raw = config.get("logging_enabled", DEFAULT_LOGGING_ENABLED)
    if isinstance(logging_enabled_raw, bool):
        config["logging_enabled"] = logging_enabled_raw
    else:
        raise ValueError("logging_enabled must be a boolean")

    if "port" in config:
        port_raw = config["port"]
    else:
        port_raw = DEFAULT_PORT
    try:
        config["port"] = int(port_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"port must be an integer: {port_raw!r}"
        ) from exc
    if config["port"] < 0:
        raise ValueError("port must be 0 or a positive integer")

    incoming_queue_limit_raw = config.get(
        "incoming_queue_size_limit", DEFAULT_INCOMING_QUEUE_SIZE_LIMIT_BYTES
    )
    try:
        incoming_queue_limit = int(incoming_queue_limit_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"incoming_queue_size_limit must be an integer: {incoming_queue_limit_raw!r}"
        ) from exc
    if incoming_queue_limit < 0:
        raise ValueError("incoming_queue_size_limit must be a non-negative integer")
    config["incoming_queue_size_limit"] = incoming_queue_limit

    incoming_queue_timeout_raw = config.get(
        "incoming_queue_timeout", DEFAULT_INCOMING_QUEUE_TIMEOUT_SECONDS
    )
    try:
        incoming_queue_timeout = float(incoming_queue_timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"incoming_queue_timeout must be a number: {incoming_queue_timeout_raw!r}"
        ) from exc
    if incoming_queue_timeout < 0:
        raise ValueError("incoming_queue_timeout must be a non-negative number")
    config["incoming_queue_timeout"] = incoming_queue_timeout

    peer_timeout_raw = config.get("peer_timeout", DEFAULT_PEER_TIMEOUT_SECONDS)
    try:
        peer_timeout = int(peer_timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"peer_timeout must be an integer: {peer_timeout_raw!r}"
        ) from exc

    if peer_timeout <= 0:
        raise ValueError("peer_timeout must be a positive integer")

    config["peer_timeout"] = peer_timeout

    interval_raw = config.get("peer_timeout_interval", DEFAULT_PEER_TIMEOUT_INTERVAL_SECONDS)
    try:
        interval = int(interval_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"peer_timeout_interval must be an integer: {interval_raw!r}"
        ) from exc

    if interval <= 0:
        raise ValueError("peer_timeout_interval must be a positive integer")

    config["peer_timeout_interval"] = interval
    verify_interval_raw = config.get("verify_blockchain_interval", interval)
    try:
        verify_interval = float(verify_interval_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"verify_blockchain_interval must be a number: {verify_interval_raw!r}"
        ) from exc
    if verify_interval <= 0:
        raise ValueError("verify_blockchain_interval must be a positive number")
    config["verify_blockchain_interval"] = verify_interval

    bootstrap_retry_raw = config.get(
        "bootstrap_retry_interval", DEFAULT_BOOTSTRAP_RETRY_INTERVAL_SECONDS
    )
    try:
        bootstrap_retry_interval = int(bootstrap_retry_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"bootstrap_retry_interval must be an integer: {bootstrap_retry_raw!r}"
        ) from exc
    if bootstrap_retry_interval <= 0:
        raise ValueError("bootstrap_retry_interval must be a positive integer")
    config["bootstrap_retry_interval"] = bootstrap_retry_interval

    storage_index_raw = config.get(
        "storage_index_interval", DEFAULT_STORAGE_INDEX_INTERVAL_SECONDS
    )
    try:
        storage_index_interval = int(storage_index_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"storage_index_interval must be an integer: {storage_index_raw!r}"
        ) from exc
    if storage_index_interval <= 0:
        raise ValueError("storage_index_interval must be a positive integer")
    config["storage_index_interval"] = storage_index_interval

    storage_request_minimum_price_raw = config.get(
        "storage_request_minimum_price", DEFAULT_STORAGE_REQUEST_MINIMUM_PRICE
    )
    try:
        storage_request_minimum_price = int(storage_request_minimum_price_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "storage_request_minimum_price must be an integer: "
            f"{storage_request_minimum_price_raw!r}"
        ) from exc
    if storage_request_minimum_price < 0:
        raise ValueError("storage_request_minimum_price must be a non-negative integer")
    config["storage_request_minimum_price"] = storage_request_minimum_price

    storage_request_price_interval_raw = config.get(
        "storage_request_price_interval", DEFAULT_STORAGE_REQUEST_PRICE_INTERVAL_SECONDS
    )
    try:
        storage_request_price_interval = float(storage_request_price_interval_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "storage_request_price_interval must be a number: "
            f"{storage_request_price_interval_raw!r}"
        ) from exc
    if storage_request_price_interval <= 0:
        raise ValueError("storage_request_price_interval must be a positive number")
    config["storage_request_price_interval"] = storage_request_price_interval

    storage_fetch_interval_raw = config.get(
        "storage_fetch_interval", DEFAULT_STORAGE_FETCH_INTERVAL_SECONDS
    )
    try:
        storage_fetch_interval = float(storage_fetch_interval_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"storage_fetch_interval must be a number: {storage_fetch_interval_raw!r}"
        ) from exc
    if storage_fetch_interval < 0:
        raise ValueError("storage_fetch_interval must be a non-negative number")
    config["storage_fetch_interval"] = storage_fetch_interval

    storage_fetch_retries_raw = config.get(
        "storage_fetch_retries", DEFAULT_STORAGE_FETCH_RETRIES
    )
    try:
        storage_fetch_retries = int(storage_fetch_retries_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"storage_fetch_retries must be an integer: {storage_fetch_retries_raw!r}"
        ) from exc
    if storage_fetch_retries < 0:
        raise ValueError("storage_fetch_retries must be a non-negative integer")
    config["storage_fetch_retries"] = storage_fetch_retries

    long_term_raw = config.get("long_term_storage", False)
    if not isinstance(long_term_raw, bool):
        raise ValueError("long_term_storage must be a boolean")
    config["long_term_storage"] = long_term_raw

    long_term_interval_raw = config.get(
        "long_term_storage_interval", DEFAULT_LONG_TERM_STORAGE_INTERVAL_SECONDS
    )
    try:
        long_term_interval = float(long_term_interval_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"long_term_storage_interval must be a number: {long_term_interval_raw!r}"
        ) from exc
    if long_term_interval <= 0:
        raise ValueError("long_term_storage_interval must be a positive number")
    config["long_term_storage_interval"] = long_term_interval

    batch_budget_raw = config.get(
        "storage_put_batch_max_bytes", DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES
    )
    if isinstance(batch_budget_raw, bool):
        raise ValueError(
            f"storage_put_batch_max_bytes must be an integer: {batch_budget_raw!r}"
        )
    try:
        batch_budget = int(batch_budget_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"storage_put_batch_max_bytes must be an integer: {batch_budget_raw!r}"
        ) from exc
    from astreum.communication.message_pow import MAX_UDP_DATAGRAM_BYTES
    from astreum.communication.storage_request.model import max_batch_entries

    if batch_budget > MAX_UDP_DATAGRAM_BYTES:
        raise ValueError(
            f"storage_put_batch_max_bytes must be at most {MAX_UDP_DATAGRAM_BYTES}"
        )
    if max_batch_entries(batch_budget) < 2:
        raise ValueError(
            "storage_put_batch_max_bytes too small to hold at least 2 entries"
        )
    config["storage_put_batch_max_bytes"] = batch_budget

    fair_use_limit_raw = config.get("fair_use_limit", DEFAULT_FAIR_USE_LIMIT_BYTES)
    try:
        fair_use_limit = int(fair_use_limit_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"fair_use_limit must be an integer: {fair_use_limit_raw!r}"
        ) from exc
    if fair_use_limit < 0:
        raise ValueError("fair_use_limit must be a non-negative integer")
    config["fair_use_limit"] = fair_use_limit

    fair_use_ratio_raw = config.get("fair_use_ratio", DEFAULT_FAIR_USE_RATIO)
    try:
        fair_use_ratio = float(fair_use_ratio_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"fair_use_ratio must be a number: {fair_use_ratio_raw!r}"
        ) from exc
    if fair_use_ratio < 0:
        raise ValueError("fair_use_ratio must be a non-negative number")
    config["fair_use_ratio"] = fair_use_ratio

    max_stale_raw = config.get(
        "verification_max_stale_seconds", DEFAULT_VERIFICATION_MAX_STALE_SECONDS
    )
    try:
        max_stale = int(max_stale_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"verification_max_stale_seconds must be an integer: {max_stale_raw!r}"
        ) from exc
    if max_stale < 0:
        raise ValueError("verification_max_stale_seconds must be a non-negative integer")
    config["verification_max_stale_seconds"] = max_stale

    max_future_raw = config.get(
        "verification_max_future_skew", DEFAULT_VERIFICATION_MAX_FUTURE_SKEW_SECONDS
    )
    try:
        max_future = int(max_future_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"verification_max_future_skew must be an integer: {max_future_raw!r}"
        ) from exc
    if max_future < 0:
        raise ValueError("verification_max_future_skew must be a non-negative integer")
    config["verification_max_future_skew"] = max_future

    timestamp_window_raw = config.get(
        "message_timestamp_window", DEFAULT_MESSAGE_TIMESTAMP_WINDOW_SECONDS
    )
    try:
        timestamp_window = int(timestamp_window_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"message_timestamp_window must be an integer: {timestamp_window_raw!r}"
        ) from exc
    if timestamp_window < 0:
        raise ValueError("message_timestamp_window must be a non-negative integer")
    config["message_timestamp_window"] = timestamp_window

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

    if "default_seed" not in config:
        config["default_seed"] = None

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

    return config
