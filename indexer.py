"""Static index of the program's classes, for the argument picker.

The sources under <root>/<src_dir> are parsed with `ast`. Nothing is imported, so torch, transformers or a GPU are not
needed on the machine that runs the launcher. For each class the index knows its bases, the arguments of its constructor
(type, default, docstring) and resolves them the way jsonargparse does:
  - a constructor with `**kwargs` forwarded to `super().__init__(...)` also gets the arguments of the parent class,
    minus those it passes explicitly;
  - `kwargs.pop("name", default)` / `kwargs.get(...)` adds `name`;
  - a dataclass gets the fields of all its dataclass bases; NamedTuple / TypedDict get their fields.
Files are parsed again only when their modification time changes, so new or edited classes show up on the next request.
"""
import ast
import json
import os
import re
import threading
from pathlib import Path

SKIP_DIRS = {"__pycache__", "node_modules", "build", "dist", "venv", "env", "site-packages"}
ENUM_BASES = {"enum.Enum", "enum.IntEnum", "enum.StrEnum", "enum.Flag", "enum.IntFlag", "enum.ReprEnum"}
FLAG_BASES = {"enum.Flag", "enum.IntFlag"}
SCALARS = {"bool": "bool", "int": "int", "float": "float", "str": "str", "bytes": "str"}
SEQUENCES = {"List", "list", "Sequence", "Iterable", "Set", "set", "FrozenSet", "frozenset", "Collection",
             "MutableSequence", "Deque", "deque", "Iterator", "Tuple", "tuple"}
MAPPINGS = {"Dict", "dict", "Mapping", "MutableMapping", "OrderedDict", "DefaultDict"}
PATHS = {"pathlib.Path", "pathlib.PurePath", "os.PathLike"}
ANY = {"k": "any"}


def dotted(expr):
    """['a', 'b', 'c'] for the expression a.b.c, None for anything else."""
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return parts[::-1]
    return None


def _last(expr):
    parts = dotted(expr)
    return parts[-1] if parts else ""


def first_line(text, limit=200):
    para = (text or "").strip().split("\n\n")[0]
    para = " ".join(para.split())
    return para if len(para) <= limit else para[: limit - 3] + "..."


# --------------------------------------------------------------------------- docstrings

_SECTION = re.compile(r"^(Args|Arguments|Parameters|Params|Attributes)\s*:\s*$")
_ITEM = re.compile(r"^\*{0,2}(\w+)\s*([(:].*)?$")


def doc_args(doc):
    """{argument: description} from the Args / Arguments / Attributes sections of a Google-style docstring."""
    out, in_section, item_indent, cur = {}, False, None, None
    for raw in (doc or "").splitlines():
        s = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if indent == 0 and _SECTION.match(s):
            in_section, item_indent, cur = True, None, None
            continue
        if not in_section or not s:
            continue
        if indent == 0:                      # Returns:, Raises:, any text at the left margin ends the section
            in_section = False
            continue
        if item_indent is None:
            item_indent = indent
        if indent <= item_indent:
            m = _ITEM.match(s)
            if m:
                cur = m.group(1)
                out[cur] = [m.group(2) or ""]
                continue
        if cur:
            out[cur].append(s)
    result = {}
    for name, lines in out.items():
        text = " ".join(lines).strip()
        if text.startswith("("):             # drop the "(type)" part, which can span several lines
            depth = 0
            for i, ch in enumerate(text):
                depth += (ch == "(") - (ch == ")")
                if depth == 0:
                    text = text[i + 1:]
                    break
        text = text.lstrip(": ").strip()
        if text:
            result[name] = text if len(text) <= 400 else text[:397] + "..."
    return result


# --------------------------------------------------------------------------- per-file parsing

