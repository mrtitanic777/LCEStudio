"""Named Binary Tag (NBT) engine for Minecraft: Xbox 360 (LCE) legacy data.

Big-endian, uncompressed NBT as used by LCE legacy chunks, player files and
level.dat. Parses the whole tag tree into editable Python objects and serializes
it back byte-for-byte, so the editor can change *any* field instead of hunting
byte patterns.

Representation (round-trip safe -- every value keeps its tag id):
    compound  -> Compound: an ordered dict  {name: Tag}
    list      -> List:     .etype (element tag id) + a python list of values
    Tag       -> .id (tag id) + .value
Primitive values are python int / float / str / bytes; byte/int arrays are lists
of ints.  Use the helpers on Compound (get/get_value/set/add/remove) to edit.

    root_name, root = nbt.parse(data)          # root is usually a Compound
    data2 = nbt.serialize(root_name, root)     # -> bytes, identical for a no-op
"""
import struct
from collections import OrderedDict

# ---- tag ids -------------------------------------------------------------
END, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE, BYTE_ARRAY, STRING, LIST, \
    COMPOUND, INT_ARRAY = range(12)

NAMES = {END: "End", BYTE: "Byte", SHORT: "Short", INT: "Int", LONG: "Long",
         FLOAT: "Float", DOUBLE: "Double", BYTE_ARRAY: "ByteArray",
         STRING: "String", LIST: "List", COMPOUND: "Compound",
         INT_ARRAY: "IntArray"}

# python-friendly aliases for building tags in code / the CLI
ALIAS = {"byte": BYTE, "short": SHORT, "int": INT, "long": LONG, "float": FLOAT,
         "double": DOUBLE, "bytes": BYTE_ARRAY, "string": STRING, "str": STRING,
         "list": LIST, "compound": COMPOUND, "ints": INT_ARRAY}


class Tag:
    """A typed value: tag id + python value."""
    __slots__ = ("id", "value")

    def __init__(self, tid, value):
        self.id = tid
        self.value = value

    def __repr__(self):
        return "Tag(%s, %r)" % (NAMES.get(self.id, self.id), self.value)


class List:
    """A TAG_List: an element tag id and a python list of raw values."""
    __slots__ = ("etype", "items")

    def __init__(self, etype=END, items=None):
        self.etype = etype
        self.items = items if items is not None else []

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        return self.items[i]

    def __repr__(self):
        return "List<%s>(%d)" % (NAMES.get(self.etype, self.etype), len(self.items))


class Compound(OrderedDict):
    """A TAG_Compound: ordered {name: Tag}. Helpers make editing ergonomic."""

    # -- read ---------------------------------------------------------------
    def get_tag(self, name):
        return OrderedDict.get(self, name)

    def get_value(self, name, default=None):
        t = OrderedDict.get(self, name)
        return t.value if t is not None else default

    def path(self, dotted, default=None):
        """Navigate 'a.b.c' through compounds; returns the Tag or default."""
        cur = self
        for part in dotted.split("."):
            if not isinstance(cur, Compound):
                return default
            cur = OrderedDict.get(cur, part)
            if cur is None:
                return default
            cur = cur.value if part != dotted.split(".")[-1] else cur
        return cur

    # -- write --------------------------------------------------------------
    def set(self, name, tid, value):
        """Set/replace a tag by explicit id + value. Returns self (chainable)."""
        self[name] = Tag(tid, value)
        return self

    def set_value(self, name, value):
        """Change an existing tag's value, keeping its id."""
        t = OrderedDict.get(self, name)
        if t is None:
            raise KeyError("no tag %r to set_value (use set with a type)" % name)
        t.value = value
        return self

    def remove(self, name):
        self.pop(name, None)
        return self


# ---- reading -------------------------------------------------------------
def _read_string(d, o):
    (n,) = struct.unpack_from(">H", d, o)
    o += 2
    s = d[o:o + n].decode("utf-8", "replace")
    return s, o + n


def _read_payload(tid, d, o):
    if tid == BYTE:
        return struct.unpack_from(">b", d, o)[0], o + 1
    if tid == SHORT:
        return struct.unpack_from(">h", d, o)[0], o + 2
    if tid == INT:
        return struct.unpack_from(">i", d, o)[0], o + 4
    if tid == LONG:
        return struct.unpack_from(">q", d, o)[0], o + 8
    if tid == FLOAT:
        return struct.unpack_from(">f", d, o)[0], o + 4
    if tid == DOUBLE:
        return struct.unpack_from(">d", d, o)[0], o + 8
    if tid == BYTE_ARRAY:
        (n,) = struct.unpack_from(">i", d, o); o += 4
        return list(d[o:o + n]), o + n
    if tid == STRING:
        return _read_string(d, o)
    if tid == LIST:
        et = d[o]; o += 1
        (n,) = struct.unpack_from(">i", d, o); o += 4
        items = []
        for _ in range(n):
            v, o = _read_payload(et, d, o)
            items.append(v)
        return List(et, items), o
    if tid == COMPOUND:
        c = Compound()
        while True:
            et = d[o]; o += 1
            if et == END:
                break
            name, o = _read_string(d, o)
            v, o = _read_payload(et, d, o)
            c[name] = Tag(et, v)
        return c, o
    if tid == INT_ARRAY:
        (n,) = struct.unpack_from(">i", d, o); o += 4
        vals = list(struct.unpack_from(">%di" % n, d, o))
        return vals, o + 4 * n
    raise ValueError("unknown tag id %d at offset %d" % (tid, o))


