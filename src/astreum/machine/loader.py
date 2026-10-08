from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib import request as _urllib_request

from astreum.expression import get_expr_tag
from astreum.machine import Env, Expr, parse, tokenize


def assemble_env(
    node: object,
    script: str,
    target: str,
) -> Env:
    """Resolve a target definition and its transitive dependencies into an Env.

    Parses the root ``.aex`` script, resolves imports across module
    boundaries, and tree-shakes the definition graph so only the
    definitions transitively referenced by ``target`` are included.

    Args:
        node: An Astreum Node instance (``None`` if only file-based imports
            are used).
        script: Path to the root ``.aex`` script file (absolute or relative).
        target: Name of the definition to resolve.  May include dots for
            imported modules, e.g. ``"math.calc_sum"``.

    Returns:
        An ``Env`` containing the target definition and all definitions
        it transitively depends on.
    """
    env_data: Dict[str, Expr] = {}
    visited: Set[Tuple[str, str, Tuple[str, ...]]] = set()
    cache: Dict[str, Tuple[Dict[str, Expr], Dict[str, str]]] = {}

    module_id = _resolve_identity(script, None)

    if "." in target:
        parts = target.split(".")
        _resolve_name_chain(
            node=node, module_id=module_id, name_parts=parts,
            prefix_chain=(), env_data=env_data, visited=visited, cache=cache,
        )
    else:
        _resolve_def(
            node=node, module_id=module_id, name=target,
            prefix_chain=(), env_data=env_data, visited=visited, cache=cache,
        )

    return Env(data=env_data)


# ---------------------------------------------------------------------------
# URL fetch
# ---------------------------------------------------------------------------

def _fetch_url(url: str) -> str:
    """Fetch content from a URL.

    Args:
        url: The URL to fetch.

    Returns:
        The content as a UTF-8 string.
    """
    with _urllib_request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def _resolve_identity(path_str: str, from_dir: Optional[Path]) -> str:
    """Resolve a path string to an identity string.

    Args:
        path_str: The path string (URL, absolute, or relative).
        from_dir: The directory to resolve relative paths from.

    Returns:
        The resolved identity string.
    """
    if path_str.startswith(("http://", "https://")):
        return path_str

    p = Path(path_str).expanduser()
    if not p.is_absolute() and from_dir is not None:
        p = (from_dir / p).resolve()
    else:
        p = p.resolve()

    return str(p)


# ---------------------------------------------------------------------------
# Prefix joining
# ---------------------------------------------------------------------------

def _join_prefixes(chain: Tuple[str, ...], name: str) -> str:
    """Join a prefix chain with a name.

    Args:
        chain: The prefix chain tuple.
        name: The name to append.

    Returns:
        The fully qualified name.
    """
    if not chain:
        return name
    return ".".join(chain) + "." + name


# ---------------------------------------------------------------------------
# Link → list helper
# ---------------------------------------------------------------------------

def _link_to_list(link: Expr) -> List[Expr]:
    """Convert a linked expression to a list.

    Args:
        link: The head of the linked expression chain.

    Returns:
        List of expressions in the chain.
    """
    result: List[Expr] = []
    while link._tag == "link":
        if link._head is None and link._tail is None:
            break
        result.append(link._head)
        link = link._tail
    return result


# ---------------------------------------------------------------------------
# Path extraction from import expr  (Symbol value or Bytes decoded as utf-8)
# ---------------------------------------------------------------------------

def _path_from_expr(expr: Expr) -> str:
    """Extract a path string from an expression.

    Args:
        expr: The expression containing the path.

    Returns:
        The path string.
    """
    expr_tag = get_expr_tag(expr)
    if expr_tag == "symbol":
        return _strip_quotes(expr.value)
    if expr_tag == "bytes":
        return expr.value.decode("utf-8")
    raise ValueError(f"import path must be a symbol or bytes, got {expr_tag}")