class ClassInfo:
    def __init__(self, mod, node, qual, scope):
        self.mod, self.node, self.qual, self.scope = mod, node, qual, scope
        self.module, self.name, self.fqn = mod.name, node.name, f"{mod.name}.{qual}"
        self.bases = list(node.bases)
        self.base_fqns = []
        self.decorators = [".".join(dotted(d.func if isinstance(d, ast.Call) else d) or []) for d in node.decorator_list]
        self.doc = ast.get_docstring(node) or ""
        self.init, self.methods = None, {}
        self.abstract_own, self.concrete_own = set(), set()
        self.fields, self.assigns = [], []
        for st in node.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.methods[st.name] = st
                if st.name == "__init__":
                    self.init = st
                decos = {_last(d.func if isinstance(d, ast.Call) else d) for d in st.decorator_list}
                (self.abstract_own if "abstractmethod" in decos else self.concrete_own).add(st.name)
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                self.fields.append((st.target.id, st.annotation, st.value))
            elif isinstance(st, ast.Assign):
                self.assigns += [(t.id, st.value) for t in st.targets if isinstance(t, ast.Name)]
        self.is_dataclass = any(d.rpartition(".")[2] == "dataclass" for d in self.decorators)


class Module:
    def __init__(self, name, path, is_pkg):
        self.name, self.path, self.is_pkg = name, path, is_pkg
        self.imports, self.classes, self.typevars, self.aliases = {}, {}, {}, {}
        self.add_args = []                    # (flag, type expression) of parser.add_argument("--flag", type=...)
        self.error = None


def _relative_base(mod, level, module):
    if not level:
        return module or ""
    parts = mod.name.split(".")
    keep = len(parts) - (level - 1 if mod.is_pkg else level)
    base = parts[: max(keep, 0)]
    return ".".join(base + ([module] if module else []))


def _collect(mod, body, prefix):
    top = prefix == ""
    for st in body:
        if isinstance(st, ast.ClassDef):
            qual = prefix + st.name
            mod.classes[qual] = ClassInfo(mod, st, qual, prefix.rstrip("."))
            _collect(mod, st.body, qual + ".")
        elif isinstance(st, ast.If):
            _collect(mod, st.body, prefix)
            _collect(mod, st.orelse, prefix)
        elif isinstance(st, ast.Try):
            for block in (st.body, st.orelse, st.finalbody, *[h.body for h in st.handlers]):
                _collect(mod, block, prefix)
        elif isinstance(st, ast.With):
            _collect(mod, st.body, prefix)
        elif not top:
            continue
        elif isinstance(st, ast.Import):
            for a in st.names:
                if a.asname:
                    mod.imports[a.asname] = a.name
                else:
                    mod.imports[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(st, ast.ImportFrom):
            base = _relative_base(mod, st.level, st.module)
            for a in st.names:
                if a.name != "*":
                    mod.imports[a.asname or a.name] = f"{base}.{a.name}" if base else a.name
        elif isinstance(st, (ast.Assign, ast.AnnAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            value = st.value
            if len(targets) != 1 or not isinstance(targets[0], ast.Name) or value is None:
                continue
            name = targets[0].id
            if isinstance(value, ast.Call) and _last(value.func) == "TypeVar":
                bound = next((k.value for k in value.keywords if k.arg == "bound"), None)
                mod.typevars[name] = bound
            elif isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)) or (
                    isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr)):
                mod.aliases[name] = value


def parse_module(path, name, is_pkg):
    mod = Module(name, path, is_pkg)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except (SyntaxError, ValueError, RecursionError) as err:   # e.g. syntax newer than the Python running the launcher
        mod.error = f"{type(err).__name__}: {err}"
        return mod
    _collect(mod, tree.body, "")
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith("--")):
            kind = next((k.value for k in node.keywords if k.arg == "type"), None)
            if kind is not None:
                mod.add_args.append((node.args[0].value[2:].replace("-", "_"), kind))
    return mod


# --------------------------------------------------------------------------- parameters

