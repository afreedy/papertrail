#!/usr/bin/env python3
"""Politician disclosure shortcuts plus persistent, full browser controls."""
import argparse
import ast
import csv
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from html import unescape
import io
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tokenize
import tempfile
import xml.etree.ElementTree as ET
import urllib.error
import urllib.parse
import urllib.request
import zipfile

URLS = {
    'quiver': 'https://www.quiverquant.com/congresstrading/',
    'house': 'https://disclosures-clerk.house.gov/FinancialDisclosure',
    'senate': 'https://efdsearch.senate.gov/search/',
    'oge': 'https://www.oge.gov/web/OGE.nsf/Officials%20Individual%20Disclosures%20Search%20Collection?OpenForm',
}
HOSTS = {
    'quiver': {'www.quiverquant.com', 'quiverquant.com'},
    'house': {'disclosures-clerk.house.gov'},
    'senate': {'efdsearch.senate.gov'},
    'oge': {'www.oge.gov', 'oge.gov', 'extapps2.oge.gov'},
}
OGE_API = 'https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v3/rest'
OGE_REQUEST = 'https://extapps2.oge.gov/201/Presiden.nsf/201%20Request?OpenForm'
MAX_BYTES = 20 * 1024 * 1024
PWCLI = ['npx', '--yes', '--package', '@playwright/cli@0.1.22', 'playwright-cli']
PROFILE_ROOT = Path.home() / '.local/share/politician-clis'


class SourceError(Exception):
    pass


def browser_args(source, command):
    """Delegate browser mechanics to the existing Playwright CLI, not a new driver."""
    if not command: command = ['snapshot']
    if command[0] == 'open':
        command = list(command)
        if len(command) == 1 or command[1].startswith('--'): command.insert(1, URLS[source])
        checked_url(source, command[1])
        if '--headed' not in command: command.append('--headed')
        if not any(c.startswith('--browser') for c in command): command += ['--browser', 'chrome']
        if not any(c.startswith('--profile') for c in command):
            profile = PROFILE_ROOT / source
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            command += ['--profile', str(profile)]
    if command[0] in ('goto', 'tab-new') and len(command) > 1:
        checked_url(source, command[1])
    return PWCLI + ['--session', 'politician-' + source] + command


def browser_command(source, command):
    return subprocess.run(browser_args(source, command), cwd=Path(__file__).parent).returncode


def browser_json(source, command):
    result = subprocess.run(browser_args(source, command) + ['--json'], cwd=Path(__file__).parent,
                            capture_output=True, text=True, timeout=90)
    try: response = json.loads(result.stdout)
    except ValueError as e: raise SourceError('Browser CLI failed: ' + (result.stderr or result.stdout)[-1500:]) from e
    if result.returncode or response.get('isError') or 'error' in response:
        raise SourceError('Browser action failed: ' + str(response)[-1500:])
    value = response.get('result', response)
    if isinstance(value, str):
        try: return json.loads(value)
        except ValueError: return value
    return value


def browser_visit(source, url):
    checked_url(source, url)
    try: browser_json(source, ['snapshot'])
    except SourceError: browser_json(source, ['open', url])
    else: browser_json(source, ['goto', url])


def browser_html(source):
    result = browser_json(source, ['eval', '() => document.documentElement.outerHTML'])
    if not isinstance(result, str): raise SourceError('Browser returned no HTML.')
    return result


