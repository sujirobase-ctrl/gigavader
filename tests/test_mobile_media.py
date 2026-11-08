"""Bounded HTTP transport used by the actual Android codecs."""
import json
import sys
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from src.http_client import HttpClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'android/app/src/main/python'))
from mobile_media import MediaSource


@pytest.fixture
def source_server():
    payload=bytes(range(256))*32768
    class Handler(BaseHTTPRequestHandler):
        requests=[]
        def log_message(self,*args): pass
        def do_GET(self):
            self.requests.append(self.headers.get('Range'))
            if self.headers.get('Referer')!='https://example.com/post':
                self.send_error(403);return
            start,end=map(int,self.headers['Range'][6:].split('-'))
            end=min(end,len(payload)-1)
            if self.path=='/wrong':start+=1
            self.send_response(206)
            self.send_header('Content-Range',f'bytes {start}-{end}/{len(payload)}')
            self.send_header('Content-Length',str(end-start+1))
            self.end_headers()
            try:self.wfile.write(payload[start:end+1])
            except (BrokenPipeError, ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    class Engine:
        path='/video'
        def _client(self):return HttpClient(max_retries=0)
        def video_source(self,*args):return json.dumps(dict(url=f'http://127.0.0.1:{server.server_port}'+self.path, headers={'Referer':'https://example.com/post'}))
    try:yield Engine(),payload,Handler.requests
    finally:server.shutdown();server.server_close()


def test_ranges_cover_budget_and_close(source_server):
    engine,payload,requests=source_server
    source=MediaSource(engine,1,0,True)
    assert source.get_size()==len(payload)
    assert source.read_at(13,100)==payload[13:113]
    assert source.read_at(len(payload)-1024,1024)==payload[-1024:]
    assert requests[-1].startswith(f'bytes={len(payload)-1024}-')
    source.limit=source.used
    with pytest.raises(OSError):source.read_at(1024*1024,100)
    source.close()
    with pytest.raises(OSError):source.read_at(0,1)


def test_invalid_range_is_not_accepted(source_server):
    engine,_,_=source_server
    engine.path='/wrong'
    with pytest.raises(OSError,match='Intervalo'):MediaSource(engine,1,0,True)


def test_video_deadline_and_read_limit(source_server):
    engine,_,_=source_server
    source=MediaSource(engine,1,0,False)
    assert source.limit==24*1024*1024
    source.deadline=0
    with pytest.raises(OSError):source.read_at(1024*1024,1024)
    source.close()
