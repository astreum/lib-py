from typing import Tuple

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from astreum import Node


def address_str_to_host_and_port(address: str) -> Tuple[str, int]:
    """Parse `host:port` (or `[ipv6]:port`) into a tuple.

    Args:
        address (str): The address string to parse.

    Returns:
        Tuple[str, int]: Description of the return value.
    """
    addr = address.strip()
    if not addr:
        raise ValueError("address cannot be empty")

    host: str
    port_str: str

    if addr.startswith('['):
        end = addr.find(']')
        if end == -1:
            raise ValueError("missing closing ']' in IPv6 address")
        host = addr[1:end]
        remainder = addr[end + 1 :]
        if not remainder.startswith(':'):
            raise ValueError("missing port separator after IPv6 address")
        port_str = remainder[1:]
    else:
        if ':' not in addr:
            raise ValueError("address must contain ':' separating host and port")
        host, port_str = addr.rsplit(':', 1)

    host = host.strip()
    if not host:
        raise ValueError("host cannot be empty")
    port_str = port_str.strip()
    if not port_str:
        raise ValueError("port cannot be empty")

    try:
        port = int(port_str, 10)
    except ValueError as exc:
        raise ValueError(f"invalid port number: {port_str}") from exc

    if not (0 < port < 65536):
        raise ValueError(f"port out of range: {port}")

    return host, port


def xor_distance(a: bytes, b: bytes) -> int:
    """Return the unsigned integer XOR distance between two equal-length identifiers.

    Args:
        a (bytes): The first identifier.
        b (bytes): The second identifier.

    Returns:
        int: Description of the return value.
    """
    if len(a) != len(b):
        raise ValueError("xor distance requires operands of equal length")
    return int.from_bytes(bytes(x ^ y for x, y in zip(a, b)), "big", signed=False)


def get_bootstrap_peers(node: "Node") -> list[str]:
    """Get the list of bootstrap peer addresses from node config.

    Args:
        node (Node): The node instance to read config from.

    Returns:
        list[str]: The list of bootstrap peer addresses.
    """
    default_seed = node.config["default_seed"]
    additional_seeds = node.config["additional_seeds"]
    peers = []
    if default_seed is not None:
        peers.append(default_seed)
    peers.extend(additional_seeds)
    return peers