def senate_page(args):
    url = args.url or URLS['senate']
    browser_visit('senate', url)
    # This acknowledges a notice of statutory restrictions; it does not create an account.
    gate = browser_json('senate', ['eval', '() => !!document.querySelector("#agree_statement")'])
    if gate:
        if not args.acknowledge_use_restrictions:
            raise SourceError('Read the Senate usage notice in the opened browser. '
                              'Acknowledge it via browser check, or rerun with --acknowledge-use-restrictions.')
        browser_json('senate', ['run-code', 'async page => { await page.locator("#agree_statement").check(); '
                      'await page.locator("#lastName").waitFor(); }'])
    if args.command == 'trades':
        if not args.url: raise SourceError('For Senate trades, provide --url REPORT_URL or --input SAVED_HTML.')
        # A first-time notice may redirect away from the requested document.
        if gate: browser_json('senate', ['goto', args.url])
        browser_json('senate', ['run-code', 'async page => { await page.getByRole("heading", {name:"Transactions", exact:true}).waitFor(); }'])
        return browser_html('senate'), url
    filters = json.dumps({'name': args.name, 'first_name': args.first_name, 'type': args.type,
                          'filer': args.filer, 'state': args.state, 'since': args.since, 'until': args.until})
    code = '''async page => {
      const f = FILTERS;
      await page.locator('#lastName').fill(f.name);
      await page.locator('#firstName').fill(f.first_name);
      if (f.type !== 'all') await page.locator('input[name="report_type"][value="' + ({ptr:11, annual:7, extension:10, trusts:14, other:15})[f.type] + '"]').check();
      if (f.filer !== 'all') await page.locator('input[name="filer_type"][value="' + ({senator:1, candidate:4, former:5})[f.filer] + '"]').check();
      if (f.state) await page.locator(f.filer === 'candidate' ? '#candidateFilerState' : '#senatorFilerState').selectOption(f.state);
      const usDate = s => s ? s.slice(5,7) + '/' + s.slice(8,10) + '/' + s.slice(0,4) : '';
      await page.locator('#fromDate').fill(usDate(f.since));
      await page.locator('#toDate').fill(usDate(f.until));
      await page.getByRole('button', {name:'Search Reports', exact:true}).click();
      await page.locator('#filedReports tbody tr').first().waitFor();
      await page.locator('#filedReports_processing').waitFor({state:'hidden'});
    }'''.replace('FILTERS', filters)
    browser_json('senate', ['run-code', code])
    # Use the site's own pagination and ordering, rather than a separate query protocol.
    options = json.dumps({'limit': args.limit, 'offset': args.offset})
    code = '''async page => {
      const opts = OPTIONS;
      await page.locator('select[name="filedReports_length"]').selectOption('100');
      await page.locator('#filedReports_processing').waitFor({state:'hidden'});
      await page.getByRole('columnheader', {name:/Date Received/}).click();
      await page.locator('#filedReports_processing').waitFor({state:'hidden'});
      if (await page.getByRole('columnheader', {name:/Date Received/}).getAttribute('aria-sort') !== 'descending') {
        await page.getByRole('columnheader', {name:/Date Received/}).click();
        await page.locator('#filedReports_processing').waitFor({state:'hidden'});
      }
      for (let i=0;i<Math.floor(opts.offset / 100);i++) {
        const next = page.locator('#filedReports_next');
        if ((await next.getAttribute('class')).includes('disabled')) throw new Error('Offset exceeds search results');
        await next.click();
        await page.locator('#filedReports_processing').waitFor({state:'hidden'});
      }
    }'''.replace('OPTIONS', options)
    browser_json('senate', ['run-code', code])
    return browser_html('senate'), url


