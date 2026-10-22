"""Minimal reader for legacy binary Excel (.xls, BIFF8 in an OLE2 container). Stdlib only.

Returns the first worksheet as a list of rows (lists of str/float). Supports the
cell records produced by common exporters: LABELSST, LABEL, NUMBER, RK, MULRK.
"""
import struct

OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
ENDOFCHAIN = 0xFFFFFFFE


def _ole_stream(data, wanted=('Workbook', 'Book')):
    if data[:8] != OLE_MAGIC:
        raise ValueError('not an OLE2 file')
    sec_shift, mini_shift = struct.unpack_from('<HH', data, 30)
    ssz, msz = 1 << sec_shift, 1 << mini_shift
    n_fat, dir_start = struct.unpack_from('<II', data, 44)
    cutoff, minifat_start, n_minifat, difat_start, n_difat = struct.unpack_from('<IIIII', data, 56)

    def sector(i):
        off = (i + 1) * ssz
        return data[off:off + ssz]

    difat = list(struct.unpack_from('<109I', data, 76))
    nxt = difat_start
    for _ in range(n_difat):
        if nxt >= ENDOFCHAIN:
            break
        vals = struct.unpack('<%dI' % (ssz // 4), sector(nxt))
        difat += vals[:-1]
        nxt = vals[-1]
    fat = []
    for s in difat[:n_fat]:
        fat += struct.unpack('<%dI' % (ssz // 4), sector(s))

    def chain(start, table, read, limit=1 << 20):
        out, i, n = [], start, 0
        while i < ENDOFCHAIN and i < len(table) and n < limit:
            out.append(read(i))
            i = table[i]
            n += 1
        return b''.join(out)

    dirs = chain(dir_start, fat, sector)
    entries = []
    for off in range(0, len(dirs), 128):
        e = dirs[off:off + 128]
        nlen = struct.unpack_from('<H', e, 64)[0]
        name = e[:max(0, nlen - 2)].decode('utf-16-le', 'replace')
        etype = e[66]
        start, size = struct.unpack_from('<II', e, 116)
        entries.append((name, etype, start, size))
    root = next(e for e in entries if e[1] == 5)
    for name, etype, start, size in entries:
        if etype == 2 and name in wanted:
            if size >= cutoff:
                return chain(start, fat, sector)[:size]
            minifat = []
            mf = chain(minifat_start, fat, sector)
            minifat = list(struct.unpack('<%dI' % (len(mf) // 4), mf))
            ministream = chain(root[2], fat, sector)
            return chain(start, minifat, lambda i: ministream[i * msz:(i + 1) * msz])[:size]
    raise ValueError('no Workbook stream')


def _records(wb):
    pos = 0
    while pos + 4 <= len(wb):
        rtype, rlen = struct.unpack_from('<HH', wb, pos)
        yield rtype, wb[pos + 4:pos + 4 + rlen]
        pos += 4 + rlen


def _rk(v):
    if v & 2:
        val = float(struct.unpack('<i', struct.pack('<I', v))[0] >> 2)
    else:
        val = struct.unpack('<d', struct.pack('<Q', (v & 0xFFFFFFFC) << 32))[0]
    if v & 1:
        val /= 100.0
    return val


class _Segments:
    """Byte reader over SST + CONTINUE segments that knows where segment borders are."""

    def __init__(self, segs):
        self.segs, self.si, self.pos = segs, 0, 0

    def _ensure(self):
        while self.si < len(self.segs) and self.pos >= len(self.segs[self.si]):
            self.si += 1
            self.pos = 0
        return self.si < len(self.segs)

    def take(self, n):
        out = b''
        while n > 0 and self._ensure():
            seg = self.segs[self.si]
            chunk = seg[self.pos:self.pos + n]
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return out

    def at_border(self):
        return self.si < len(self.segs) and self.pos >= len(self.segs[self.si])

    def string(self):
        cch, flags = struct.unpack('<HB', self.take(3))
        runs = struct.unpack('<H', self.take(2))[0] if flags & 8 else 0
        ext = struct.unpack('<I', self.take(4))[0] if flags & 4 else 0
        wide = flags & 1
        parts, left = [], cch
        while left > 0:
            if self.at_border():
                self._ensure()
                if self.si >= len(self.segs):
                    break
                wide = self.take(1)[0] & 1  # a continued string restarts with a flags byte
            seg = self.segs[self.si]
            avail = (len(seg) - self.pos) // (2 if wide else 1)
            n = min(left, max(avail, 1))
            raw = self.take(n * (2 if wide else 1))
            parts.append(raw.decode('utf-16-le' if wide else 'latin-1', 'replace'))
            left -= n
        self.take(runs * 4 + ext)
        return ''.join(parts)


def _label_string(d, off):
    cch, flags = struct.unpack_from('<HB', d, off)
    off += 3
    if flags & 8:
        off += 2
    if flags & 4:
        off += 4
    if flags & 1:
        return d[off:off + 2 * cch].decode('utf-16-le', 'replace')
    return d[off:off + cch].decode('latin-1', 'replace')


def read_xls(data):
    wb = _ole_stream(data)
    sst, cells = [], {}
    bof_depth, sheet_index, in_sheet = 0, -1, False
    recs = list(_records(wb))
    i = 0
    while i < len(recs):
        rtype, d = recs[i]
        if rtype == 0x0809:  # BOF
            sheet_index += 1
            in_sheet = sheet_index == 1
        elif rtype == 0x000A:  # EOF
            if in_sheet:
                break
        elif rtype == 0x00FC:  # SST
            segs = [d[8:]]
            j = i + 1
            while j < len(recs) and recs[j][0] == 0x003C:
                segs.append(recs[j][1])
                j += 1
            total = struct.unpack_from('<I', d, 4)[0]
            rd = _Segments(segs)
            for _ in range(total):
                if not rd._ensure():
                    break
                sst.append(rd.string())
            i = j
            continue
        elif in_sheet:
            if rtype == 0x00FD:  # LABELSST
                r, c, _, idx = struct.unpack_from('<HHHI', d)
                cells[(r, c)] = sst[idx] if idx < len(sst) else ''
            elif rtype == 0x0204:  # LABEL
                r, c, _ = struct.unpack_from('<HHH', d)
                cells[(r, c)] = _label_string(d, 6)
            elif rtype == 0x0203:  # NUMBER
                r, c, _, v = struct.unpack_from('<HHHd', d)
                cells[(r, c)] = v
            elif rtype == 0x027E:  # RK
                r, c, _, v = struct.unpack_from('<HHHI', d)
                cells[(r, c)] = _rk(v)
            elif rtype == 0x00BD:  # MULRK
                r, c = struct.unpack_from('<HH', d)
                n = (len(d) - 6) // 6
                for k in range(n):
                    _, v = struct.unpack_from('<HI', d, 4 + k * 6)
                    cells[(r, c + k)] = _rk(v)
        i += 1
    if not cells:
        return []
    nrows = max(r for r, _ in cells) + 1
    ncols = max(c for _, c in cells) + 1
    return [[cells.get((r, c), '') for c in range(ncols)] for r in range(nrows)]
