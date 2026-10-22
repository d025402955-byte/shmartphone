#!/usr/bin/env python3
"""Download all files (recordings + ini settings) from a Yemot HaMashiach system.

  python3 /opt/shmartphone-app/yemotexport.py

Asks for a Yemot API token (not shown, not saved). Walks every extension with
GetIVR2Dir and saves files with DownloadFile under /var/lib/shmartphone/yemot/.
Writes index.txt listing extension -> files.
"""
import getpass
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = 'https://www.call2all.co.il/ym/api/'
OUT = '/var/lib/shmartphone/yemot'
KEEP = ('.wav', '.mp3', '.ini', '.tts', '.txt', '.ogg')
MAX_FILES = 20000


def call(token, cmd, **params):
    q = urllib.parse.urlencode({'token': token, **params})
    req = urllib.request.Request(BASE + cmd + '?' + q, headers={'authorization': token, 'User-Agent': 'curl/8.5.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def items(listing, key):
    out = []
    for it in listing.get(key) or []:
        if isinstance(it, dict):
            what = it.get('what') or it.get('path') or ''
            name = it.get('name') or what.rsplit('/', 1)[-1]
            out.append((name, what))
    return out


def main():
    token = getpass.getpass('Yemot API token: ').strip()
    if not token:
        sys.exit('no token')
    os.makedirs(OUT, exist_ok=True)
    os.chmod(OUT, 0o700)
    queue, seen, index, count, errors = ['ivr2:/'], set(), [], 0, 0
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            listing = json.loads(call(token, 'GetIVR2Dir', path=path))
        except Exception as e:
            print('list failed', path, repr(e)[:120])
            errors += 1
            continue
        if listing.get('responseStatus') != 'OK':
            print('list error', path, listing.get('responseStatus'), str(listing.get('message', ''))[:120])
            if path == 'ivr2:/':
                sys.exit('cannot read the system - check the token')
            errors += 1
            continue
        for name, what in items(listing, 'dirs'):
            sub = what or (path.rstrip('/') + '/' + name)
            if not sub.startswith('ivr2:'):
                sub = 'ivr2:' + sub
            queue.append(sub.rstrip('/') + '/')
        names = []
        for key in ('files', 'ini', 'html'):
            for name, what in items(listing, key):
                if not name.lower().endswith(KEEP):
                    continue
                src = what or (path.rstrip('/') + '/' + name)
                if not src.startswith('ivr2:'):
                    src = 'ivr2:' + src
                rel = src[len('ivr2:'):].lstrip('/')
                dest = os.path.join(OUT, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                try:
                    data = call(token, 'DownloadFile', path=src)
                    with open(dest, 'wb') as f:
                        f.write(data)
                    names.append(name)
                    count += 1
                except Exception as e:
                    print('download failed', src, repr(e)[:100])
                    errors += 1
                if count >= MAX_FILES:
                    queue = []
                    break
        index.append(f"{path}  {' '.join(sorted(names))}")
        print(f'{path}  {len(names)} files')
    with open(os.path.join(OUT, 'index.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(index) + '\n')
    print(f'\ndone: {count} files from {len(seen)} extensions, {errors} errors -> {OUT}')


if __name__ == '__main__':
    main()