def checked_url(source, url):
    u = urllib.parse.urlsplit(url)
    if (u.scheme != 'https' or u.hostname not in HOSTS[source]
            or u.username or u.password or u.port not in (None, 443)):
        raise SourceError('Use an HTTPS URL on this source\'s official host.')
    return url


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, source):
        self.source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(self.source, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(source, url, headers=None):
    checked_url(source, url)
    opener = urllib.request.build_opener(Redirects(source))
    req = urllib.request.Request(url, headers={'User-Agent': 'politician-disclosures-cli/0.1', **(headers or {})})
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise SourceError(f'{source}: HTTP {e.code}. Access failed, not an empty result. '
                          'Use the open command and a browser-saved file with --input; '
                          'no login, paywall or anti-bot bypass is attempted.') from e
    if len(body) > MAX_BYTES:
        raise SourceError('Response exceeds the 20 MiB safety limit.')
    return body


class HTML(HTMLParser):
    """Only the scripts, headings, links and table cells needed by these sources."""
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.scripts, self.headings, self.links, self.tables = [], [], [], []
        self.script = self.heading = self.cell = self.row = self.table = None
        self.link = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script': self.script = []
        if tag in ('h1', 'h2'): self.heading = []
        if tag == 'table': self.table = []; self.tables.append(self.table)
        if tag == 'tr' and self.table is not None: self.row = []
        if tag in ('td', 'th') and self.row is not None: self.cell = {'text': [], 'links': []}
        if tag == 'a':
            self.link = {'url': attrs.get('href', ''), 'text': []}
            self.links.append(self.link)
            if self.cell is not None: self.cell['links'].append(self.link)
        if tag == 'br': self.handle_data(' ')

    def handle_data(self, data):
        for target in (self.script, self.heading):
            if target is not None: target.append(data)
        if self.cell is not None: self.cell['text'].append(data)
        if self.link is not None: self.link['text'].append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            self.scripts.append(''.join(self.script)); self.script = None
        if tag in ('h1', 'h2') and self.heading is not None:
            self.headings.append(' '.join(''.join(self.heading).split())); self.heading = None
        if tag in ('td', 'th') and self.cell is not None:
            self.cell['text'] = ' '.join(''.join(self.cell['text']).split())
            self.row.append(self.cell); self.cell = None
        if tag == 'tr' and self.row is not None:
            self.table.append(self.row); self.row = None
        if tag == 'table': self.table = None
        if tag == 'a' and self.link is not None:
            self.link['text'] = ' '.join(''.join(self.link['text']).split()); self.link = None


def js_array(page, variable):
    for script in HTML(page).scripts:
        match = re.search(r'\b(?:let|var|const)\s+' + re.escape(variable) + r'\s*=\s*(\[)', script)
        if not match: continue
        start = match.start(1)
        depth, quote, escaped = 0, None, False
        for end in range(start, len(script)):
            char = script[end]
            if quote:
                if escaped: escaped = False
                elif char == '\\': escaped = True
                elif char == quote: quote = None
            elif char in ('"', "'"): quote = char
            elif char == '[': depth += 1
            elif char == ']':
                depth -= 1
                if depth == 0:
                    # Replace JS constants as tokens, never inside merchant/asset text.
                    constants = {'null': 'None', 'NaN': 'None', 'true': 'True', 'false': 'False'}
                    tokens = tokenize.generate_tokens(io.StringIO(script[start:end + 1]).readline)
                    literal = tokenize.untokenize((t.type, constants.get(t.string, t.string)
                                                  if t.type == tokenize.NAME else t.string) for t in tokens)
                    return ast.literal_eval(literal)
        raise SourceError(f'{variable}: unterminated array; source format changed.')
    raise SourceError(f'{variable} absent; page may be blocked or its format changed.')


def matches(query, text):
    return all(word in text.casefold() for word in query.casefold().split())


def quiver_people(page):
    registry = re.search(r'allCongressMembers\s*=\s*JSON.parse\(`(.*?)`\)', page, re.S)
    if not registry: raise SourceError('Public politician registry absent; source format changed.')
    return json.loads(registry[1])


def quiver_holdings(args):
    if not args.name: raise SourceError('Holdings require a unique --name.')
    people=[p for p in quiver_people(fetch('quiver',URLS['quiver']).decode()) if matches(args.name,p['name'])]
    if len(people)!=1: raise SourceError('Use a unique politician name.')
    person=people[0]
    url='https://www.quiverquant.com/get_politician_page_tab_data/'+urllib.parse.quote(person['bioguideID'],safe='')
    data=json.loads(Path(args.input).read_text() if args.input else fetch('quiver',url,{'X-Requested-With':'XMLHttpRequest'}))
    raw=json.loads(data['holdings_data']['politician_holdings'])
    records=[]
    for r in raw:
        if len(r)!=10: raise SourceError('Quiver holdings columns changed.')
        ticker,kind,asset,amount,owner,year,filed,document,chamber,_=r
        link=f'https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{str(filed)[:4]}/{document}' if chamber=='House' and re.fullmatch(r'\d+\.pdf',document or '') else None
        records.append(dict(name=person['name'],ticker=ticker,asset_type=kind,asset=asset,
            amount_range=amount,owner=owner,report_year=year,filed_date=str(filed)[:10],
            chamber=chamber,source_url=link,aggregator_url=url))
    records.sort(key=lambda r:(r['report_year'],r['filed_date']),reverse=True)
    if records: records=[r for r in records if r['report_year']==records[0]['report_year']]
    records=[r for r in records if not args.ticker or (r['ticker'] or '').casefold()==args.ticker.casefold()]
    return records,len(records),url,'Latest annual reported holdings parsed by Quiver; not current positions. Recent trades are not included.',False


def quiver_directory(args):
    rows=[p for p in quiver_people(fetch('quiver',URLS['quiver']).decode()) if matches(args.name,p['name'])]
    return rows,len(rows),URLS['quiver'],'Public Quiver politician directory; includes former members.',False


def quiver(args):
    url = URLS['quiver']
    def get_page(url):
        if args.browser:
            browser_visit('quiver', url)
            return browser_html('quiver')
        return fetch('quiver', url).decode()
    page = Path(args.input).read_text() if args.input else get_page(url)
    profile = any(re.search(r'\blet\s+tradeData\s*=', s) for s in HTML(page).scripts)
    if args.name and not args.input:
        people = [p for p in quiver_people(page) if matches(args.name, p['name'])]
        if len(people) != 1:
            raise SourceError('Use a unique politician name. Matches: ' +
                              ', '.join(p['name'] for p in people[:20]))
        person = people[0]
        slug = urllib.parse.quote(person['name'] + '-' + person['bioguideID'], safe='')
        url += 'politician/' + slug
        page = get_page(url)
        profile = True
    raw = js_array(page, 'tradeData' if profile else 'recentTradesData')
    if not isinstance(raw, list): raise SourceError('Invalid Quiver trade array.')
    records = []
    for r in raw:
        if not isinstance(r, list) or len(r) != (15 if profile else 16):
            raise SourceError('Quiver row schema changed; refusing to mislabel columns.')
        if profile:
            ticker, transaction, filed, traded, comment, _, name, rid, asset, asset_type, amount, chamber, party, _, _ = r
        else:
            ticker, asset, asset_type, transaction, amount, name, chamber, party, filed, traded, comment, rid, _, _, _, _ = r
        records.append(dict(name=name, ticker=ticker, asset=asset, asset_type=asset_type,
                            transaction=transaction, amount_range=amount, owner=None,
                            filed_date=str(filed)[:10], trade_date=str(traded)[:10],
                            chamber=chamber, party=party, comment=comment, id=rid, source_url=url))
    records = [r for r in records if matches(args.name, r['name']) and
               (not args.ticker or (r['ticker'] or '').casefold() == args.ticker.casefold())]
    records.sort(key=lambda r: (r['filed_date'], r['trade_date']), reverse=True)
    coverage = ('Public politician page history; completeness is not guaranteed.' if profile else
                'Public dashboard recent-trade window only; not full history. Use --name for a politician page.')
    return records, len(records), url, coverage, False


def house(args):
    url = f'https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{args.year}FD.zip'
    body = Path(args.input).read_bytes() if args.input else fetch('house', url)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        info = archive.getinfo(f'{args.year}FD.txt')
        if info.file_size > MAX_BYTES: raise SourceError('House index exceeds 20 MiB.')
        text = archive.read(info).decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(text), delimiter='\t')
    if not {'First', 'Last', 'FilingType', 'Year', 'FilingDate', 'DocID'} <= set(reader.fieldnames or []):
        raise SourceError('House index columns changed.')
    records = []
    for r in reader:
        name = ' '.join(filter(None, (r['First'], r['Last'], r.get('Suffix'))))
        if not matches(args.name, name) or (args.type != 'all' and r['FilingType'] != args.type): continue
        if not r['DocID'].isdigit() or not r['Year'].isdigit(): raise SourceError('Invalid House document ID/year.')
        folder = 'ptr-pdfs' if r['FilingType'] == 'P' else 'financial-pdfs'
        records.append(dict(name=name, filing_type=r['FilingType'], filed_date=iso_date(r['FilingDate']),
                            district=r.get('StateDst'), year=r['Year'], id=r['DocID'],
                            source_url=f"https://disclosures-clerk.house.gov/public_disc/{folder}/{r['Year']}/{r['DocID']}.pdf"))
    records.sort(key=lambda r: (r['filed_date'], r['id']), reverse=True)
    return records, len(records), url, 'Filing metadata for the selected year; PDFs contain the transactions.', False


