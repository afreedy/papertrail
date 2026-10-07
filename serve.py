from http.server import ThreadingHTTPServer
from api.search import handler
ThreadingHTTPServer(('127.0.0.1',8787),handler).serve_forever()