def _strip_quotes(value: str) -> str:
    """Strip surrounding quotes from a string.

    Args:
        value: The string potentially surrounded by quotes.

    Returns:
        The string with quotes removed if present.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


# ---------------------------------------------------------------------------
# Module parsing (cached)
# ---------------------------------------------------------------------------

def _parse_module(
    identity: str,
    cache: Dict[str, Tuple[Dict[str, Expr], Dict[str, str]]],
) -> Tuple[Dict[str, Expr], Dict[str, str]]:
    """Parse a module and return its definitions and imports.

    Args:
        identity: The module identity (file path or URL).
        cache: Cache for parsed modules.

    Returns:
        Tuple of (definitions dict, imports dict).
    """
    if identity in cache:
        return cache[identity]

    if identity.startswith(("http://", "https://")):
        text = _fetch_url(identity)
        mod_dir = None
    else:
        try:
            text = Path(identity).read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"unable to read module at '{identity}'") from exc
        mod_dir = Path(identity).parent

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    tokens = tokenize(text)

    defs: Dict[str, Expr] = {}
    imports: Dict[str, str] = {}

    remaining = tokens
    while remaining:
        expr, remaining = parse(tokens=remaining)
        elems = _link_to_list(expr)

        if len(elems) != 3:
            raise ValueError(f"definition must have 3 elements, got {len(elems)}")

        first, second, terminator = elems
        if get_expr_tag(terminator) != "symbol":
            raise ValueError("definition must terminate with a symbol")

        if terminator.value == "def":
            if get_expr_tag(second) != "symbol":
                raise ValueError("def name must be a symbol")
            defs[second.value] = first

        elif terminator.value == "import":
            if get_expr_tag(first) != "symbol":
                raise ValueError("import prefix must be a symbol")
            path_str = _path_from_expr(second)
            imports[first.value] = _resolve_identity(path_str, mod_dir)

        else:
            raise ValueError(
                f"definition must terminate with def or import, got '{terminator.value}'"
            )

    if not defs and not imports:
        raise ValueError(f"module '{identity}' contains no definitions or imports")

    cache[identity] = (defs, imports)
    return defs, imports


# ---------------------------------------------------------------------------
# resolve  –  tree-shaking entry per target def
# ---------------------------------------------------------------------------

def _resolve_def(
    node,
    module_id: str,
    name: str,
    prefix_chain: Tuple[str, ...],
    env_data: Dict[str, Expr],
    visited: Set[Tuple[str, str, Tuple[str, ...]]],
    cache: Dict[str, Tuple[Dict[str, Expr], Dict[str, str]]],
) -> None:
    """Resolve a definition and its dependencies.

    Args:
        node: The Astreum Node instance.
        module_id: The module identity.
        name: The definition name to resolve.
        prefix_chain: The prefix chain for qualified names.
        env_data: Dictionary to store resolved definitions.
        visited: Set of visited (module, name, chain) tuples.
        cache: Module parse cache.
    """
    vkey = (module_id, name, prefix_chain)
    if vkey in visited:
        return
    visited.add(vkey)

    defs, imports = _parse_module(module_id, cache)

    if name not in defs:
        raise ValueError(f"definition '{name}' not found in '{module_id}'")

    body = defs[name]
    qualified = _join_prefixes(prefix_chain, name)
    env_data[qualified] = body

    _walk_body(
        body,
        module_id=module_id,
        prefix_chain=prefix_chain,
        defs=defs,
        imports=imports,
        env_data=env_data,
        visited=visited,
        cache=cache,
        node=node,
    )


def _resolve_name_chain(
    node,
    module_id: str,
    name_parts: List[str],
    prefix_chain: Tuple[str, ...],
    env_data: Dict[str, Expr],
    visited: Set[Tuple[str, str, Tuple[str, ...]]],
    cache: Dict[str, Tuple[Dict[str, Expr], Dict[str, str]]],
) -> None:
    """Resolve a dot-separated name chain across module imports.

    Args:
        node: The Astreum Node instance.
        module_id: The starting module identity.
        name_parts: List of name parts (split by dots).
        prefix_chain: The accumulated prefix chain.
        env_data: Dictionary to store resolved definitions.
        visited: Set of visited (module, name, chain) tuples.
        cache: Module parse cache.
    """
    first, *rest = name_parts
    defs, imports = _parse_module(module_id, cache)

    if rest:
        if first not in imports:
            raise ValueError(f"import prefix '{first}' not found in '{module_id}'")
        next_id = imports[first]
        new_chain = prefix_chain + (first,)
        _resolve_name_chain(
            node=node, module_id=next_id, name_parts=rest,
            prefix_chain=new_chain, env_data=env_data,
            visited=visited, cache=cache,
        )
    else:
        _resolve_def(
            node=node, module_id=module_id, name=first,
            prefix_chain=prefix_chain, env_data=env_data,
            visited=visited, cache=cache,
        )


def _walk_body(
    expr: Expr,
    module_id: str,
    prefix_chain: Tuple[str, ...],
    defs: Dict[str, Expr],
    imports: Dict[str, str],
    env_data: Dict[str, Expr],
    visited: Set[Tuple[str, str, Tuple[str, ...]]],
    cache: Dict[str, Tuple[Dict[str, Expr], Dict[str, str]]],
    node,
) -> None:
    """Walk an expression tree and resolve referenced symbols.

    Args:
        expr: The expression to walk.
        module_id: The current module identity.
        prefix_chain: The current prefix chain.
        defs: Module definitions dictionary.
        imports: Module imports dictionary.
        env_data: Dictionary to store resolved definitions.
        visited: Set of visited (module, name, chain) tuples.
        cache: Module parse cache.
        node: The Astreum Node instance.
    """
    if get_expr_tag(expr) == "symbol":
        sym = expr.value
        if "." in sym:
            parts = sym.split(".")
            first, rest = parts[0], parts[1:]
            if first in imports:
                next_id = imports[first]
                new_chain = prefix_chain + (first,)
                _resolve_name_chain(
                    node=node, module_id=next_id, name_parts=rest,
                    prefix_chain=new_chain, env_data=env_data,
                    visited=visited, cache=cache,
                )
        elif sym in defs:
            _resolve_def(
                node=node, module_id=module_id, name=sym,
                prefix_chain=prefix_chain, env_data=env_data,
                visited=visited, cache=cache,
            )

    elif expr._tag == "link":
        if expr._head is not None:
            _walk_body(
                expr._head, module_id=module_id, prefix_chain=prefix_chain,
                defs=defs, imports=imports, env_data=env_data,
                visited=visited, cache=cache, node=node,
            )
        if expr._tail is not None:
            _walk_body(
                expr._tail, module_id=module_id, prefix_chain=prefix_chain,
                defs=defs, imports=imports, env_data=env_data,
                visited=visited, cache=cache, node=node,
            )