def iso_date(value):
    return datetime.strptime(value, '%m/%d/%Y').date().isoformat()


def house_pdf(path, source_url):
    """Strict extraction of the current digital PTR table, retaining original annotations."""
    text = subprocess.check_output(['pdftotext', '-layout', str(path), '-'], text=True)
    name = re.search(r'^Name:\s*(?:Hon\.\s*)?(.+)$', text, re.M)
    filing = re.search(r'Filing ID #(\d+)', text)
    signed = re.search(r'Digitally Signed:.*?,\s*(\d{2}/\d{2}/\d{4})', text)
    if not name or not filing: raise SourceError('Unrecognised/scanned House PDF; review the original.')
    xml = subprocess.check_output(['pdftotext', '-bbox-layout', str(path), '-'])
    ns = {'x': 'http://www.w3.org/1999/xhtml'}
    records = []
    for number, page in enumerate(ET.fromstring(xml).findall('.//x:page', ns), 1):
        words = [(float(w.get('xMin')), float(w.get('yMin')), w.text or '')
                 for w in page.findall('.//x:word', ns)]
        headers = [w for w in words if w[2] == 'Owner']
        if not headers: continue
        header = headers[0]
        headings = {t: x for x, y, t in words if abs(y-header[1]) < 2}
        if not {'Owner', 'Asset', 'Transaction', 'Date', 'Notification', 'Amount', 'Cap.'} <= headings.keys():
            raise SourceError('House table layout changed; review the original.')
        bounds = [headings[k]-1 for k in ('Owner','Asset','Transaction','Date','Notification','Amount','Cap.')]
        starts = sorted({y for x,y,t in words if bounds[3] <= x < bounds[4]
                         and y > header[1]+20 and re.fullmatch(r'\d{2}/\d{2}/\d{4}',t)})
        for index, start in enumerate(starts):
            end = starts[index+1] if index+1 < len(starts) else float(page.get('height'))
            block = sorted((w for w in words if start-1 <= w[1] < end-1), key=lambda w:(round(w[1],1),w[0]))
            # An annotation starts below the cells. It must not become part of the asset.
            annotation = min((y for x,y,t in block if t == ':' and bounds[1] <= x < bounds[2]), default=end)
            cells = [' '.join(t for x,y,t in block if bounds[i] <= x < bounds[i+1] and y < annotation-1)
                     for i in range(6)]
            owner, asset, transaction, traded, notified, amount = cells
            if (not asset or not re.search(r'\[[A-Z]+\]',asset) or
                not re.fullmatch(r'[PSE](?:\s*\([^)]*\))?',transaction) or
                not re.fullmatch(r'\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+',amount)):
                raise SourceError(f'House row {len(records)+1} is uncertain; review the original PDF.')
            ticker = re.search(r'\(([A-Z][A-Z0-9.\-]{0,9})\)\s*\[',asset)
            asset_type = re.search(r'\[([A-Z]+)\]',asset)
            notes = ' '.join(t for x,y,t in block if y >= annotation-1 and y < end-1)
            # Strip report-level material after the table from the last row's notes.
            notes = re.split(r'\* For the complete|Digitally Signed:',notes)[0].strip()
            records.append(dict(name=name[1].strip(), id=str(len(records)+1), filing_id=filing[1],
                page=number, owner=owner or None, asset=asset, ticker=ticker[1] if ticker else None,
                asset_type=asset_type[1], transaction=transaction, amount_range=amount,
                trade_date=iso_date(traded), notification_date=iso_date(notified),
                filed_date=iso_date(signed[1]) if signed else None, comment=notes, source_url=source_url))
    if not records: raise SourceError('No supported digital House PTR rows; review the original PDF.')
    return records


