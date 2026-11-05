"""Import shim: lets the repo's src/ (written for 3.11) import under 3.9 by
prepending `from __future__ import annotations` at load time. Nothing on disk
is modified."""
import importlib.abc, importlib.machinery, sys, os
REPO = "/Users/siddarthvaidya/Downloads/Projects/CausalLine"
class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)
    def source_to_code(self, data, path, *, _optimize=-1):
        src = data.decode("utf-8")
        lines = src.splitlines(True)
        # insert after any leading docstring/comments: simplest is to put the
        # future import first; a leading docstring then becomes an expression,
        # which is harmless for execution.
        if "from __future__ import annotations" not in src:
            src = "from __future__ import annotations\n" + src
        return compile(src, path, "exec", dont_inherit=True, optimize=_optimize)
class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "src" or name.startswith("src.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path or [REPO])
        if spec and spec.origin and spec.origin.endswith(".py"):
            spec.loader = _Loader(name, spec.origin)
        return spec
sys.meta_path.insert(0, _Finder())
if REPO not in sys.path: sys.path.insert(0, REPO)
