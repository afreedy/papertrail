from http.server import BaseHTTPRequestHandler
from pathlib import Path
import json
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

ROOT=Path(__file__).resolve().parents[1]


def query(params):
    action=params.get('kind',['trades'])[0]
    if action not in ('trades','holdings','filings','people'): raise ValueError('Unsupported search.')
    name=params.get('name',[''])[0].strip()
    if len(name)>100 or any(ord(c)<32 for c in name): raise ValueError('Invalid name.')
    source=params.get('source',['quiver'])[0]
    if source not in ('quiver','house','senate','oge'): raise ValueError('Invalid source.')
    offset=int(params.get('offset',['0'])[0]);limit=int(params.get('limit',['25'])[0])
    if not 0<=offset<=10000 or not 1<=limit<=100: raise ValueError('Invalid page.')
    if action=='people': source='quiver'
    if source=='senate' and action=='filings':
        return {'status':'unavailable','records':[], 'coverage':'The Senate requires an interactive browser acknowledgement. Open its official search or use the local senate CLI.', 'source_url':'https://efdsearch.senate.gov/search/'}
    if action=='filings':
        if source=='quiver': source='house'
        command='list'
    else: command=action
    if source=='house' and action=='holdings': source='quiver'
    if source=='senate': source='quiver'
    # The actual CLI entrypoint is the single data implementation for local and hosted searches.
    args=[sys.executable,str(ROOT/'cli.py'),source,command,'--name',name,'--limit',str(limit),'--offset',str(offset)]
    if source=='house':
        if action=='trades': raise ValueError('Use a House report URL in the local CLI. Dashboard trades use Quiver parsing with source links.')
        year=int(params.get('year',['2026'])[0])
        args+=['--year',str(year)]
    result=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,timeout=110)
    if result.returncode:
        try: error=json.loads(result.stderr)['error']
        except (ValueError,KeyError): error='Source lookup failed. Try again or open the source.'
        raise ValueError(error)
    data=json.loads(result.stdout)
    if action=='people' and name and ('trump' in name.lower() or 'donald'.startswith(name.lower())):
        data['records'].insert(0,dict(name='Donald J. Trump',chamber='President',party='R',bioguideID='executive-trump',image=None))
    return data


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            data=query(parse_qs(urlsplit(self.path).query));status=200
        except (ValueError,OSError,subprocess.TimeoutExpired) as e:
            data=dict(status='error',error=str(e),records=[]);status=502
        body=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers();self.wfile.write(body)