def house_trades(args):
    url = args.url or None
    if not args.input and not url: raise SourceError('Provide --url HOUSE_PDF_URL or --input LOCAL_PDF.')
    if args.input: records = house_pdf(Path(args.input),url)
    else:
        body = fetch('house',url)
        if not body.startswith(b'%PDF-'): raise SourceError('Expected a House PDF.')
        with tempfile.NamedTemporaryFile(suffix='.pdf') as f:
            f.write(body); f.flush()
            records = house_pdf(Path(f.name),url)
    records = [r for r in records if matches(args.name,r['name']) and
               (not args.ticker or (r['ticker'] or '').casefold() == args.ticker.casefold())]
    return records,len(records),url,'Strict digital House PTR extraction; raw filing is authoritative. Unrecognised layouts require review.',False


def oge(args):
    params = {'draw': 1, 'start': args.offset, 'length': args.limit, 'search[value]': 'Trump' if args.name.casefold() in ('donald j. trump','donald trump') else args.name}
    url = OGE_API + '?' + urllib.parse.urlencode(params)
    data = json.loads(Path(args.input).read_text() if args.input else fetch('oge', url))
    if not isinstance(data.get('data'), list) or not isinstance(data.get('recordsFiltered'), int):
        raise SourceError('OGE response schema changed.')
    records = []
    for r in data['data']:
        markup = HTML(r['type'])
        link = markup.links[0] if markup.links else None
        link_url = urllib.parse.urljoin(OGE_API, link['url']) if link else None
        if link_url: checked_url('oge', link_url)
        access = 'request_required' if link and any(s in link['text'].casefold() for s in ('request', 'contact agency')) else ('download' if link else 'unavailable')
        # Plain text includes annotations outside the link, including pending review.
        label = unescape(re.sub('<[^>]*>', '', r['type']))
        records.append(dict(name=r['name'], title=r['title'], agency=r['agency'],
                            date_added=r['docDate'], document_type=label, access=access,
                            source_url=link_url, amended=r.get('amended')))
    return records, data['recordsFiltered'], url, 'OGE index, in server order. --name searches all index fields. Date added is not trade date.', True


def reviewed_oge(body, url):
    review=json.loads((Path(__file__).parent/'data/oge-reviewed.json').read_text())
    if url!=review['source_url'] or hashlib.sha256(body).hexdigest()!=review['sha256']: return None
    return [dict(r,name=review['name'],source_url=url,ticker=None,owner=None,
                 filed_date=review['filed_date'],date_added=review['date_added']) for r in review['records']]


