"""Standalone binary FBX (7.x) reader.

No Autodesk FBX SDK, no Blender. Implements the record tree + property
decoding, including zlib / delta / zigzag compressed arrays.
"""
import os
import struct
import zlib


class FbxNode:
    __slots__ = ("name", "props", "children", "ps", "plen", "cls")

    def __init__(self, name, props, children, ps=0, plen=0):
        self.name = name
        self.props = props
        self.children = children
        self.ps = ps          # absolute offset of the property list
        self.plen = plen      # byte length of the property list
        self.cls = props[2] if len(props) > 2 and isinstance(props[2], str) else name

    def find(self, name):
        return [c for c in self.children if c.name == name]

    def get(self, name):
        for c in self.children:
            if c.name == name:
                return c
        return None

    def walk(self):
        yield self
        for c in self.children:
            for n in c.walk():
                yield n

    def __repr__(self):
        return "<%s props=%d kids=%d>" % (self.name, len(self.props),
                                          len(self.children))


def _read_prop(d, p):
    t = chr(d[p])
    p += 1
    if t == "Y":
        v = struct.unpack_from("<h", d, p)[0]
        p += 2
    elif t == "C":
        v = d[p]
        p += 1
    elif t == "I":
        v = struct.unpack_from("<i", d, p)[0]
        p += 4
    elif t == "F":
        v = struct.unpack_from("<f", d, p)[0]
        p += 4
    elif t == "D":
        v = struct.unpack_from("<d", d, p)[0]
        p += 8
    elif t == "L":
        v = struct.unpack_from("<q", d, p)[0]
        p += 8
    elif t == "S":
        n = struct.unpack_from("<I", d, p)[0]
        p += 4
        v = d[p:p + n].decode("utf-8", "replace")
        p += n
    elif t == "R":
        n = struct.unpack_from("<I", d, p)[0]
        p += 4
        v = d[p:p + n]
        p += n
    elif t in "fdlibc":
        n, enc, ln = struct.unpack_from("<III", d, p)
        p += 12
        raw = d[p:p + ln]
        p += ln
        if enc == 1:
            raw = zlib.decompress(raw)
        if t == "f":
            v = list(struct.unpack_from("<%df" % n, raw, 0))
        elif t == "d":
            v = list(struct.unpack_from("<%dd" % n, raw, 0))
        elif t in "il":
            fmt = "<%d" % n + ("i" if t == "i" else "q")
            v = list(struct.unpack_from(fmt, raw, 0))
            # NOTE: Autodesk docs describe delta+zigzag for deflated integer
            # arrays, but files written by the Blender exporter (verified on
            # FBX 7400) store plain little-endian ints.  Decoding them as
            # delta produces out-of-range polygon indices, so we keep raw.
            # Set FBX_INT_DELTA=1 in the environment to force delta decoding.
            if enc == 1 and os.environ.get("FBX_INT_DELTA") == "1":
                for i in range(1, n):
                    v[i] = (v[i] >> 1) ^ -(v[i] & 1)
                    v[i] += v[i - 1]
        else:
            v = list(raw)
    else:
        raise ValueError("unknown property type %r at %d" % (t, p))
    return v, p


def _read_node(d, p, wide):
    if wide:
        end, nprop, plen = struct.unpack_from("<QQQ", d, p)
        p += 24
    else:
        end, nprop, plen = struct.unpack_from("<III", d, p)
        p += 12
    if end == 0:
        return None, p + (24 if wide else 12)
    nlen = d[p]
    p += 1
    name = d[p:p + nlen].decode("utf-8", "replace")
    p += nlen
    props_start = p
    props = []
    for _ in range(nprop):
        val, p = _read_prop(d, p)
        props.append(val)
    p = props_start + plen
    children = []
    while p < end:
        ch, p2 = _read_node(d, p, wide)
        if ch is None:
            p = p2
            break
        children.append(ch)
        p = p2
    p = end
    return FbxNode(name, props, children, props_start, plen), p


def load(path):
    d = open(path, "rb").read()
    assert d[:20] == b"Kaydara FBX Binary  ", "not a binary FBX"
    ver = struct.unpack_from("<I", d, 23)[0]
    wide = ver >= 7500
    p = 27
    top = []
    while p < len(d) - 32:
        try:
            node, p2 = _read_node(d, p, wide)
        except Exception:
            break
        if node is None:
            break
        top.append(node)
        if p2 <= p:
            break
        p = p2
    return ver, top


def index_by_id(nodes):
    """Collect every node that carries an (id, name) pair, keyed by id."""
    out = {}
    for n in nodes:
        if n.props and isinstance(n.props[0], int) and len(n.props) >= 3:
            out[n.props[0]] = n
    return out