def parse(d, offset=0):
    """Parse a named root tag. Returns (root_name, root_value)."""
    tid = d[offset]; o = offset + 1
    if tid == END:
        return "", None
    name, o = _read_string(d, o)
    value, o = _read_payload(tid, d, o)
    return name, value


def parse_tag(d, offset=0):
    """Like parse but returns (name, Tag)."""
    tid = d[offset]
    if tid == END:
        return "", Tag(END, None), offset + 1
    name, o = _read_string(d, offset + 1)
    value, o = _read_payload(tid, d, o)
    return name, Tag(tid, value), o


# ---- writing -------------------------------------------------------------
def _write_string(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _tid_of(value):
    """Infer a tag id for a python value that isn't wrapped (best effort)."""
    if isinstance(value, Compound):
        return COMPOUND
    if isinstance(value, List):
        return LIST
    if isinstance(value, bool):
        return BYTE
    if isinstance(value, int):
        return INT
    if isinstance(value, float):
        return DOUBLE
    if isinstance(value, str):
        return STRING
    if isinstance(value, (bytes, bytearray)):
        return BYTE_ARRAY
    raise TypeError("cannot infer tag id for %r" % type(value))


def _write_payload(tid, v):
    if tid == BYTE:
        return struct.pack(">b", int(v))
    if tid == SHORT:
        return struct.pack(">h", int(v))
    if tid == INT:
        return struct.pack(">i", int(v))
    if tid == LONG:
        return struct.pack(">q", int(v))
    if tid == FLOAT:
        return struct.pack(">f", float(v))
    if tid == DOUBLE:
        return struct.pack(">d", float(v))
    if tid == BYTE_ARRAY:
        b = bytes(bytearray(x & 0xFF for x in v)) if not isinstance(v, (bytes, bytearray)) else bytes(v)
        return struct.pack(">i", len(b)) + b
    if tid == STRING:
        return _write_string(v)
    if tid == LIST:
        et = v.etype if isinstance(v, List) else (_tid_of(v[0]) if v else END)
        items = v.items if isinstance(v, List) else list(v)
        out = bytes([et]) + struct.pack(">i", len(items))
        for it in items:
            out += _write_payload(et, it)
        return out
    if tid == COMPOUND:
        out = bytearray()
        for name, tag in v.items():
            out += bytes([tag.id]) + _write_string(name) + _write_payload(tag.id, tag.value)
        out += bytes([END])
        return bytes(out)
    if tid == INT_ARRAY:
        return struct.pack(">i", len(v)) + struct.pack(">%di" % len(v), *v)
    raise ValueError("unknown tag id %d" % tid)


def serialize(root_name, root_value, root_tid=COMPOUND):
    """Serialize a named root tag back to bytes."""
    return bytes([root_tid]) + _write_string(root_name) + _write_payload(root_tid, root_value)


# ---- display (for CLI / GUI tree) ---------------------------------------
def to_tree(name, tag):
    """Convert a (name, Tag) into a nested dict for a UI tree:
    {name, type, value|None, children:[...]}"""
    node = {"name": name, "type": NAMES.get(tag.id, str(tag.id)), "tid": tag.id}
    if tag.id == COMPOUND:
        node["children"] = [to_tree(n, t) for n, t in tag.value.items()]
    elif tag.id == LIST:
        et = tag.value.etype
        node["children"] = [to_tree("[%d]" % i, Tag(et, v))
                            for i, v in enumerate(tag.value.items)]
        node["type"] = "List<%s>" % NAMES.get(et, et)
    elif tag.id in (BYTE_ARRAY, INT_ARRAY):
        node["value"] = "[%d %s]" % (len(tag.value), "bytes" if tag.id == BYTE_ARRAY else "ints")
    else:
        node["value"] = tag.value
    return node


def dumps(name, tag, indent=0):
    """Human-readable NBT dump (SNBT-ish)."""
    pad = "  " * indent
    label = "%s%s(%r): " % (pad, NAMES.get(tag.id, tag.id), name) if name else pad
    if tag.id == COMPOUND:
        out = label + "{\n"
        for n, t in tag.value.items():
            out += dumps(n, t, indent + 1) + "\n"
        return out + pad + "}"
    if tag.id == LIST:
        out = label + "[%d x %s] [\n" % (len(tag.value), NAMES.get(tag.value.etype, "?"))
        for i, v in enumerate(tag.value.items):
            out += dumps("", Tag(tag.value.etype, v), indent + 1) + "\n"
        return out + pad + "]"
    if tag.id in (BYTE_ARRAY, INT_ARRAY):
        return label + "[%d values]" % len(tag.value)
    return label + repr(tag.value)