def oge_report(args):
    try: import pymupdf
    except ImportError as e: raise SourceError('OGE PDF extraction needs PyMuPDF (see requirements.txt).') from e
    url=args.url; filing=None
    if not args.input and not url:
        if not args.name: raise SourceError('Provide --name, --url or --input for OGE reports.')
        opts=argparse.Namespace(name=args.name,offset=0,limit=100,input=None)
        entries=oge(opts)[0]
        needle='278 Transaction' if args.command=='trades' else 'Annual'
        eligible=[r for r in entries if r['access']=='download' and needle in r['document_type']]
        if not eligible: raise SourceError('No direct public report found. Use oge list for request links.')
        names={r['name'] for r in eligible}
        if len(names)!=1: raise SourceError('Use a unique executive official name.')
        filing=max(eligible,key=lambda r:r['date_added']);url=filing['source_url']
    body=Path(args.input).read_bytes() if args.input else fetch('oge',url)
    if not body.startswith(b'%PDF-'): raise SourceError('Expected OGE PDF.')
    checked=reviewed_oge(body,url) if args.command=='trades' else None
    if checked is not None:
        return checked,len(checked),url,'This exact OGE PDF was visually checked by AI against its transaction pages. Extraction review is not an independent audit. Amounts are ranges; no execution prices or tickers inferred. OGE alerts remain new-filing notifications.',False
    records=[]
    with pymupdf.open(stream=body,filetype='pdf') as doc:
        report_year=None
        first=doc[0].get_text()
        year=re.search(r'Year\s*\(Annual Report only\):\s*(\d{4})',first)
        if year: report_year=int(year[1])
        for page_number,page in enumerate(doc,1):
            text=page.get_text();words=page.get_text('words')
            if args.command=='holdings' and not re.search(r'Part\s+[256]:',text): continue
            amount_header=next((w for w in words if w[4]=='Value'),None) if args.command=='holdings' else next((w for w in words if w[4]=='Amount'),None)
            hash_header=next((w for w in words if w[4]=='#'),None)
            if args.command=='trades' and amount_header and not hash_header:
                raise SourceError(f'Unsupported transaction table on page {page_number}; review original PDF.')
            if not amount_header or not hash_header: continue
            yhead=hash_header[1];xid=hash_header[0]
            starts=sorted((w for w in words if abs(w[0]-xid)<8 and w[1]>yhead+8 and re.fullmatch(r'\d+(?:\.\d+)*\.?',w[4])),key=lambda w:w[1])
            if args.command=='holdings':
                description=next((w for w in words if w[4]=='Description'),None)
                eif=next((w for w in words if w[4]=='EIF'),None)
                income=next((w for w in words if w[4]=='Income' and abs(w[1]-amount_header[1])<3),None)
                if not description or not eif or not income: continue
            else:
                date_header=next((w for w in words if w[4]=='Date' and abs(w[1]-yhead)<10),None)
                if not date_header: raise SourceError(f'Unsupported transaction date column on page {page_number}; review original PDF.')
            for i,start in enumerate(starts):
                end=starts[i+1][1] if i+1<len(starts) else page.rect.height-12
                block=sorted((w for w in words if start[1]-3<=w[1]<end-3),key=lambda w:(round(w[1]/3),w[0]))
                def column(left,right): return ' '.join(w[4] for w in block if left<=w[0]<right).strip()
                base=dict(name=filing['name'] if filing else args.name,id=f'{page_number}:{start[4]}',page=page_number,
                    filed_date=filing['date_added'][:10] if filing else None,source_url=url,ticker=None)
                if args.command=='holdings':
                    asset=column(description[0]-3,eif[0]-2);amount=column(amount_header[0]-6,income[0]-2)
                    if not asset or not amount: continue
                    verified=bool(re.fullmatch(r'\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+|None \(or less than \$1,001\)',amount))
                    base.update(asset=asset,amount_range=amount,owner='Spouse' if 'Part 5:' in text else 'Filer',report_year=report_year,needs_review=not verified)
                else:
                    # ponytail: OGE scanned tables retain OCR text and require human verification;
                    # never use uncertain rows to classify purchase alerts or infer stock tickers.
                    desc_left=xid+10;type_left=date_header[0]-110;date_left=date_header[0]-28
                    asset=column(desc_left,type_left);transaction=column(type_left,date_left)
                    traded=column(date_left,date_header[0]+25)
                    money_left=min((w[0] for w in block if '$' in w[4] and w[0]>date_header[0]+25),default=amount_header[0]-58)
                    amount=column(money_left-1,page.rect.width)
                    if not asset or not transaction: continue
                    valid=re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}',traded or '')
                    try: trade_date=iso_date(traded) if valid else None
                    except ValueError: trade_date=None
                    base.update(asset=asset,transaction=transaction,trade_date=trade_date,raw_trade_date=traded,
                        amount_range=amount,owner=None,needs_review=True)
                records.append(base)
    if not records: raise SourceError('Report layout not supported; view original OGE PDF.')
    coverage=('OGE annual report Parts 2, 5 and 6 only; attachment schedules excluded. Reported assets are not current holdings.' if args.command=='holdings' else 'Latest directly available OGE transaction filing. OCR rows require checking against the PDF; dates/amounts are not inferred.')
    return records,len(records),url,coverage,False


