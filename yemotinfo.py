#!/usr/bin/env python3
"""Summarize the Yemot export without printing personal data.

  python3 /opt/shmartphone-app/yemotinfo.py            top-level folders: files, recordings, ini keys
  python3 /opt/shmartphone-app/yemotinfo.py FOLDER     detail for one folder (file names, ini keys/values
                                                       with phone-like values masked)
"""
import collections
import os
import re
import sys

ROOT = '/var/lib/shmartphone/yemot'
PHONE = re.compile(r'0\d{8,9}')


def mask(v):
    return PHONE.sub(lambda m: m.group()[:3] + '*' * (len(m.group()) - 5) + m.group()[-2:], v)


def ini_pairs(path):
    out = []
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if '=' in line:
                    k, v = line.rstrip('\n').split('=', 1)
                    out.append((k.strip(), v.strip()))
    except OSError:
        pass
    return out


def summary():
    for name in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, name)
        if not os.path.isdir(p):
            print(f'{name:28} (file)')
            continue
        n_dirs = n_files = n_audio = 0
        keys = collections.Counter()
        for dp, dns, fns in os.walk(p):
            n_dirs += len(dns)
            for fn in fns:
                n_files += 1
                if fn.lower().endswith(('.wav', '.mp3', '.ogg')):
                    n_audio += 1
                if fn.endswith('.ini'):
                    keys.update(k for k, _ in ini_pairs(os.path.join(dp, fn)))
        top = ','.join(k for k, _ in keys.most_common(6))
        print(f'{name:28} sub={n_dirs:<5} files={n_files:<5} audio={n_audio:<5} ini: {top}')


def detail(folder):
    p = os.path.join(ROOT, folder)
    for dp, dns, fns in sorted(os.walk(p))[:40]:
        rel = os.path.relpath(dp, ROOT)
        print(f'[{mask(rel)}] {len(fns)} files: ' + ' '.join(mask(f) for f in sorted(fns)[:12]))
        for fn in sorted(fns):
            if fn.endswith('.ini'):
                for k, v in ini_pairs(os.path.join(dp, fn))[:25]:
                    print(f'    {k} = {mask(v)[:80]}')


if __name__ == '__main__':
    if len(sys.argv) > 1:
        detail(sys.argv[1])
    else:
        summary()
