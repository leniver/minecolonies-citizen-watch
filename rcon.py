"""Tiny Source RCON client, as used by Minecraft servers."""
import socket
import struct

LOGIN, COMMAND, RESPONSE = 3, 2, 0


class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host, port, password, timeout=3.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock = None
        self.next_id = 1

    def _send(self, kind, body):
        request_id = self.next_id
        self.next_id += 1
        payload = struct.pack('<ii', request_id, kind) + body.encode('utf-8') + b'\x00\x00'
        self.sock.sendall(struct.pack('<i', len(payload)) + payload)
        return request_id

    def _read_exact(self, size):
        data = b''
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RconError('connection closed by server')
            data += chunk
        return data

    def _read(self):
        size = struct.unpack('<i', self._read_exact(4))[0]
        packet = self._read_exact(size)
        request_id, kind = struct.unpack('<ii', packet[:8])
        return request_id, kind, packet[8:-2].decode('utf-8', 'replace')

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        request_id = self._send(LOGIN, self.password)
        reply_id, _, _ = self._read()
        if reply_id == -1 or reply_id != request_id:
            self.close()
            raise RconError('RCON password rejected')

    def command(self, text):
        if self.sock is None:
            self.connect()
        try:
            request_id = self._send(COMMAND, text)
            # Replies over 4096 bytes arrive in several packets; a marker request tells us when it is complete.
            marker_id = self._send(COMMAND, '')
            parts = []
            while True:
                reply_id, _, body = self._read()
                if reply_id == request_id:
                    parts.append(body)
                elif reply_id == marker_id:
                    return ''.join(parts)
        except (OSError, RconError):
            self.close()
            raise

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None