def senate(args):
    text, source_url = (Path(args.input).read_text(), args.url or URLS['senate']) if args.input else senate_page(args)
    page = HTML(text)
    records = []
    kind = 'Transaction Date' if args.command == 'trades' else 'First Name (Middle)'
    tables = [t for t in page.tables if t and any(kind in c['text'] for c in t[0])]
    if len(tables) != 1:
        raise SourceError('Expected one rendered Senate ' + ('transaction' if args.command == 'trades' else 'search-results') +
                          ' table. Scanned PDFs and unrendered pages are not supported.')
    title = next((h for h in page.headings if h.startswith('Periodic Transaction Report')), '')
    name = next((h for h in page.headings if h.startswith('The Honorable')), '')
    name = re.sub(r'\s+\([^)]*\)$', '', name.removeprefix('The Honorable '))
    filed = re.search(r'Filed\s+(\d{2}/\d{2}/\d{4})', unescape(re.sub('<[^>]*>', ' ', text)))
    if not filed: filed = re.search(r'(\d{2}/\d{2}/\d{4})', title)
    for row in tables[0][1:]:
        cells = [c['text'] for c in row]
        if len(cells) == 1 and 'no matching' in cells[0].lower(): continue
        if args.command == 'trades':
            if len(cells) != 9: raise SourceError('Senate transaction columns changed.')
            records.append(dict(name=name, id=cells[0], trade_date=iso_date(cells[1]), owner=cells[2],
                                ticker=cells[3].replace('', '').strip(), asset=cells[4], asset_type=cells[5],
                                transaction=cells[6], amount_range=cells[7], comment=cells[8],
                                filed_date=iso_date(filed[1]) if filed else None, source_url=source_url))
        else:
            if len(cells) != 5 or not row[3]['links']: raise SourceError('Senate filing columns changed.')
            url = urllib.parse.urljoin(URLS['senate'], row[3]['links'][0]['url'])
            checked_url('senate', url)
            records.append(dict(name=cells[0] + ' ' + cells[1], office=cells[2], document_type=cells[3],
                                filed_date=iso_date(cells[4]), source_url=url))
    records = [r for r in records if matches(args.name, r['name']) and
               (not getattr(args, 'ticker', '') or r.get('ticker', '').casefold() == args.ticker.casefold())]
    if not args.input and args.command == 'list':
        # The live browser navigates in 100-row pages; preserve an arbitrary requested offset.
        within = args.offset % 100
        if within + args.limit > 100:
            more = browser_json('senate', ['run-code', '''async page => {
              const next = page.locator('#filedReports_next');
              if ((await next.getAttribute('class')).includes('disabled')) return null;
              await next.click();
              await page.locator('#filedReports_processing').waitFor({state:'hidden'});
              return await page.locator('#filedReports').evaluate(t => t.outerHTML);
            }'''])
            if more:
                extra = HTML(more).tables[0][1:]
                for row in extra:
                    cells = [c['text'] for c in row]
                    if len(cells) != 5 or not row[3]['links']: raise SourceError('Senate filing columns changed.')
                    url = urllib.parse.urljoin(URLS['senate'], row[3]['links'][0]['url'])
                    checked_url('senate', url)
                    records.append(dict(name=cells[0] + ' ' + cells[1], office=cells[2], document_type=cells[3],
                                        filed_date=iso_date(cells[4]), source_url=url))
        records = records[within:within + args.limit]
        info = browser_json('senate', ['eval', '() => document.querySelector("#filedReports_info")?.textContent'])
        count = re.search(r'of\s+([\d,]+)\s+entries', info or '')
        if not count: raise SourceError('Senate result count missing; source format changed.')
        return records, int(count[1].replace(',', '')), source_url, 'Live Senate browser search, ordered by filing date descending.', True
    coverage = ('Saved HTML contents; search exports include only the rendered page.' if args.input else
                'Complete contents of the selected live electronic Senate transaction report.')
    return records, len(records), source_url, coverage, False