class Param:
    __slots__ = ("name", "ann", "default", "has_default", "factory", "origin", "mod", "scope", "doc")

    def __init__(self, name, ann, default, has_default, origin, mod, scope, doc="", factory=None):
        self.name, self.ann, self.default, self.has_default, self.factory = name, ann, default, has_default, factory
        self.origin, self.mod, self.scope, self.doc = origin, mod, scope, doc

    def clone(self, **changes):
        p = Param(self.name, self.ann, self.default, self.has_default, self.origin, self.mod, self.scope, self.doc, self.factory)
        for k, v in changes.items():
            setattr(p, k, v)
        return p


def _c3(seqs):
    seqs = [list(s) for s in seqs if s]
    out = []
    while seqs:
        for s in seqs:
            head = s[0]
            if not any(head in t[1:] for t in seqs):
                break
        else:
            return None
        out.append(head)
        seqs = [[x for x in s if x != head] for s in seqs]
        seqs = [s for s in seqs if s]
    return out


class Index:
    def __init__(self, src, modules):
        self.src, self.modules = src, modules
        self.classes = {c.fqn: c for m in modules.values() for c in m.classes.values()}
        self.top = {n.split(".")[0] for n in modules}
        self.errors = [{"file": str(m.path.relative_to(src)), "error": m.error} for m in modules.values() if m.error]
        self._mro, self._params, self._abstract = {}, {}, {}
        for c in self.classes.values():
            c.base_fqns = [f for f in (self.resolve(c.mod, c.scope, b) for b in c.bases) if f]
        self.subs = {}
        for c in self.classes.values():
            for b in c.base_fqns:
                self.subs.setdefault(b, []).append(c.fqn)

    # ---- name resolution

    def internal(self, fqn):
        return fqn.split(".")[0] in self.top

    def resolve(self, mod, scope, expr, depth=0):
        """Fully qualified name of a Name / Attribute / Subscript / "forward reference" expression, or None."""
        if depth > 6:
            return None
        if isinstance(expr, ast.Subscript):
            return self.resolve(mod, scope, expr.value, depth + 1)
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                return self.resolve(mod, scope, ast.parse(expr.value.strip(), mode="eval").body, depth + 1)
            except SyntaxError:
                return None
        parts = dotted(expr)
        if not parts:
            return None
        head, rest = parts[0], parts[1:]
        q = scope
        while True:                                         # nested classes visible from the enclosing classes
            cand = f"{q}.{head}" if q else head
            if cand in mod.classes:
                return self.canonical(".".join([f"{mod.name}.{cand}", *rest]))
            if not q:
                break
            q = q.rpartition(".")[0]
        if head in mod.aliases and not rest:
            return self.resolve(mod, "", mod.aliases[head], depth + 1)
        if head in mod.imports:
            return self.canonical(".".join([mod.imports[head], *rest]))
        return None

    def canonical(self, fqn):
        """Follow re-exports (`from .x import Y` in a package) until the name of a known class."""
        for _ in range(8):
            if fqn in self.classes:
                return fqn
            parts = fqn.split(".")
            for i in range(len(parts) - 1, 0, -1):
                mod = self.modules.get(".".join(parts[:i]))
                if mod is not None and parts[i] in mod.imports:
                    fqn = ".".join([mod.imports[parts[i]], *parts[i + 1:]])
                    break
            else:
                return fqn
        return fqn

    # ---- hierarchy

    def mro(self, fqn, _stack=()):
        if fqn in self._mro:
            return self._mro[fqn]
        c = self.classes.get(fqn)
        if c is None or fqn in _stack:
            return [fqn]
        seqs = [self.mro(b, _stack + (fqn,)) for b in c.base_fqns] + [list(c.base_fqns)]
        merged = _c3(seqs)
        if merged is None:                                  # inconsistent hierarchy: depth-first order
            merged, seen = [], set()
            for s in seqs[:-1]:
                for x in s:
                    if x not in seen:
                        seen.add(x)
                        merged.append(x)
        self._mro[fqn] = [fqn] + merged
        return self._mro[fqn]

    def descendants(self, fqn):
        out, todo = set(), [fqn]
        while todo:
            for s in self.subs.get(todo.pop(), []):
                if s not in out:
                    out.add(s)
                    todo.append(s)
        return out

    def is_enum(self, fqn):
        return any(b in ENUM_BASES for b in self.mro(fqn))

    def is_flag(self, fqn):
        return any(b in FLAG_BASES for b in self.mro(fqn))

    def is_group(self, fqn):
        """Dataclass, NamedTuple or TypedDict: jsonargparse nests their fields directly (no class_path / init_args)."""
        return fqn in self.classes and (self.classes[fqn].is_dataclass or any(
            b in ("typing.NamedTuple", "typing.TypedDict", "typing_extensions.TypedDict") for b in self.mro(fqn)))

    def enum_members(self, fqn):
        c = self.classes[fqn]
        return [n for n, v in c.assigns if not n.startswith("_") and not isinstance(v, ast.Lambda)]

    def abstract_set(self, fqn):
        if fqn not in self._abstract:
            c = self.classes.get(fqn)
            names = set()
            if c is not None:
                for b in c.base_fqns:
                    names |= self.abstract_set(b)
                names = (names - c.concrete_own) | c.abstract_own
            self._abstract[fqn] = names
        return self._abstract[fqn]

    def candidates(self, base):
        """Concrete public classes that derive from `base` (and `base` itself if it can be instantiated)."""
        out = []
        for fqn in self.descendants(base) | ({base} if base in self.classes else set()):
            c = self.classes[fqn]
            if any(seg.startswith("_") for seg in c.qual.split(".")) or self.abstract_set(fqn):
                continue
            if c.name.startswith("Base") and self.subs.get(fqn):    # naming convention: Base* with children is an interface
                continue
            out.append({"fqn": fqn, "name": c.name, "module": c.module, "doc": first_line(c.doc, 160)})
        return sorted(out, key=lambda c: (c["name"].lower(), c["fqn"]))

    # ---- types

    def type_desc(self, expr, mod, scope, depth=0):
        """Normalised type: {k: any|none|bool|int|float|str|literal|enum|group|class|type|ext|list|tuple|dict|callable|union}."""
        if expr is None or depth > 8:
            return ANY
        if isinstance(expr, ast.Constant):
            if expr.value is None:
                return {"k": "none"}
            if isinstance(expr.value, str):
                try:
                    return self.type_desc(ast.parse(expr.value.strip(), mode="eval").body, mod, scope, depth + 1)
                except SyntaxError:
                    return ANY
            return ANY
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            return self._union([self.type_desc(expr.left, mod, scope, depth + 1), self.type_desc(expr.right, mod, scope, depth + 1)])
        if isinstance(expr, ast.Subscript):
            head = _last(expr.value)
            elts = expr.slice.elts if isinstance(expr.slice, ast.Tuple) else [expr.slice]
            sub = lambda e: self.type_desc(e, mod, scope, depth + 1)
            if head == "Optional":
                return self._union([sub(elts[0]), {"k": "none"}])
            if head == "Union":
                return self._union([sub(e) for e in elts])
            if head == "Literal":
                return {"k": "literal", "values": [self._literal(e) for e in elts]}
            if head in ("Annotated", "ClassVar", "Final", "InitVar", "Incomplete", "Required", "NotRequired"):
                return sub(elts[0])
            if head in ("Type", "type"):
                return {"k": "type", "of": sub(elts[0])}
            if head in ("Tuple", "tuple"):
                if len(elts) == 2 and isinstance(elts[1], ast.Constant) and elts[1].value is Ellipsis:
                    return {"k": "list", "of": sub(elts[0])}
                return {"k": "tuple", "of": [sub(e) for e in elts]}
            if head in SEQUENCES:
                return {"k": "list", "of": sub(elts[0])}
            if head in MAPPINGS:
                return {"k": "dict", "of": [sub(e) for e in elts[:2]]}
            if head == "Callable":
                return {"k": "callable"}
            return self.type_desc(expr.value, mod, scope, depth + 1)       # generic class: Base[Item]
        parts = dotted(expr)
        if not parts:
            return ANY
        name = parts[-1]
        if len(parts) == 1 and name in mod.typevars:
            bound = mod.typevars[name]
            return self.type_desc(bound, mod, "", depth + 1) if bound is not None else ANY
        if len(parts) == 1 and name in SCALARS and name not in mod.imports and name not in mod.classes:
            return {"k": SCALARS[name]}
        fqn = self.resolve(mod, scope, expr)
        if fqn is None:
            return ANY
        if fqn in self.classes:
            if self.is_enum(fqn):
                return {"k": "enum", "fqn": fqn, "flag": self.is_flag(fqn)}
            return {"k": "group" if self.is_group(fqn) else "class", "fqn": fqn}
        short = fqn.rpartition(".")[2]
        if fqn.startswith(("typing.", "typing_extensions.", "collections.abc.", "collections.")):
            if short in SEQUENCES:
                return {"k": "list", "of": ANY}
            if short in MAPPINGS:
                return {"k": "dict", "of": [ANY, ANY]}
            return {"k": "callable"} if short == "Callable" else ANY
        if fqn in PATHS:
            return {"k": "str"}
        if self.internal(fqn):
            return {"k": "class", "fqn": fqn, "missing": True}
        return {"k": "ext", "name": fqn}

    @staticmethod
    def _literal(e):
        try:
            return ast.literal_eval(e)
        except (ValueError, SyntaxError):
            return e.attr if isinstance(e, ast.Attribute) else ast.unparse(e)

    @staticmethod
    def _union(items):
        flat = []
        for d in items:
            for m in (d["of"] if d["k"] == "union" else [d]):
                if m not in flat:
                    flat.append(m)
        return flat[0] if len(flat) == 1 else {"k": "union", "of": flat}

    # ---- constructor arguments

    def _docs(self, c):
        docs = doc_args(c.doc)
        if c.init is not None:
            docs.update(doc_args(ast.get_docstring(c.init) or ""))
        return docs

    def _next_init(self, fqn, missing):
        """The class whose constructor `super().__init__` reaches: first class after `fqn` in the MRO that we can read."""
        for k in self.mro(fqn)[1:]:
            c = self.classes.get(k)
            if c is None:
                if self.internal(k):
                    missing.add(k)
                    return None
                continue                                    # library class (torch, ...): not readable, skip it
            if c.init is not None or c.is_dataclass or self.is_group(k):
                return k
        return None

    def _super_call(self, c):
        best = None
        for node in ast.walk(c.init):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "__init__"):
                continue
            v, args, target = node.func.value, node.args, None
            if not (isinstance(v, ast.Call) and _last(v.func) == "super"):
                target = self.resolve(c.mod, c.scope, v)    # Parent.__init__(self, ...)
                if not target:
                    continue
                args = args[1:]
            if best is None or node.lineno < best[0].lineno:
                best = (node, target, args)
        if best is None:
            return None
        node, target, args = best
        npos = 0
        for a in args:
            if isinstance(a, ast.Starred):
                break
            npos += 1
        kw = c.init.args.kwarg.arg
        return {"target": target, "npos": npos, "given": {k.arg for k in node.keywords if k.arg},
                "forwards": any(k.arg is None and isinstance(k.value, ast.Name) and k.value.id == kw for k in node.keywords)}

    @staticmethod
    def _kwargs_reads(init, kw):
        out = []
        for n in ast.walk(init):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("pop", "get", "setdefault")
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == kw and n.args
                    and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
                default = n.args[1] if len(n.args) > 1 else None
                out.append((n.args[0].value, default, default is not None or n.func.attr != "pop"))
            elif (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == kw
                  and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)):
                out.append((n.slice.value, None, False))
        return out

    def _field_params(self, fqn, missing):
        """Dataclass fields over the MRO (base-most first, like dataclasses do), NamedTuple / TypedDict fields."""
        fields = {}
        for k in reversed(self.mro(fqn)):
            c = self.classes.get(k)
            if c is None:
                if self.internal(k):
                    missing.add(k)
                continue
            if not (c.is_dataclass or self.is_group(k)):
                continue
            docs = self._docs(c)
            for name, ann, value in c.fields:
                if _last(ann.value if isinstance(ann, ast.Subscript) else ann) == "ClassVar":
                    continue
                default, has_default, factory = value, value is not None, None
                if isinstance(value, ast.Call) and _last(value.func) == "field":
                    kws = {kw.arg: kw.value for kw in value.keywords if kw.arg}
                    if isinstance(kws.get("init"), ast.Constant) and kws["init"].value is False:
                        fields.pop(name, None)
                        continue
                    default, factory = kws.get("default"), kws.get("default_factory")
                    has_default = default is not None or factory is not None
                fields[name] = Param(name, ann, default, has_default, k, c.mod, c.qual, docs.get(name, ""), factory)
        return list(fields.values())

    def params_of(self, fqn, _stack=()):
        """(parameters, unresolved internal base classes) of a class, as jsonargparse would expose them."""
        if fqn in self._params:
            return self._params[fqn]
        c = self.classes.get(fqn)
        if c is None or fqn in _stack:
            return [], set()
        missing, stack = set(), _stack + (fqn,)
        if c.init is None and (c.is_dataclass or self.is_group(fqn)):
            params = self._field_params(fqn, missing)
        elif c.init is None:
            parent = self._next_init(fqn, missing)
            params = list(self.params_of(parent, stack)[0]) if parent else []
            if parent:
                missing |= self.params_of(parent, stack)[1]
        else:
            params = self._init_params(c, missing, stack)
        self._params[fqn] = (params, missing)
        return params, missing

    def _init_params(self, c, missing, stack):
        a = c.init.args
        docs = self._docs(c)
        pos = a.posonlyargs + a.args
        defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
        own = [Param(p.arg, p.annotation, d, d is not None, c.fqn, c.mod, c.qual, docs.get(p.arg, ""))
               for p, d in zip(pos, defaults) if p.arg not in ("self", "cls")]
        own += [Param(p.arg, p.annotation, d, d is not None, c.fqn, c.mod, c.qual, docs.get(p.arg, ""))
                for p, d in zip(a.kwonlyargs, a.kw_defaults)]
        if a.kwarg is None:
            return own
        call = self._super_call(c)
        parent_params = []
        if call and call["forwards"]:
            parent = call["target"] if call["target"] in self.classes else self._next_init(c.fqn, missing)
            if parent:
                parent_params, parent_missing = self.params_of(parent, stack)
                missing |= parent_missing
                dropped = {p.name for p in parent_params[: call["npos"]]} | call["given"]
                names = {p.name for p in own}
                own += [p for p in parent_params if p.name not in dropped and p.name not in names]
        names = {p.name for p in own}
        for name, default, has_default in self._kwargs_reads(c.init, a.kwarg.arg):
            if name in names:
                continue
            names.add(name)
            inherited = next((p for p in parent_params if p.name == name), None)
            if inherited is not None:                       # the parent declares its type; the popped default wins
                own.append(inherited.clone(default=default, has_default=has_default, doc=inherited.doc or docs.get(name, "")))
            else:
                own.append(Param(name, None, default, has_default, c.fqn, c.mod, c.qual, docs.get(name, "")))
        return own

    def param_type(self, p):
        return self.type_desc(p.ann, p.mod, p.scope)

    def type_text(self, p):
        if p.ann is None:
            return ""
        ann = p.ann.value if isinstance(p.ann, ast.Constant) and isinstance(p.ann.value, str) else None
        text = (ann if ann is not None else ast.unparse(p.ann)).replace("typing.", "")
        return text if len(text) <= 70 else text[:67] + "..."

    def default_text(self, p):
        """(text to show, config-style text to prefill or None)."""
        if p.factory is not None:
            text = ast.unparse(p.factory)
            return (text if text.startswith("lambda") else text + "()"), None
        e = p.default
        if e is None:
            return ("" if not p.has_default else "None"), ("NULL" if p.has_default else None)
        member = self._enum_text(e, p)
        if member:
            return member, member
        try:
            value = ast.literal_eval(e)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            text = ast.unparse(e)
            return (text if len(text) <= 60 else text[:57] + "..."), None
        if value is None:
            return "None", "NULL"
        if isinstance(value, bool):
            return str(value), str(value)
        if isinstance(value, str):
            return repr(value) if len(value) <= 60 else repr(value[:57]) + "...", value
        if isinstance(value, (int, float)):
            return repr(value), repr(value)
        try:
            flow = json.dumps(value)
        except (TypeError, ValueError):
            flow = None
        return ast.unparse(e)[:60], flow

    def _enum_text(self, e, p):
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.BitOr):
            l, r = self._enum_text(e.left, p), self._enum_text(e.right, p)
            return f"{l} | {r}" if l and r else None
        if isinstance(e, ast.Attribute) and re.fullmatch(r"[A-Z][A-Z0-9_]*", e.attr):
            owner = self.resolve(p.mod, p.scope, e.value)
            if owner is not None and (self.internal(owner) or owner in self.classes):
                return e.attr
        return None

    # ---- entry points of the program

    def roots(self):
        """[(name, class fqn)] of the top-level arguments: parser.add_argument("--task", type=BaseTask) ..."""
        out = []
        for m in self.modules.values():
            for flag, expr in m.add_args:
                fqn = self.resolve(m, "", expr)
                if fqn in self.classes and flag not in [o[0] for o in out]:
                    out.append((flag, fqn))
        return out

    def links(self, fqn):
        """{target: source} of the argument links a task class declares in `links()` (dict literal), dotted names."""
        for k in self.mro(fqn):
            c = self.classes.get(k)
            fn = c.methods.get("links") if c else None
            if fn is None:
                continue
            for st in fn.body:
                if isinstance(st, ast.Return) and isinstance(st.value, ast.Dict):
                    out = {}
                    for key, val in zip(st.value.keys, st.value.values):
                        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                            continue
                        targets = val.elts if isinstance(val, (ast.List, ast.Tuple)) else [val]
                        for t in targets:
                            if isinstance(t, ast.Constant) and isinstance(t.value, str):
                                out[t.value] = key.value
                    return out
        return {}

    def summary(self):
        return {"src": str(self.src), "files": len(self.modules), "classes": len(self.classes), "errors": self.errors}


