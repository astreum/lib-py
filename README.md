# lib

Python library to interact with the Astreum blockchain and its virtual machine.

[View on PyPI](https://pypi.org/project/astreum/)

## Configuration

When initializing an `astreum.Node`, pass a dictionary with any of the options below. Only the parameters you want to override need to be present – everything else falls back to its default.

### Core Configuration

| Parameter                   | Type       | Default        | Description                                                                                                                                                                      |
| --------------------------- | ---------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `hot_storage_limit`         | int        | `1073741824`   | Maximum bytes kept in the hot cache before new atoms are skipped (1 GiB).                                                                                                         |
| `cold_storage_limit`        | int        | `10737418240`  | Cold storage write threshold (10 GiB by default); set to `0` to skip the limit.                                                                                                   |
| `cold_storage_path`         | string     | `None`         | Directory where persisted atoms live; Astreum creates it on startup and skips cold storage when unset.                                                                            |
| `logging_retention_days`    | int        | `90`           | Number of days to keep rotated log files (daily gzip).                                                                                                                           |
| `chain_id`                  | int        | `0`            | Chain identifier used for validation (0 = test, 1 = main).                                                                                                                        |
| `verbose`                   | bool       | `False`        | When **True**, also mirror JSON logs to stdout with a human-readable format.                                                                                                     |

### Communication

| Parameter                | Type        | Default               | Description                                                                                             |
| ------------------------ | ----------- | --------------------- | ------------------------------------------------------------------------------------------------------- |
| `relay_secret_key`       | hex string  | Auto-generated        | X25519 private key used for the relay route; a new keypair is created when this field is omitted.        |
| `validation_secret_key`  | hex string  | `None`                | Optional Ed25519 key that lets the node join the validation route; leave blank to opt out of validation. |
| `use_ipv6`               | bool        | `False`               | Bind the incoming/outgoing sockets on IPv6 (the OS still listens on IPv4 if a peer speaks both).         |
| `incoming_port`          | int         | `52780`                | UDP port the relay binds to; pass `0` or omit to let the OS pick an ephemeral port.                       |
| `bootstrap`              | list\[str\] | `[]`                  | Addresses to ping with a handshake before joining; each must look like `host:port` or `[ipv6]:port`.     |
| `peer_timeout`           | int         | `900`                 | Evict peers that have not been seen within this many seconds (15 minutes).                               |
| `peer_timeout_interval`  | int         | `10`                  | How often (seconds) the peer manager checks for stale peers.                                             |

> **Note**
> The peer‑to‑peer *route* used for object discovery is always enabled.
> If `validation_secret_key` is provided the node automatically joins the validation route too.

### Example

```python
from astreum.node import Node

config = {
    "relay_secret_key": "ab…cd",             # optional – hex encoded
    "validation_secret_key": "12…34",        # optional – validator
    "hot_storage_limit": 1073741824,         # cap hot cache at 1 GiB
    "cold_storage_limit": 10737418240,       # cap cold storage at 10 GiB
    "cold_storage_path": "./data/node1",
    "incoming_port": 52780,
    "use_ipv6": False,
    "bootstrap": [
        "bootstrap.astreum.org:52780",
        "127.0.0.1:7374"
    ]
}

node = Node(config)
# … your code …
```


## Astreum Machine Quickstart

The Astreum virtual machine (VM) is embedded inside `astreum.Node`. You feed it Astreum script, and the node tokenizes, parses, and evaluates.

```python
# Define a named function int.add (stack body) and call it with bytes 1 and 2

import uuid
from astreum import Node, Env, Expr

# 1) Spin‑up a stand‑alone VM
node = Node()

# 2) Create an environment (simple manual setup)
env_id = uuid.uuid4()
node.environments[env_id] = Env()

# 3) Build a function value using a low‑level stack body via `sk`.
# Body does: $0 $1 add   (i.e., a + b)
low_body = Expr.ListExpr([
    Expr.Symbol("$0"),  # a (first arg)
    Expr.Symbol("$1"),  # b (second arg)
    Expr.Symbol("add"),
])

fn_body = Expr.ListExpr([
    Expr.Symbol("a"),
    Expr.Symbol("b"),
    Expr.ListExpr([low_body, Expr.Symbol("sk")]),
])

params = Expr.ListExpr([Expr.Symbol("a"), Expr.Symbol("b")])
int_add_fn = Expr.ListExpr([fn_body, params, Expr.Symbol("fn")])

# 4) Store under the name "int.add"
node.env_set(env_id, "int.add", int_add_fn)

# 5) Retrieve the function and call it with bytes 1 and 2
bound = node.env_get(env_id, "int.add")
call = Expr.ListExpr([Expr.Bytes(b"\x01"), Expr.Bytes(b"\x02"), bound])
res  = node.high_eval(env_id, call)

# sk returns a list of bytes; for 1+2 expect a single byte with value 3
print([int.from_bytes(b.value, 'big', signed=True) for b in res.elements])  # [3]
```

### Handling errors

Both helpers raise `ParseError` (from `astreum.machine.error`) when something goes wrong:

* Unterminated string literals are caught by `tokenize`.
* Unexpected or missing parentheses are caught by `parse`.

Catch the exception to provide developer‑friendly diagnostics:

```python
try:
    tokens = tokenize(bad_source)
    expr, _ = parse(tokens)
except ParseError as e:
    print("Parse failed:", e)
```

---


## Logging

Every `Node` instance wires up structured logging automatically:

- Logs land in per-instance files named `node.log` under `%LOCALAPPDATA%\Astreum\lib-py\logs/<instance_id>` on Windows and `$XDG_STATE_HOME` (or `~/.local/state`)/`Astreum/lib-py/logs/<instance_id>` on other platforms. The `<instance_id>` is the first 16 hex characters of a BLAKE3 hash of the caller's file path, so running the node from different entry points keeps their logs isolated.
- Files rotate at midnight UTC with gzip compression (`node-YYYY-MM-DD.log.gz`) and retain 90 days by default. Override via `config["logging_retention_days"]`.
- Each event is a single JSON line containing timestamp, level, logger, message, process/thread info, module/function, and the derived `instance_id`.
- Set `config["verbose"] = True` to mirror logs to stdout in a human-friendly format like `[2025-04-13-42-59] [info] Starting Astreum Node`.
- The very first entry emitted is the banner `Starting Astreum Node`, signalling that the logging pipeline is live before other subsystems spin up.

## Testing

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

for all tests
```
python3 -m unittest discover -s tests
```

for individual tests
```
python3 -m unittest tests.node.test_atom_get
python3 -m unittest tests.node.test_current_validator
python3 -m unittest tests.node.test_node_connection
python3 -m unittest tests.node.test_node_init
python3 -m unittest tests.node.test_node_validation
python3 -m unittest tests.node.tokenize
python3 -m unittest tests.node.parse
python3 -m unittest tests.node.function
python3 -m unittest tests.node.stack
python3 -m unittest tests.models.test_merkle
python3 -m unittest tests.models.test_patricia
python3 -m unittest tests.block.atom
python3 -m unittest tests.block.nonce
```