def emit(source, args, result):
    records, total, url, coverage, paginated = result
    selected = records[:args.limit] if paginated else records[args.offset:args.offset + args.limit]
    meta = dict(source=source, status='ok', read_at=datetime.now(timezone.utc).isoformat(),
                fetched_at=None if args.input else datetime.now(timezone.utc).isoformat(),
                input=args.input, source_url=url, coverage=coverage, matched_count=total,
                offset=args.offset, returned_count=len(selected), truncated=args.offset + len(selected) < total)
    if args.format == 'json':
        print(json.dumps({**meta, 'records': selected}, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        print(json.dumps(meta), file=sys.stderr)
        if selected:
            writer = csv.DictWriter(sys.stdout, fieldnames=list(selected[0]))
            writer.writeheader(); writer.writerows(selected)


def download(source, args):
    checked_url(source, args.url)
    if source == 'oge' and re.search(r'/201(?:%20|\+| )Request', args.url, re.I):
        raise SourceError('This is an OGE request form, not a downloadable report. Use oge open --url URL.')
    if source == 'senate':
        args.command = 'trades'
        text, _ = senate_page(args)
        body = text.encode()
    else: body = fetch(source, args.url)
    if source in ('house', 'oge') and not body.startswith(b'%PDF-'):
        raise SourceError('Response is not a PDF. It may require a request; nothing was saved.')
    if source == 'senate' and b'<table' not in body.lower() and not body.startswith(b'%PDF-'):
        raise SourceError('Response is not a Senate report; nothing was saved.')
    # Exclusive creation prevents an accidental overwrite of existing research.
    with Path(args.output).open('xb') as f: f.write(body)
    print(json.dumps({'source': source, 'url': args.url, 'output': str(Path(args.output).resolve()), 'bytes': len(body)}))


def positive(value):
    number = int(value)
    if not 1 <= number <= 100: raise argparse.ArgumentTypeError('Choose 1–100.')
    return number


def nonnegative(value):
    number = int(value)
    if number < 0: raise argparse.ArgumentTypeError('Must be zero or greater.')
    return number


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    alias = Path(sys.argv[0]).name
    if alias in URLS: argv.insert(0, alias)
    if len(argv) >= 2 and argv[0] in URLS and argv[1] == 'browser':
        try: return browser_command(argv[0], argv[2:])
        except (SourceError, OSError, ValueError) as e:
            print(json.dumps({'source': argv[0], 'status': 'error', 'error': str(e)}), file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_subparsers(dest='source', required=True)
    for source in URLS:
        p = sources.add_parser(source)
        commands = p.add_subparsers(dest='command', required=True)
        commands.add_parser('browser', help='Full persistent browser CLI: open, snapshot, click, fill, select, tabs, downloads, etc.')
        commands.add_parser('save-page', help='Save current rendered browser page as HTML (--output FILE)')
        if source == 'quiver': commands.add_parser('login', help='Open the normal sign-in page in the dedicated browser profile')
        for command in (('list', 'trades','holdings','people') if source=='quiver' else ('list','trades','holdings') if source=='oge' else ('list','trades')):
            q = commands.add_parser(command, aliases=['search'] if command == 'list' else [], help='Read trades' if source == 'quiver' or command == 'trades' else 'Read filing metadata')
            q.add_argument('--name', default='')
            q.add_argument('--limit', type=positive, default=25)
            q.add_argument('--offset', type=nonnegative, default=0)
            q.add_argument('--format', choices=('json', 'csv'), default='json')
            q.add_argument('--input', help='Read a saved source file offline (House ZIP; OGE JSON; Quiver/Senate HTML)')
            if source in ('quiver', 'senate') or (source == 'house' and command == 'trades'): q.add_argument('--ticker', default='')
            if source == 'house' and command == 'trades': q.add_argument('--url')
            if source=='oge' and command!='list': q.add_argument('--url')
            if source == 'quiver': q.add_argument('--browser', action='store_true', help='Use the persistent browser session, including normal signed-in access')
            if source == 'senate':
                q.add_argument('--url', help='Official Senate report URL for trades')
                q.add_argument('--acknowledge-use-restrictions', action='store_true')
                q.add_argument('--first-name', default='')
                q.add_argument('--type', choices=('ptr', 'annual', 'extension', 'trusts', 'other', 'all'), default='ptr')
                q.add_argument('--filer', choices=('senator', 'candidate', 'former', 'all'), default='all')
                q.add_argument('--state', default='')
                q.add_argument('--since', type=date.fromisoformat)
                q.add_argument('--until', type=date.fromisoformat)
            if source == 'house':
                q.add_argument('--year', type=int, choices=range(2008, date.today().year + 1), default=date.today().year)
                q.add_argument('--type', default='P', help='House filing code; P = periodic transactions, all = every type')
        q = commands.add_parser('open', help='Open the official public page or a source URL')
        q.add_argument('--url')
        if source == 'oge': q.add_argument('--name', help='Open a prefilled request form; never submits it')
        if source != 'quiver':
            q = commands.add_parser('download', help='Download a directly accessible document; never overwrite')
            q.add_argument('--url', required=True); q.add_argument('--output', required=True)
            if source == 'senate': q.add_argument('--acknowledge-use-restrictions', action='store_true')
        q = commands.add_parser('read', help='Extract text from a local PDF using installed pdftotext')
        q.add_argument('file')
    # save-page has no arbitrary JavaScript; it saves exactly the currently rendered page.
    if len(argv) >= 2 and argv[0] in URLS and argv[1] == 'save-page':
        save_parser = argparse.ArgumentParser(prog=f'{argv[0]} save-page')
        save_parser.add_argument('--output', required=True)
        save_args = save_parser.parse_args(argv[2:])
        try:
            text = browser_html(argv[0])
            with Path(save_args.output).open('x') as f: f.write(text)
            print(json.dumps({'source': argv[0], 'output': str(Path(save_args.output).resolve())}))
            return 0
        except (SourceError, OSError, subprocess.TimeoutExpired) as e:
            print(json.dumps({'status': 'error', 'error': str(e)}), file=sys.stderr)
            return 1
    args = parser.parse_args(argv)
    if args.command == 'search': args.command = 'list'
    if args.source == 'senate' and args.command in ('list', 'trades'):
        if args.since and args.until and args.since > args.until: parser.error('--since must be before --until')
        if args.state and args.filer not in ('senator', 'candidate'): parser.error('--state requires --filer senator or candidate')
        args.since = args.since.isoformat() if args.since else ''
        args.until = args.until.isoformat() if args.until else ''
    try:
        if args.command == 'login':
            return browser_command('quiver', ['open', 'https://www.quiverquant.com/login/'])
        if args.command == 'open':
            url = args.url or (OGE_REQUEST + '&Filer=' + urllib.parse.quote(args.name) if getattr(args, 'name', None) else URLS[args.source])
            checked_url(args.source, url)
            return browser_command(args.source, ['open', url])
        elif args.command == 'download': download(args.source, args)
        elif args.command == 'read':
            path = Path(args.file).resolve()
            with path.open('rb') as f:
                if f.read(5) != b'%PDF-': raise SourceError('Expected a local PDF.')
            subprocess.run(['pdftotext', '-layout', str(path), '-'], check=True)
        else: emit(args.source, args, oge_report(args) if args.source=='oge' and args.command in ('trades','holdings') else quiver_directory(args) if args.source=='quiver' and args.command=='people' else quiver_holdings(args) if args.source=='quiver' and args.command=='holdings' else house_trades(args) if args.source == 'house' and args.command == 'trades' else globals()[args.source](args))
    except (SourceError, OSError, ValueError, KeyError, TypeError, SyntaxError, zipfile.BadZipFile,
            tokenize.TokenError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(json.dumps({'source': args.source, 'status': 'error', 'error': str(e)}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
