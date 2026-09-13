"""Unpack a .unitypackage without Unity.

一个 .unitypackage 只是一个经过 gzip 压缩的 tar 归档文件。每个资源都存放在一个以其 32 位十六进制 GUID 命名的文件夹中，该文件夹包含：
    asset        the real file (FBX, prefab, png, ...)
    asset.meta   Unity metadata (importer type, guid)
    pathname     original path inside Assets/
    preview.png  optional thumbnail

Usage:
    python unitypackage_unpack.py <file.unitypackage> [-o OUTDIR] [--list]
	
# 先看包里有什么（不解包）
python unitypackage_unpack.py "Sexy Sailor_Sapphy_v1.00.unitypackage" --list

# 解包到默认目录（<文件名>_extracted）
python unitypackage_unpack.py "Sexy Sailor_Sapphy_v1.00.unitypackage"

# 解包到指定目录
python unitypackage_unpack.py "Sexy Sailor_Sapphy_v1.00.unitypackage" -o "D:/output/Sapphy"

只要路径里带空格或中文，就套一层双引号，否则 shell 会按空格切参数。
"""
import argparse
import os
import shutil
import sys
import tarfile


def read_paths(t):
    """Map GUID -> original Assets/ relative path."""
    out = {}
    for m in t.getmembers():
        if not m.isfile() or not m.name.endswith('pathname'):
            continue
        guid = m.name.split('/')[0]
        out[guid] = t.extractfile(m).read().decode('utf-8', 'replace').strip()
    return out


def unpack(src, outdir, list_only=False):
    with tarfile.open(src, 'r:gz') as t:
        paths = read_paths(t)
        if list_only:
            for m in t.getmembers():
                if not m.isfile() or not m.name.endswith('asset'):
                    continue
                guid = m.name.split('/')[0]
                print('%10d  %s' % (m.size, paths.get(guid, '?')))
            return 0

        if os.path.isdir(outdir):
            shutil.rmtree(outdir)
        os.makedirs(outdir)
        n = 0
        for m in t.getmembers():
            if not m.isfile():
                continue
            guid, fn = m.name.split('/', 1)
            rel = paths.get(guid, guid).lstrip('Assets/')
            if rel.endswith('/') or rel == guid:
                rel = os.path.join(rel, 'asset')
            if fn == 'asset':
                dst = os.path.join(outdir, rel)
            else:
                base = rel if fn == 'pathname' else os.path.splitext(rel)[0]
                dst = os.path.join(outdir, os.path.dirname(rel), fn)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'wb') as w:
                w.write(t.extractfile(m).read())
            n += 1
            print('  %8.1f KB  %s' % (os.path.getsize(dst) / 1024,
                                      os.path.relpath(dst, outdir)))
        print('\n%d files -> %s' % (n, outdir))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('-o', '--out', default=None)
    ap.add_argument('--list', action='store_true')
    a = ap.parse_args()
    out = a.out or os.path.splitext(a.src)[0] + '_extracted'
    return unpack(a.src, out, a.list)


if __name__ == '__main__':
    sys.exit(main())
