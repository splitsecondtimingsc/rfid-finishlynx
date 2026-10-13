"""Collection guard for package trees (audit 2026-10-13, S-04).

The hand-out package and the public GitHub release leave out the
export-ignored files (.gitattributes: bench/, tools/promote.py, ...). A test
module that imports one of those at module level then failed
`python -m pytest tests` with a collection error before any test ran.
Here such a module is left out of collection when what it imports is not in
this tree: a module-level `import rfid_finishlynx.x` / `from tests.x import
...` whose file is missing. In a full checkout nothing is left out.
"""

import ast
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def module_exists(name: str) -> bool:
    """Is this repo module's file (or package, or namespace package) in this tree?"""
    base = os.path.join(ROOT, *name.split("."))
    return os.path.isfile(base + ".py") or os.path.isdir(base)


def missing_imports(path: str):
    """Module-level imports of this repo's modules (rfid_finishlynx.*,
    tests.*) whose files are not in this tree."""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
    except (OSError, SyntaxError, ValueError):
        return []
    out = []
    for node in tree.body:               # module level only: imports inside a test are its own business
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        else:
            continue
        out += [n for n in names if n.split(".")[0] in ("rfid_finishlynx", "tests") and not module_exists(n)]
    return out


collect_ignore = [name for name in sorted(os.listdir(HERE))
                  if name.startswith("test_") and name.endswith(".py")
                  and missing_imports(os.path.join(HERE, name))]
