"""Minimal binary-FBX inspector (no SDK needed).

Usage: python fbx_probe.py <file.fbx>

Extracts, without any FBX SDK:
  - mesh / material / bone counts
  - bone names (Model + LimbNode entries)
  - whether geometry carries skin deformation data
"""
import re
import struct
import sys


def load(path):
    d = open(path, 'rb').read()
    assert d[:20] == b'Kaydara FBX Binary  ', 'not a binary FBX'
    return d, struct.unpack_from('<I', d, 23)[0]


def objects_region(d):
    """Return the byte slice of the top-level Objects node (best effort)."""
    i = d.find(b'Objects\x00')
    # the marker appears as a node name inside the record; take a generous window
    return i


def names_by_class(d, cls):
    """Find Model nodes whose property pair is  <name>\\x00\\x01Model / <cls>.

    Binary FBX stores a Model node's 2nd/3rd properties as:
        S <u32 len> <name> \\x00 \\x01 Model S <u32 len> <LimbNode|Mesh|...>
    so we match the literal 'S' + length byte + 3 zero pad on both sides.
    """
    pat = re.compile(
        rb'S.\x00\x00\x00([\x20-\x7e]{1,80}?)\x00\x01ModelS.\x00\x00\x00' + cls)
    out = []
    for m in pat.finditer(d):
        s = m.group(1).decode('ascii')
        if s and s not in out:
            out.append(s)
    return out


def main(path):
    d, ver = load(path)
    print('file      :', path)
    print('FBX ver   :', ver)
    print('size      : %.2f MB' % (len(d) / 1048576))

    for cls, label in ((b'LimbNode', 'LimbNode (bones)'),
                       (b'Mesh', 'Mesh'),
                       (b'Null', 'Null'),
                       (b'Root', 'Root')):
        ns = names_by_class(d, cls)
        print('\n[%s] count=%d' % (label, len(ns)))
        if ns:
            print('  ', ', '.join(ns[:24]) + (' ...' if len(ns) > 24 else ''))

    bones = names_by_class(d, b'LimbNode')
    print('\n=== BONES (%d) ===' % len(bones))
    for i in range(0, len(bones), 6):
        print('  ' + '  '.join('%-18s' % b for b in bones[i:i + 6]))

    # crude detection of skin data
    print('\nDeformer-ish markers: Skin=%d Cluster=%d SubDeformer=%d'
          % (d.count(b'Skin\x00'), d.count(b'Cluster\x00'), d.count(b'SubDeformer')))

    mats = names_by_class(d, b'Material')
    print('Materials:', ', '.join(mats[:20]) if mats else '(none in FBX)')

    has_tex = any(k in d for k in (b'Texture\x00', b'Video\x00', b'.png', b'.tga', b'.psd'))
    print('Embedded texture refs:', has_tex)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'x.fbx')
