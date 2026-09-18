"""Standalone FBX (7.x) reader —— 二进制 + ASCII 两种都读。

No Autodesk FBX SDK, no Blender. Implements the record tree + property
decoding, including zlib / delta / zigzag compressed arrays.

2026-09-18 起也支持 **ASCII FBX**（文本版）：工程新加的 fbxout.py 写的就是
ASCII（文本好调试、任何 DCC 都认），不补这条读取路径的话，自己写出来的 FBX
拖回本工具会直接「not a binary FBX」，也没法做往返自校验。
两种格式解析成同一棵 FbxNode 树，下游（fbx2pmx / 预览）完全无感——
尤其注意数组：二进制里 `Vertices` 的属性就是那个长数组（props[0] = [..]），
ASCII 里数据写在子节点 `a` 里，所以解析时要把 `*N { a: ... }` 折叠成同样的
「props[0] = 数组」，否则 fbx2pmx 的 `g.get("Vertices").props[0]` 会拿到垃圾。
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


def _load_binary(path):
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


# --------------------------------------------------------------- ASCII FBX --
class _Arr(object):
    """ASCII 里的 `*N` 数组标记，解析完用子节点 a 的数据替换掉。"""

    __slots__ = ("n",)

    def __init__(self, n):
        self.n = n


class _AsciiParser(object):
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)

    # -- 词法
    def _skip(self):
        """跳过空白和 `;` 行注释（换行也跳过）。"""
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == ";":
                j = s.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            else:
                break

    def _skip_inline(self):
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def _name(self):
        s, j = self.s, self.i
        while j < self.n and s[j] not in ":{ \t\r\n":
            j += 1
        nm = s[self.i:j]
        self.i = j
        return nm

    def _string(self):
        s, j = self.s, self.i + 1
        buf = []
        while j < self.n:
            c = s[j]
            if c == "\\":
                if j + 1 < self.n:
                    buf.append(s[j + 1])
                j += 2
                continue
            if c == '"':
                j += 1
                break
            buf.append(c)
            j += 1
        self.i = j
        return "".join(buf)

    def _number(self):
        s, j = self.s, self.i
        while j < self.n and s[j] in "+-.0123456789eE":
            j += 1
        tok = s[self.i:j]
        self.i = j
        try:
            return float(tok) if any(c in tok for c in ".eE") else int(tok)
        except ValueError:
            return 0

    def _token(self):
        s, j = self.s, self.i
        while j < self.n and s[j] not in " \t\r\n,{}":
            j += 1
        tok = s[self.i:j]
        self.i = max(j, self.i + 1)
        return tok

    # -- 语法
    def _array_block(self):
        """读 `{ a: v,v,v  a: v,v ... }` 这种数组块，返回展平的数值列表。

        实际存在的三种写法都要吃下（实测踩过）：
          * Autodesk 风格：整个数组一条 `a: ...` 行，可能长达几 MB；
          * 分行且每行重复行标签：`a: v,v` 换行 `a: v,v`（本工程的写出器这样写）；
          * 分行但只有第一行有标签，后续行直接续数（可能以逗号开头）。
        """
        vals = []
        self._skip()
        if not (self.i < self.n and self.s[self.i] == "{"):
            return vals
        self.i += 1
        while True:
            self._skip()
            if self.i >= self.n or self.s[self.i] == "}":
                break
            # 行首可能是行标签（`a`）也可能直接就是数值：先看一眼再决定
            save = self.i
            self._name()
            self._skip_inline()
            if self.i < self.n and self.s[self.i] == ":":
                self.i += 1
            else:
                self.i = save
            # 读这一行的数值，换行即止
            while True:
                self._skip_inline()
                if self.i >= self.n:
                    break
                c = self.s[self.i]
                if c in "\r\n}":
                    break
                if c == ",":
                    self.i += 1
                    continue
                if c == '"':
                    vals.append(self._string())
                elif c in "+-.0123456789":
                    vals.append(self._number())
                else:
                    self._token()           # 认不出的 token 丢掉，别污染数值
        if self.i < self.n and self.s[self.i] == "}":
            self.i += 1
        return vals

    def node(self):
        """解析一个节点。

        普通节点的属性必须写在同一行 —— 遇到换行即终止属性列表，否则会把
        下一行的兄弟节点名字吃进自己的 props。真正会跨行的只有数组数据，
        那部分由 `_array_block()` 专门处理。
        """
        self._skip()
        if self.i >= self.n or self.s[self.i] == "}":
            return None
        name = self._name()
        if not name:
            return None
        self._skip()
        if self.i < self.n and self.s[self.i] == ":":
            self.i += 1
        props = []
        counts = []
        rows = []
        while True:
            self._skip_inline()
            if self.i >= self.n:
                break
            c = self.s[self.i]
            if c in "{\r\n}":
                break
            if c == ",":
                self.i += 1
                continue
            if c == '"':
                props.append(self._string())
            elif c == "*":
                self.i += 1
                n_arr = self._number()
                counts.append(n_arr)
                props.append(_Arr(n_arr))
                rows.extend(self._array_block())
            elif c in "+-.0123456789":
                props.append(self._number())
            else:
                props.append(self._token())
        has_arr = any(isinstance(p, _Arr) for p in props)
        children = []
        self._skip()
        if self.i < self.n and self.s[self.i] == "{":
            self.i += 1
            while True:
                mark = self.i
                # 数组块的 `{...}` 已经被 _array_block 吃掉，这里剩下的都是
                # 普通单行属性子节点。
                ch = self.node()
                if ch is None:
                    break
                children.append(ch)
                if self.i <= mark:              # 防死循环
                    break
            self._skip()
            if self.i < self.n and self.s[self.i] == "}":
                self.i += 1
        if has_arr:
            # 按声明长度把行数据切回各个数组属性；只有一个数组时整份给它
            # （`*N` 与行数据不一致时以行数据为准，宁可多也别丢）。
            filled = []
            off = 0
            k = 0
            for p in props:
                if isinstance(p, _Arr):
                    if len(counts) > 1:
                        n = counts[k] if k < len(counts) else 0
                        filled.append(rows[off:off + n])
                        off += n
                    else:
                        filled.append(rows)
                    k += 1
                else:
                    filled.append(p)
            props = filled
        return FbxNode(name, props, children)


def _load_ascii(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    p = _AsciiParser(text)
    top = []
    while True:
        mark = p.i
        nd = p.node()
        if nd is None:
            break
        top.append(nd)
        if p.i <= mark:
            break
    ver = 7400
    for nd in top:
        if nd.name == "FBXHeaderExtension":
            for c in nd.children:
                if c.name == "FBXVersion" and c.props:
                    try:
                        ver = int(c.props[0])
                    except (TypeError, ValueError):
                        pass
    if not top:
        raise ValueError("既不是二进制 FBX，也解析不出 ASCII FBX 节点：%s" % path)
    return ver, top


def load(path):
    """返回 (version, [顶层节点])。二进制 / ASCII 自动识别。"""
    with open(path, "rb") as f:
        head = f.read(23)
    if head[:20] == b"Kaydara FBX Binary  ":
        return _load_binary(path)
    return _load_ascii(path)


def index_by_id(nodes):
    """Collect every node that carries an (id, name) pair, keyed by id."""
    out = {}
    for n in nodes:
        if n.props and isinstance(n.props[0], int) and len(n.props) >= 3:
            out[n.props[0]] = n
    return out
