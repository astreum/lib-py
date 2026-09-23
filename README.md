# lib

Python library to interact with the Astreum blockchain and its virtual machine.

[View on PyPI](https://pypi.org/project/astreum/)

## Content

- [Configuration](#configuration)
- [Validation Overview](#validation-overview)
- [Transaction Overview](#transaction-overview)
- [Treasury Overview](#treasury-overview)
- [Query API](#query-api)
- [Language Syntax](#language-syntax)
- [Machine Overview](#machine-overview)
- [Operators](#operators)
- [Actor Model](#actor-model)
- [Quickstart Example](#quickstart-example)
- [Console Mode](#console-mode)
- [Logging](#logging)
- [Testing](#testing)

## Configuration

When initializing an `astreum.Node`, pass a dictionary with any of the options below. Only the parameters you want to override need to be present – everything else falls back to its default.

### Core Configuration

| Parameter                        | Type       | Default        | Description                                                                                                                                                                            |
| -------------------------------- | ---------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `chain`                          | string     | `"test"`       | Chain name (`"main"` or `"test"`). If `chain_id` is omitted, `main` maps to `1`; all other values default to test (`0`).                                                           |
| `chain_id`                       | int        | `0`            | Chain identifier used for validation (0 = test, 1 = main). If `chain` is omitted, `chain` is derived from this value (`1` => `main`, otherwise `test`).                             |
| `hot_storage_limit`              | int        | `1073741824`   | Maximum bytes kept in the hot cache before new atoms are skipped (1 GiB).                                                                                                            |
| `cold_storage_limit`             | int        | `10737418240`  | Cold storage write threshold (10 GiB by default); set to `0` to skip the limit.                                                                                                      |
| `cold_storage_path`              | string     | `None`         | Directory where persisted atoms live; Astreum creates it on startup and skips cold storage when unset.                                                                               |
| `cold_storage_scale`             | string     | `"MB"`         | Base unit for cold storage roll-up thresholds (`KB`, `MB`, or `GB`). This sets the derived `cold_storage_base_size` used for `level_0` collation and higher-level merges.           |
| `atom_fetch_interval`            | float      | `0.25`         | Poll interval (seconds) while waiting for missing atoms in `get_atom_list_from_storage`; `0` disables waiting.                                                                       |
| `atom_fetch_retries`             | int        | `8`            | Number of poll attempts for missing atoms; max wait is roughly `interval * retries`, `0` disables waiting.                                                                           |
| `verify_blockchain_interval`     | float      | `10.0`         | Delay (seconds) between consensus verification worker iterations. Defaults to `peer_timeout_interval` when not explicitly set.                                                        |
| `verification_max_stale_seconds` | int        | `10`           | Ignore otherwise-valid candidate heads whose block timestamp is older than this many seconds when selecting the latest verified chain head.                                           |
| `verification_max_future_skew`   | int        | `2`            | Ignore candidate heads whose block timestamp is more than this many seconds in the future when selecting the latest verified chain head.                                              |
| `latest_block_hash`              | hex string | `None`         | Optional 32-byte block-hash override used to preload the node's starting `latest_block_hash` from config.                                                                            |
| `verified_up_to`                 | hex string | `None`         | Optional 32-byte hash override used to preload the verification anchor (`node.verified_up_to`) from config.                                                                          |
| `logging_enabled`               | bool       | `True`         | When **False**, disable logger setup entirely, including file creation and the background logging listener thread.                                                                   |
| `logging_retention_days`         | int        | `7`            | Number of days to keep rotated log files (daily gzip).                                                                                                                                |
| `verbose`                        | bool       | `False`        | When **True**, also mirror JSON logs to stdout with a human-readable format.                                                                                                         |

### Communication

| Parameter                     | Type        | Default                       | Description                                                                                                                                          |
| ----------------------------- | ----------- | ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `relay_secret_key`            | hex string  | Auto-generated                | X25519 private key used for the relay route; a new keypair is created when this field is omitted.                                                   |
| `relay_payment_secret_key`    | hex string  | `None`                        | Optional Ed25519 private key used for relay/storage payment channels; when set, the node can advertise a relay payment public key for paid objects. |
| `validation_secret_key`       | hex string  | `None`                        | Optional Ed25519 key that lets the node join the validation route; leave blank to opt out of validation.                                            |
| `use_ipv6`                    | bool        | `False`                       | Bind the incoming/outgoing sockets on IPv6 (the OS still listens on IPv4 if a peer speaks both).                                                   |
| `port`                        | int         | `52780`                       | UDP port the relay binds to; pass `0` or omit to let the OS pick an ephemeral port.                                                                 |
| `default_seed`                | string      | `"bootstrap.astreum.org:52780"` | Default address to ping before joining; set to `None` to disable the built-in default.                                                            |
| `additional_seeds`            | list\[str\] | `[]`                          | Extra addresses appended to the bootstrap list; each must look like `host:port` or `[ipv6]:port`.                                                   |
| `peer_timeout`                | int         | `900`                         | Evict peers that have not been seen within this many seconds (15 minutes).                                                                          |
| `peer_timeout_interval`       | int         | `10`                          | How often (seconds) the peer manager checks for stale peers.                                                                                        |
| `bootstrap_retry_interval`    | int         | `30`                          | How often (seconds) to retry bootstrapping when the peer list is empty.                                                                             |
| `storage_index_interval`      | int         | `600`                         | How often (seconds) to re-advertise entries in `node.atom_advertisments` to the closest known peer.                                                |
| `storage_request_minimum_price` | int       | `1`                           | Floor price for storage/object requests; the dynamic storage request price never drops below this value.                                            |
| `storage_request_price_interval` | float    | `5.0`                         | How often (seconds) the storage thread recomputes request pricing from inbound queue pressure.                                                      |
| `fair_use_limit`              | int         | `1048576`                     | Bytes a peer may receive via shared object uploads before fair-use ratio enforcement begins (1 MiB by default).                                     |
| `fair_use_ratio`              | float       | `0.5`                         | Minimum `download/upload` ratio a peer must maintain after `fair_use_limit` is exceeded; set `0` to disable the fair-use gate.                     |
| `incoming_queue_size_limit`   | int         | `67108864`                    | Soft cap (bytes) for inbound queue usage tracked by `enqueue_incoming`; set to `0` to disable.                                                      |
| `incoming_queue_timeout`      | float       | `1.0`                         | When > 0, `enqueue_incoming` waits up to this many seconds for space before dropping the payload.                                                   |
| `message_timestamp_window`    | int         | `60`                          | Drop incoming messages whose envelope timestamp differs from local time by more than this many seconds (anti-replay); set `0` to disable the check. |

Advertisements: `node.atom_advertisments` holds `(atom_id, payload_type, expires_at)` tuples. Use `node.add_atom_advertisement` or `node.add_atom_advertisements` to enqueue entries (`expires_at=None` keeps them indefinite). Validators automatically advertise block, transaction (main and detail lists), receipt, and account trie lists for 15 minutes by default.

> **Note**
> The peer‑to‑peer *route* used for object discovery is always enabled.
> If `validation_secret_key` is provided the node automatically joins the validation route too.

### Usage

```python
from astreum.node import Node

config = {
    "relay_secret_key": "ab…cd",             # optional – hex encoded
    "validation_secret_key": "12…34",        # optional – validator
    "hot_storage_limit": 1073741824,         # cap hot cache at 1 GiB
    "cold_storage_limit": 10737418240,       # cap cold storage at 10 GiB
    "cold_storage_path": "./data/node1",
    "port": 52780,
    "use_ipv6": False,
    "default_seed": None,
    "additional_seeds": [
        "127.0.0.1:7374"
    ]
}

node = Node(config)
# … your code …
```

## Validation Overview

To start creating blocks, call `validate_blockchain(node, validation_secret_key)`. Validation connects the node, prepares validator state, initializes fork tracking, runs consensus verification when no `default_seed` is configured as a trusted head provider, and starts the consensus validation worker.

```python
from astreum.consensus.validation.node import validate_blockchain
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

validation_secret_key = Ed25519PrivateKey.generate()

validate_blockchain(node, validation_secret_key)
```

The validation worker only creates blocks when this node is the scheduled validator for the current head. It applies queued transactions when available, can create empty blocks when the queue is empty, stores the new block atoms locally, advertises them to peers, and updates `node.latest_block_hash` / `node.latest_block`.

## Transaction Overview

Use `send_transaction(...)` to atomize, store, advertise, and forward an already-signed transaction to available validators.

```python
from astreum.consensus.transaction import (
    create_transaction,
    send_transaction,
)

tx = create_transaction(
    chain_id=node.config["chain_id"],
    amount=100,
    counter=sender_account.counter + 1,
    recipient=recipient_public_key,
    sender=sender_public_key,
)
tx.sign(sender_key)
tx_hash = send_transaction(node, tx)
print(tx_hash.hex())
```

The node must already be connected and have a `latest_block`; otherwise the function raises `RuntimeError`. It writes the transaction's atoms to local storage, advertises them on the P2P network, and sends the transaction hash to peers on the validation route.


## Treasury Overview

The treasury account (`TREASURY_ADDRESS`) tracks per-participant state in a `TreasuryUserRecord`, keyed by address inside the treasury account's own data trie: staked `balance`, a `loans_root_hash` trie of secured loans, `total_interest_paid`, and an `offers_root_hash` trie of posted limit offers.

### Limit offers (`TREASURY_SELL`)

A limit seller posts a fixed-size, fixed-duration, fixed-price offer of capacity with `TREASURY_SELL`. The offer is stored in the seller's own `TreasuryUserRecord.offers_root_hash` trie, keyed by the hash of the transaction that created it — no principal moves on a post; the offer only becomes economically active once claimed.

```python
from astreum.consensus.transaction import create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.constants import TREASURY_ADDRESS

tx = create_transaction(
    chain_id=node.config["chain_id"],
    counter=sender_account.counter + 1,
    sender=sender_public_key,
    recipient=TREASURY_ADDRESS,
    code=TransactionCode.TREASURY_SELL,
    limit=1_000,     # capacity this offer covers
    duration=8,       # term in blocks; must be a power of 2
    price=50,         # flat fee paid to the seller at claim time
    expiry=100_000,   # block height after which an unclaimed offer can never be claimed
)
tx.sign(sender_key)
```

| Parameter  | Type  | Description |
|------------|-------|--------------|
| `limit`    | `int` | Exact capacity this offer covers. Must be `> 0`. |
| `duration` | `int` | Term in blocks. Must be `> 0` and a power of 2. |
| `price`    | `int` | Flat fee paid to the seller, in ASTR, in full, at claim time. Must be `>= 0`. |
| `expiry`   | `int` | Block height after which an unclaimed offer can never be claimed. Must be greater than the current block height. |

### `claim_offer(...)`

Consumer-specific transaction handlers claim a posted offer directly against the seller's offers trie:

```python
from astreum.consensus.transaction.treasury.offers import claim_offer
from astreum.storage.radix import RadixTree

offers_trie = RadixTree(root_hash=seller_user_record.offers_root_hash)
updated_offer = claim_offer(
    offers_trie=offers_trie,
    node=node,
    offer_transaction_id=offer_tx_hash,   # the TREASURY_SELL tx hash that created the offer
    claimant_id=claimant_id,              # opaque 32-byte id, e.g. a loan transaction hash
    current_height=block.height,
)
```

| Parameter | Type | Description |
|-----------|------|--------------|
| `offers_trie` | `RadixTree` | The seller's offers trie, opened from `TreasuryUserRecord.offers_root_hash`. |
| `node` | `Node` | Storage node used to resolve and persist trie/expr data. |
| `offer_transaction_id` | `bytes` | The 32-byte `TREASURY_SELL` transaction hash identifying the offer. |
| `claimant_id` | `bytes` | Opaque 32-byte id recorded as the claimant. |
| `current_height` | `int` | Current block height, used to reject claims past `expiry`. |

Returns the updated (claimed) `TreasuryCreditOffer`, or `None` if the offer doesn't exist, is already claimed, or is expired (`current_height >= expiry`). **A claim is permanent** — once `claimed_by` is set it never resets, and there is no "free" operation or transaction code to release it. `claim_offer` does not touch the seller's `TreasuryUserRecord.offers_root_hash` field itself — the caller must write the trie's new root hash back into that field.


## Query API

Query functions let you fetch blocks and search transactions by height or attribute from the chain tip.

```python
from astreum import get_block, find_transactions
```

### `get_block(node, *, height)`

Fetch a single block by its chain height. Returns the `Block` object or `None` if the block hasn't been mined yet or isn't reachable.

| Parameter | Type | Description |
|-----------|------|-------------|
| `node` | `Node` | An initialised, connected Astreum node. |
| `height` | `int` | The target block height. Must be ≤ the node's latest block. |

```python
block = get_block(node, height=100_000)
if block:
    print(f"block hash: {block.expr_id.hex()[:16]}...")
    print(f"tx count:   {len(block.transactions) if block.transactions else 0}")
```

Internally walks the `previous_block` chain to the target era, then binary-descents the bloom tree by offset — approximately 11 storage fetches (O(log N)) plus the chain walk.

### `find_transactions(node, *, sender, receiver, …)`

Search for transactions matching the given filters. All filter parameters are optional — leave a filter at its default (32 zero bytes) to match anything.

When multiple filters are set, only transactions matching **all** of them are returned (AND semantics).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `node` | `Node` | — | An initialised, connected Astreum node. |
| `tx_hash` | `bytes` | `ZERO32` | Match a specific transaction hash. |
| `sender` | `bytes` | `ZERO32` | Filter by sender public key. |
| `receiver` | `bytes` | `ZERO32` | Filter by recipient public key. |
| `key` | `bytes` | `ZERO32` | Filter by contract bloom key (from `bloom.put`). |
| `start_height` | `int` | `node.latest_block.height` | Search backward from this height. |
| `end_height` | `int` | `0` | Stop when blocks drop below this height. |
| `limit` | `int` | `1` | Max results; pass `0` for no limit. |

```python
# Find up to 10 transactions from a specific sender
txs = find_transactions(node, sender=addr, limit=10)

# Find transactions in a specific height range
txs = find_transactions(
    node,
    receiver=addr,
    start_height=50_000,
    end_height=40_000,
    limit=5,
)
```

Each returned `Transaction` has its `block_hash` field set to the expr hash of the block that included it, so you can locate the containing block.

Internally uses the bloom tree index to skip eras that can't contain a match, then walks individual blocks inside candidate eras.


## Language Syntax

Astreum Language is a homoiconic, stack-based concatenative language with runtime reflection and actor support. It has a functional programming style, but also supports state and effects; `deterministic` mode disables runtime effects and loading, making it suitable for on-chain evaluation. Its type system is open and nominally tagged, with tags structurally embedded in every value's canonical form.

Astreum uses S-expressions with prefix notation. Expressions are either atoms or parenthesised lists. Lists are right-linked `link` pairs — `(a b c)` parses as `link(a, link(b, c))`.

### Tokens

| Token | Meaning |
|-------|---------|
| `(` `)` | Delimit a list expression. |
| `'` | Quote token — when alone, parses as the symbol `'`. Inside a list it's a regular symbol. |
| `123` `-5` | Integer literals. Parsed as `int`. |
| `3.14` `-2.5` | Float literals. Parsed as `float`. |
| `"hello world"` | String literals. Everything between double quotes is one token (spaces, parens preserved). Parsed as `str`. |
| `0x1f` `0Xab` | Hex bytes. Raw hex digits, no two's complement. Parsed as `bytes`. |
| `#<64hex>` | Hash pointer — exactly 64 hex chars prefixed by `#`. References an expression by content hash. Parsed as a bare link (`head_hash` set, no `head`/`tail`). |
| `add` `def` | Everything else is a symbol. Parsed as `symbol`. |
| `;` | Line comment — skips to end of line. |
| `;;` | Expression skip — skips the next complete expression (including nested lists). |

### Type System

Every value is an `Expr` with a `base` slot (`"link"`, `"symbol"`, or `"bytes"`) and a `value` slot that holds the inline payload for scalar types (int, str, floats) or `None` for pair links (payload in `head`/`tail`). The `_tag`, `_value`, `_head`, and `_tail` properties expose the same read interface as before for backward compatibility. The type tag is the terminal symbol of the value's canonical linked form — the type is structurally embedded in the value itself. Types fall into three tiers:

- **Open** — any symbol passed to `init` introduces a new type at runtime; no declaration or registry required.
- **Dynamic** — tags are runtime symbols, introspected via `type` and dispatched by tag equality; no static checking.
- **Nominal** — type identity is tag-symbol equality, not structural shape.
- **Structurally embedded** — the tag is the terminal of the value's canonical linked form (`link(args…, symbol("tag"))`), making the type part of its content-addressed encoding.

#### Base types

The three terminal types with direct wire encoding. All other types decompose into these for hashing and serialization.

| Type | Wire tag | Description |
|------|----------|-------------|
| `link` | `0x00` | A pair `(head, tail)`. `link(None, None)` is NIL. |
| `symbol` | `0x01` | A named identifier. |
| `bytes` | `0x02` | Raw byte data. |

#### Builtin types

All eleven natively supported types. Includes the three base types (italicised) plus eight composed types that serialize as `link(bytes(payload), symbol(tag))`:

| Type | Tag | Encoding | Notes |
|------|-----|----------|-------|
| `int` | `"int"` | Variable-length signed LE | Composed |
| `e4m3` | `"e4m3"` | 1-byte (4-bit exp, 3-bit mantissa) | Composed |
| `e5m2` | `"e5m2"` | 1-byte (5-bit exp, 2-bit mantissa) | Composed |
| `fp16` | `"fp16"` | 2-byte IEEE 754 LE | Composed, half precision |
| `bf16` | `"bf16"` | 2-byte brain float | Composed |
| `fp32` | `"fp32"` | 4-byte IEEE 754 LE | Composed, single precision |
| `fp64` | `"fp64"` | 8-byte IEEE 754 LE | Composed, double precision (default literal) |
| `str` | `"str"` | UTF-8 | Composed |
| `symbol` | `"symbol"` | UTF-8 (wire 0x01) | *Base* |
| `bytes` | `"bytes"` | Raw (wire 0x02) | *Base* |
| `link` | `"link"` | — (wire 0x00) | *Base* |

#### Float arithmetic

Arithmetic on floats stays at the **same precision** — no automatic widening.
Finite overflow raises an error (NIL for bare ops, `("msg" . err)` for `?`
variants). Users explicitly cast to a wider type when computation requires
more headroom. Inf and NaN are valid values and round-trip through encoding.

#### User types

Any other tag string. User types are entirely open — passing any symbol to `init` creates a new type on the spot; no declaration, registry, or schema is required. Constructed via `init` and introspected via `type`:

```
(3 5 link 'point init)   → Expr("link", head=link(3, 5), tail=symbol("point"))
(point_val type)          → Symbol("point")
```

#### Type-name-as-constructor

Built-in type names (`int`, `bytes`, `e4m3`, `e5m2`, `fp16`, `bf16`, `fp32`, `fp64`, `str`, `symbol`, `link`) are polymorphic coercion operators. User types follow the same pattern — the type name is bound as a function that calls `init` with its quoted tag:

```
'( (x y link 'point init) (x y) closure) 'point def
'( (expr) expr head closure) 'point.x def
'( (expr) expr tail head closure) 'point.y def
```

`(3 5 point apply)` constructs a point. The explicit `apply` is required because function values are tagged link pairs. `init` is idempotent (re-tagging a value that already bears the target tag is a no-op). `type` returns the tag as a Symbol, following the tag-last canonical form — the type symbol is the terminal of the link chain.

### Environment

`Env(data={}, parent=None)` is a lexically-scoped binding store with parent-chain lookup. `env.get(key)` walks up parent environments. `env.put(key, value)` writes to the local environment only. Parent environments are structurally immutable — they are created once and never mutated, making them safe to share by reference for closures and continuations.

### Module Assembly

`assemble_env(node, script, target)` resolves a target definition from a
`.aex` source file, recursively resolving imports and tree-shaking only the
transitively referenced definitions into an ``Env``.

```python
from astreum.machine import assemble_env

env = assemble_env(
    node=None,
    script="calc.aex",
    target="calc_sum",
)
```

Dotted targets resolve across module boundaries — ``"math.calc_sum"`` finds the
``calc_sum`` definition inside the ``math`` import.  Pass a connected ``Node``
when modules reference HTTP-imported definitions (network fetch is triggered
during resolution).

## Machine Overview

The machine evaluates an expression tree against an environment, producing a result stack.

```python
from astreum.machine.main import Machine
from astreum.machine import Env, Expr, tokenize, parse
from astreum.node import Node

node = Node()
machine = Machine(node)

# Parse source text and evaluate (env is optional — run() creates a fresh one by default)
tokens = tokenize("(1 2 +)")
expr, _ = parse(tokens)

result = machine.run(expr)
# result = 3
```

`machine.run(expr, env=None)` walks the expression tree and returns the top value of the result stack (or NIL if the stack is empty). Each `run()` call without an explicit env creates a fresh top-level `Env()`, so `def` bindings from one call do not persist across subsequent calls. Pass an explicit env to share bindings. Symbols that match operators pop arguments and push results.

The `Machine` constructor accepts a `mode` parameter (`"dynamic"` or `"deterministic"`, default `"dynamic"`). In deterministic mode the operators `spawn`, `send`, `receive`, `ref`, `load`, `print`, and `println` push NIL instead of executing — this ensures reproducible evaluation for contexts such as block validation. Pure operators like `closure`, `apply`, and `eval` work normally.

### Metering

Every `Machine` carries a `Meter` that tracks computation cost:

```python
machine = Machine(node, meter_limit=1_000_000)
machine.meter.eval
machine.meter.storage
machine.meter.total
```

### Error handling

Operators raise `OpError` on type mismatches, stack underflow, out-of-bounds access, or other semantic errors. The dispatch layer catches `OpError` and responds differently depending on the operator name:

- **Bare form** (`+`, `/`, `split`, …) — catches the error and pushes NIL (`link(None, None)`). The program continues with NIL on the stack.
- **Tagged form** (`+?`, `/?`, `split?`, …) — appending `?` to any primitive operator name wraps the result as a tagged pair:
  - Success: `(result_value . ok)` — or `(nil . ok)` for void operators (`drop?`, `def?`, `print?`, `send?`, etc.)
  - Error: `("error message" . err)` — the message string describes what went wrong.

`MeterExceededError` is never caught and always propagates.

```python
from astreum.machine.main import Machine
from astreum.machine import tokenize, parse, Expr

machine = Machine(node=None)

# Bare form: error pushes NIL
result = machine.run(*parse(tokenize("(drop)")))

# Tagged form: error wraps as (reason . err)
result = machine.run(*parse(tokenize("(drop?)")))

# Tagged form: success wraps as (value . ok)
result = machine.run(*parse(tokenize("(7 8 +?)")))
```

## Operators

Operators are symbols that pop arguments from the stack and push a result. Any primitive operator can be suffixed with `?` to wrap the result as `(v ok)` on success or `(reason err)` on error (see [Error handling](#error-handling)).

### Arithmetic

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `+` | `(a b -- sum)` | Addition. Int/Int → Int. Floats require matching types; result stays at operand precision. Finite overflow raises OpError. Mixed float types error. |
| `-` | `(a b -- diff)` | Subtraction. Same type rules as `+`. |
| `*` | `(a b -- prod)` | Multiplication. Same type rules as `+`. |
| `/` | `(a b -- quot)` | Division. Int/Int → integer division (`//`). Floats require matching types; result stays at operand precision. Division by zero raises OpError. |
| `%` | `(a b -- rem)` | Modulo (Int only). Raises OpError on non-Int. |
| `sqrt` | `(a -- sqrt(a))` | Square root (Floats only). Result stays at operand precision. Raises OpError on non-float or negative. |
| `abs` | `(a -- abs(a))` | Absolute value (Int or Float). Result stays at operand precision. Raises OpError on non-numeric input. |

### Comparison

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `<` | `(a b -- 0\|1)` | Less than (Int/Int or matching Float types). Pushes `Bytes(b"\\x01")` if true, else `Bytes(b"\\x00")`. Raises OpError on type mismatch or mixed float types. |
| `>` | `(a b -- 0\|1)` | Greater than. Same type rules as `<`. |
| `<=` | `(a b -- 0\|1)` | Less than or equal. Same type rules as `<`. |
| `>=` | `(a b -- 0\|1)` | Greater than or equal. Same type rules as `<`. |

### Bitwise

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `&` | `(a b -- a&b)` | Bitwise AND (Bytes). |
| `\|` | `(a b -- a\|b)` | Bitwise OR (Bytes). |
| `^` | `(a b -- a^b)` | Bitwise XOR (Bytes). |
| `~` | `(a -- ~a)` | Bitwise NOT (Bytes, one's complement within the operand's byte width). |

### Shift & Rotate

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `<<` | `(value shifts -- result)` | Shift: value (Bytes or Int) left by `shifts` (Int > 0) or right by `shifts` (Int < 0). For Bytes the shift is logical (zero-fill), for Int it is arithmetic (sign-extend). No-op on 0. Raises OpError on type mismatch. |
| `<<<` | `(value shifts -- result)` | Rotate: value (Bytes or Int) left by `shifts` (Int > 0) or right by `shifts` (Int < 0). Rotation width is byte-rounded for Int. No-op on 0. Raises OpError on type mismatch. |

### Stack

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `dip` | `(v (expr) -- ... v)` | Temporarily remove `v`, evaluate `(expr)` on the remaining stack, then push `v` back. Raises OpError on underflow. |
| `drop` | `(a -- )` | Pop and discard one value. Raises OpError on underflow. |
| `dup` | `(a -- a a)` | Pop and push the same value twice. Raises OpError on underflow. |
| `swap` | `(a b -- b a)` | Pop two values and push them back in reversed order. Raises OpError on underflow. |
| `rot` | `(a b c -- b c a)` | Rotate the top three stack values left. Raises OpError on underflow. |

### Pairs (link)

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `link` | `(head tail -- link(head, tail))` | Construct a `link` pair. |
| `head` | `(link(h, t) -- h)` | Extract the head of a `link`; raises OpError on non-link. |
| `tail` | `(link(h, t) -- t)` | Extract the tail of a `link`; raises OpError on non-link. |

### Tag operators

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `ok` | `(a -- (a . ok))` | Wrap value as tagged success pair. |
| `err` | `(msg -- (msg . err))` | Wrap message as tagged error pair. |
| `result` | `(tagged -- a)` or `(tagged continuation -- ...)` | Monadic bind. If tag is `err`, forward the tagged value (short-circuit). Otherwise, extract the value and evaluate continuation. Provides Haskell-style `>>=` semantics for error propagation. |
| `match` | `(val succ_tag succ_cl fail_cl -- ...)` | Pattern match on tag. If `val` has tag matching `succ_tag`, unwrap and run `succ_cl`. Otherwise run `fail_cl`. |

### Predicates

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `is_atom` | `(expr -- 0\|1)` | Pushes `Bytes(b"\\x01")` if the value is not a `link`, else `Bytes(b"\\x00")`. |
| `is_eq` | `(a b -- 0\|1)` | Structural equality: atoms compared by value; `link` by recursive head+tail. Different types are never equal. |

### Type operators

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `init` | `(value 'tag -- expr)` | Wrap `value` in a typed Expr with tag `tag`. Idempotent for matching tags (`(42 'int init)` → `42`). |
| `type` | `(expr -- symbol)` | Return the tag of `expr` as a Symbol (`(42 type)` → `Symbol("int")`). |
| `id` | `(expr -- bytes)` | Push the 32-byte BLAKE3 content-addressable id of `expr`. Works on any expression type. |

### Conversion

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `int` | `(a -- int\|nil)` | Convert Bytes (LE signed), String, Symbol, or Float to `Int`. |
| `e4m3` | `(a -- e4m3\|nil)` | Convert Bytes (1 byte), Int, String, or Symbol to E4M3 float. |
| `e5m2` | `(a -- e5m2\|nil)` | Convert Bytes (1 byte), Int, String, or Symbol to E5M2 float. |
| `fp16` | `(a -- fp16\|nil)` | Convert Bytes (2 bytes), Int, String, or Symbol to FP16. |
| `bf16` | `(a -- bf16\|nil)` | Convert Bytes (2 bytes), Int, String, or Symbol to BF16. |
| `fp32` | `(a -- fp32\|nil)` | Convert Bytes (4 bytes), Int, String, or Symbol to FP32. |
| `fp64` | `(a -- fp64\|nil)` | Convert Bytes (8 bytes), Int, String, or Symbol to FP64. |
| `str` | `(a -- string\|nil)` | Convert any atom to `String`. Raises OpError on unsupported type. |
| `bytes` | `(a -- bytes\|nil)` | Convert Int (variable-length signed), Float (per-type byte width), String, or Symbol (UTF-8) to `Bytes`. |
| `symbol` | `(a -- symbol\|nil)` | Convert Bytes (UTF-8 decoded), String, Int, or Float to `Symbol`. Raises OpError on invalid UTF-8. |

### Sequence operators

Sequence operators work on `bytes`, `str`, and `link` (collectively referred to as sequences). Their fn argument is a **program** evaluated once per element with the element value(s) pre-pushed on the stack. Dispatch is explicit in the program, not inferred by the operator: a raw quotation runs concatenatively (`'(1 +) map`), an **eval-style spec** runs a named program (`'(rule eval)`), and an **apply-style spec** invokes a named function with the stack as arguments (`'((fn) apply)`). Tagged function values must be apply-wrapped — a raw tagged fn value raises `OpError`, as does any non-program fn (scalars, tagged results).

The `?` suffix follows standard error handling: bare form pushes NIL on error, tagged form wraps success as `(value . ok)` and error as `("message" . err)`.

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `concat` | `(a b -- a⊕b)` | Concatenate two sequences. Both must be the same type (bytes, str, or link). Raises OpError on type mismatch. |
| `count` | `(seq -- int)` | Return the number of elements. Empty seq → `0`. Raises OpError on non-sequence. |
| `each` | `(seq fn -- seq)` | Apply `fn` as a side effect on each element, then restore the original seq on the stack. |
| `filter` | `(seq pred -- filtered)` | Return a new seq containing only elements for which `pred` is truthy. Result type matches input. Empty if none match. |
| `find` | `(seq pred -- (elem . some)\|("not found" . none))` | Return the first element matching `pred`. On match pushes a `(elem . some)` tagged pair; on miss pushes a `("not found" . none)` tagged pair. |
| `fold` | `(seq acc fn -- result)` | Left-associative fold. Calls `fn(acc, elem)` (elem on top) for each element. Empty seq → `acc` unchanged. An apply-style spec with a 2-param closure binds `[acc, elem]`. |
| `index` | `(seq int -- elem)` | Return the element at position `k` (0-based). For bytes returns a 1-byte value; for str a single character; for link the nth element. Raises OpError on out-of-bounds. |
| `map` | `(seq fn -- mapped)` | Apply `fn` to each element, returning a new seq of the same type. `bytes`/`str` require results to be the same element tag; `link` allows heterogeneous results. |
| `reverse` | `(seq -- reversed)` | Return the seq with element order reversed. |
| `split` | `(seq int -- (left . right))` | Split a sequence at position `k`, returning `link(left, right)`. `k=0` on empty produces two empty values. Raises OpError on out-of-bounds. |
| `zip` | `(a b -- link-of-pairs)` | Pair elements of two sequences pairwise into `link(link(a_i, b_i), ...)`. Truncated to the shorter sequence. Operands may be different sequence types. |

### Control flow

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `if` | `((cond) then else -- result)` | Evaluate `cond` quotation; if truthy evaluate `then`, otherwise evaluate `else`. |
| `rec` | `(pred then_branch rec1 rec2 -- result)` | Tail/general recursion loop. Evaluates `pred`; if truthy evaluates `then_branch`. Otherwise evaluates `rec1`, recurses, then evaluates `rec2` on return. |

### Functions & binding

Astreum has two function models — **point-free** (unnamed, stack-based) and **named** (parameter-bound, explicitly scoped).

**Point-free (unnamed).** A quoted list `'(1 +)` is a raw expression body. Operators that consume it evaluate directly against the current stack — no parameter names, no environment setup:

```
5 '(1 +) eval                   → 6
'(1 2 3) '(1 +) map             → '(2 3 4)
```

**Named (tagged).** Use `closure` to bundle body + param list + environment capture into a tagged value (tag `'lex`). `apply` pops args from the stack, binds them to param names, and evaluates in a controlled environment:

```
5 '(a) '(a 1 +) closure apply   → 6        (1 param)
3 5 '(a b) '(a b +) closure apply → 8      (2 params)
```

Sequence ops dispatch the same two ways — via **spec** programs (`'(rule eval)` / `'((fn) apply)`). A named fn must be apply-wrapped; a raw tagged value raises `OpError`:

```
(('(a) '(a 1 +) closure) 'f def '(1 2 3) '((f) apply) map)   → '(2 3 4)
((('(a b) '(a b +) closure) 'f def) '(1 2 3) 0 '((f) apply) fold) → 6
```

`'dyn` and `'pure` are tag symbols that mark a function value's parent-env semantics — `apply` reads the tag to decide the parent at call time. Users construct tagged function values manually with `link`. `closure` is the only producer operator.

```
(3 5 '(a b +) '(a b) link 'dyn link apply)   -- live env (dyn tag)
(3 5 '(a b +) '(a b) link 'pure link apply)   -- closed env (pure tag)
```

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `closure` | `(params body -- tagged)` | Pop `params` (Symbol chain) and `body`. Snapshot the current environment and push a tagged link pair `(((env_uuid . body) . params) . 'lex)` for later use with `apply`. |
| `apply` | `(argN … arg1 tagged -- result)` | Pop a tagged function value and N args. Build a child environment parented per the tag: `'lex` → captured snapshot, `'dyn` → call-site env, `'pure` → `None`. Evaluate body and push the top result. |
| `def` | `(value name -- )` | Bind `name` (Symbol) to `value` in the current lexical environment. Write-once: raises `OpError` if already bound in the current scope. |

### Quotation

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `'` | `((' X) -- X)` | Quote special form — wraps a single unevaluated expression. `(' 42)` pushes `42`. |
| `quote` | `(a -- (' a))` | Stack operator — pops a value and pushes it back wrapped in a `(' …)` quotation. |

### Code & storage

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `eval` | `(expr -- result\|nil)` | Pop an expression and evaluate it as code in the current environment. Raises OpError on underflow. |
| `ref` | `(hash -- expr\|nil)` | Resolve a 32-byte hash to its stored expression (`node.get_expr`). For `link` values, returns thunk-wrapped `(head_h ref)` / `(tail_h ref)` for lazy traversal. Raises OpError on non-Bytes or wrong-size input. In deterministic mode pushes NIL. |
| `load` | `(hash -- full_expr\|nil)` | Deep-resolve a 32-byte hash recursively through the entire sub-tree (`node.get_expr_full`). Cost is 2× the resolved expression size. Raises OpError on non-Bytes or wrong-size input. In deterministic mode pushes NIL. |
| `parse` | `(str -- expr)` | Tokenize and parse a string into an Expr. Raises OpError on non-string, ParseError on empty/invalid input. |

### Consensus

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `acc.balance` | `( -- balance)` | Push the expression account's (`tx.recipient`) balance as Int. |
| `acc.get` | `(key -- value\|nil)` | Look up `key` (Bytes) in the expression account's (`tx.recipient`) data store. Pushes NIL if absent. |
| `acc.put` | `(key value -- )` | Store `value` (Bytes) under `key` (Bytes) in the expression account's (`tx.recipient`) data. Sender pays a storage fee. Void: pushes nothing. |
| `acc.pay` | `(recipient amount -- )` | Pay `amount` (Int) from the expression account (`tx.recipient`) to `recipient` (Bytes). Creates recipient account if missing; sender pays storage fee for new accounts. |
| `block.bloom.insert` | `(value -- )` | Record `value.hash()` as a bloom search key. Charges 8 storage bytes per non-dedup call. Deduped per-tx and per-block. Void: pushes nothing. |
| `block.chain_id` | `( -- chain_id)` | Push the current block's `chain_id` as Int. |
| `block.height` | `( -- height)` | Push the current block's `height` as Int. |
| `block.previous_block_hash` | `( -- hash)` | Push the current block's `previous_block_hash` as Bytes (32 bytes). |
| `block.timestamp` | `( -- timestamp)` | Push the current block's `timestamp` as Int. |
| `tx.amount` | `( -- amount)` | Push the current transaction's `amount` as Int. |
| `tx.new` | `(code recipient amount data -- 1\|nil)` | Construct an internal (unsigned) transaction and apply its effects inline as part of the current contract call. The contract appears as the nested tx's sender; the value transferred debits the contract's balance; execution + storage fees debit the outer tx sender. On success pushes `1`; on failure pushes NIL. |
| `tx.log` | `(value -- )` | Append `value` to the transaction's log list. Charges a storage fee. Void: pushes nothing. |
| `tx.recipient` | `( -- recipient)` | Push the current transaction's `recipient` public key as Bytes (32 bytes). |
| `tx.sender` | `( -- sender)` | Push the current transaction's `sender` public key as Bytes (32 bytes). |


### Console I/O

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `print` | `(text -- )` | Write text to stdout (no newline). Void: pushes nothing. In deterministic mode pushes NIL without writing. |
| `println` | `(text -- )` | Write text + newline to stdout. Void: pushes nothing. In deterministic mode pushes NIL without writing. |


## Actor Model

The machine supports concurrent actors communicating via named mailboxes.

| Operator | Stack effect | Description |
|----------|-------------|-------------|
| `spawn` | `(body name -- name\|nil)` | Spawn a new actor thread running `body` in a child environment. `name` must be a Symbol. Raises OpError on non-symbol name or non-link body. Returns NIL if the name is already taken or threading is disabled. In deterministic mode pushes NIL. |
| `send` | `(target msg -- )` | Send `msg` to the mailbox of actor `target`. `target` must be a Symbol. Raises OpError on non-symbol target or if the mailbox doesn't exist. Void: pushes nothing. In deterministic mode pushes NIL. |
| `receive` | `(target -- msg\|nil)` | Block until a message arrives in the mailbox of actor `target`. Returns NIL if the mailbox doesn't exist. Raises OpError on non-symbol target. In deterministic mode pushes NIL. |

Actors run on daemon threads with their own environment (parented to the spawner's environment). In deterministic mode `spawn`, `send`, `receive`, `ref`, `load`, `print`, and `println` push NIL — they require concurrency, external content lookup, or I/O, all of which are disabled there for reproducible evaluation. Pure operators (`closure`, `apply`, `eval`) work normally.

## Quickstart Example

```python
from astreum.machine.main import Machine
from astreum.machine import Env, Expr, tokenize, parse, Meter
from astreum.node import Node

node = Node()
machine = Machine(node)

# Create a function that adds its two arguments, then add 2 to the result
src = "((3 5 '(x y) '(x y +) closure apply) 2 +)"
tokens = tokenize(src)
expr, _ = parse(tokens)

result = machine.run(expr)

print(result.value)  # 10
```

### Parse errors

`tokenize` and `parse` raise `ParseError` (from `astreum.machine.parser`) on malformed input:

```python
from astreum.machine import tokenize, parse, ParseError

try:
    tokens = tokenize("(1 2")
    expr, _ = parse(tokens)
except ParseError as e:
    print("Parse failed:", e)
```

Runtime errors during evaluation raise `OpError` and are caught by the dispatch layer — see [Error handling](#error-handling).

## Console Mode

`machine.enable_console()` starts a stdin daemon thread that sends line-delimited input to the `@pipe` mailbox. A REPL actor can `('@pipe receive parse eval println) rec` to read, parse, evaluate, and print expressions interactively.

```python
from astreum.machine.main import Machine
from astreum.machine import Env, tokenize, parse

machine = Machine(node, mode="dynamic")
machine.enable_console()

repl_script = "0 drop ('@pipe receive parse eval println) drop rec"
tokens = tokenize(repl_script)
repl_expr, _ = parse(tokens)

env = Env()
machine.spawn_actor(repl_expr, "@repl", env)

# Console mode running — press Ctrl+C to exit
# machine.disable_console()
```

`disable_console()` signals the daemon to stop. The daemon thread exits on the next `readline()` call or when the process ends.

Lines are `\n`-delimited (LF), stripped by the daemon before sending as `str_` Exprs.

---


## Logging

Every `Node` instance wires up structured logging automatically:

- Set `config["logging_enabled"] = False` to skip logging setup entirely. This bypasses log directory creation, file rotation, console mirroring, and the background listener thread.
- Logs land in per-instance files named `node.csv` under `%LOCALAPPDATA%\Astreum\lib-py\logs/<instance_id>` on Windows and `$XDG_STATE_HOME` (or `~/.local/state`)/`Astreum/lib-py/logs/<instance_id>` on other platforms. The `<instance_id>` is the first 16 hex characters of a BLAKE3 hash of the caller's file path, so running the node from different entry points keeps their logs isolated.
- Files rotate at midnight UTC with gzip compression (`node-YYYY-MM-DD.csv.gz`) and retain 7 days by default. Override via `config["logging_retention_days"]`.
- Each event is a single CSV row with columns `ts`, `level`, `msg`, `module`, and `func`.
- Set `config["verbose"] = True` to mirror logs to stdout in a human-friendly format like `[2025-04-13-42-59] [info] Starting Astreum Node`.
- The very first entry emitted is the banner `Starting Astreum Node`, signalling that the logging pipeline is live before other subsystems spin up.

## Testing

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
python3 -m unittest discover -s tests
```

### Test summary

Each package under `tests/` (`machine`, `consensus`, `communication`, `crypto`, `storage`, `node`, `utils`) is covered by its own suite. Run a single package, e.g. `python3 -m unittest discover -s tests/machine`.
