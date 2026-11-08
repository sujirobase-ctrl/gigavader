"""Bounded, authenticated random access for Android's native video codecs."""
import json
import re
import threading
import time


class MediaSource:
    def __init__(self, engine, queue, index, cover=False):
        self.client = engine._client()
        try:
            source = json.loads(engine.video_source(queue, index))
            self.url, self.headers = source['url'], source['headers']
        except Exception:
            self.client.close()
            raise
        self.limit = (4 if cover else 24) * 1024 * 1024
        self.deadline = time.monotonic() + (8 if cover else 20)
        self.used = 0
        self.size = -1
        self.closed = threading.Event()
        self.cache = {}
        self.cached = 0
        try:
            self.read_at(0, 1)
        except Exception:
            self.close()
            raise

    def get_size(self):
        return self.size

    def read_at(self, position, length):
        if self.closed.is_set(): raise OSError('Prévia encerrada')
        if self.size >= 0 and position >= self.size: return b''
        length = min(int(length), 256 * 1024)
        for start, data in self.cache.items():
            if start <= position < start + len(data):
                return data[position-start:position-start+length]
        if time.monotonic() > self.deadline or self.used >= self.limit:
            raise OSError('Limite da prévia atingido; download continua disponível')
        length = min(max(length, 65536), self.limit-self.used)
        headers = {**self.headers, 'Range': f'bytes={position}-{position+length-1}', 'Accept-Encoding': 'identity'}
        with self.client.get(self.url, headers=headers, stream=True, timeout=(4, 4)) as response:
            if response.status_code == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                if not match or int(match[1]) != position: raise OSError('Intervalo inválido')
                self.size = int(match[3])
            elif response.status_code == 200 and position == 0:
                self.size = int(response.headers.get('Content-Length', '-1'))
                if not 0 < self.size <= self.limit: raise OSError('Host não permite prévia limitada')
                length = self.size
            else: raise OSError('Prévia indisponível')
            if 'text/html' in response.headers.get('Content-Type', ''): raise OSError('Host retornou uma página')
            data = bytearray()
            for chunk in response.iter_content(32768):
                self.used += len(chunk)
                if self.closed.is_set() or self.used > self.limit or time.monotonic() > self.deadline:
                    raise OSError('Limite da prévia atingido')
                data.extend(chunk)
                if len(data) >= length: break
        result = bytes(data[:length])
        if self.cached + len(result) > 2 * 1024 * 1024:
            self.cache.clear()
            self.cached = 0
        self.cache[position] = result
        self.cached += len(result)
        return result

    def close(self):
        self.closed.set()
        self.cache.clear()
        self.client.close()
