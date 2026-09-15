"""Minimal reader for Java NBT files, plain or gzip compressed."""
import gzip
import struct

_SCALARS = {1: 'b', 2: 'h', 3: 'i', 4: 'q', 5: 'f', 6: 'd'}


class _Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def unpack(self, fmt):
        value = struct.unpack_from('>' + fmt, self.data, self.pos)[0]
        self.pos += struct.calcsize('>' + fmt)
        return value

    def string(self):
        length = self.unpack('H')
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        return raw.decode('utf-8', 'replace')

    def payload(self, tag):
        if tag in _SCALARS:
            return self.unpack(_SCALARS[tag])
        if tag == 7:
            length = self.unpack('i')
            raw = self.data[self.pos:self.pos + length]
            self.pos += length
            return list(raw)
        if tag == 8:
            return self.string()
        if tag == 9:
            element = self.unpack('b')
            return [self.payload(element) for _ in range(self.unpack('i'))]
        if tag == 10:
            compound = {}
            while True:
                child = self.unpack('b')
                if child == 0:
                    return compound
                name = self.string()  # name comes before the payload, read it first
                compound[name] = self.payload(child)
        if tag == 11:
            return [self.unpack('i') for _ in range(self.unpack('i'))]
        if tag == 12:
            return [self.unpack('q') for _ in range(self.unpack('i'))]
        raise ValueError(f'unknown NBT tag {tag} at offset {self.pos}')


def load(path):
    with open(path, 'rb') as handle:
        data = handle.read()
    if data[:2] == b'\x1f\x8b':  # gzip, as used by the world's data/*.dat files
        data = gzip.decompress(data)
    reader = _Reader(data)
    root_tag = reader.unpack('b')
    reader.string()
    return reader.payload(root_tag)