# --------------------------------------------------------------------------- scanning with a cache

_LOCK = threading.Lock()
_FILES = {}          # path -> (mtime_ns, size, Module)
_CACHE = {"key": None, "index": None}


def _scan(src):
    found = []
    for folder, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            if f.endswith(".py"):
                p = Path(folder) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                found.append((p, st.st_mtime_ns, st.st_size))
    return found


def _module_name(src, path):
    parts = list(path.relative_to(src).with_suffix("").parts)
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts.pop()
    return ".".join(parts), is_pkg


def get_index(src, fresh=False):
    src = Path(src).resolve()
    with _LOCK:
        if fresh:
            _FILES.clear()
            _CACHE["key"] = None
        found = _scan(src)
        key = (str(src), tuple(found))
        if _CACHE["key"] == key:
            return _CACHE["index"]
        modules = {}
        for path, mtime, size in found:
            name, is_pkg = _module_name(src, path)
            if not name:
                continue
            cached = _FILES.get(path)
            if cached is None or cached[:2] != (mtime, size) or cached[2].name != name:
                cached = (mtime, size, parse_module(path, name, is_pkg))
                _FILES[path] = cached
            modules[name] = cached[2]
        index = Index(src, modules)
        _CACHE.update(key=key, index=index)
        return index
