"""Small source-parser and request-boundary check; no network or credentials."""
from cli import js_array, SourceError
from api.search import query

assert js_array('<script>let tradeData=[["A ] quoted asset",true,null,2]];</script>', 'tradeData') == [['A ] quoted asset', True, None, 2]]
try:
    js_array('<script>let tradeData=[__import__("os").getcwd()];</script>', 'tradeData')
except (SourceError, ValueError, SyntaxError):
    pass
else:
    raise AssertionError('Source JavaScript must never execute')
for params in [{'kind':['execute']},{'source':['untrusted']},{'limit':['101']},{'offset':['-1']},{'name':['bad\nname']}]:
    try:
        query(params)
    except ValueError:
        pass
    else:
        raise AssertionError('Invalid request accepted')
print('PASS: source array parsing, untrusted-code rejection and search input boundaries')

# Reviewed rows must never transfer to another URL or changed PDF bytes.
import json
from pathlib import Path
from unittest.mock import patch
from cli import reviewed_oge
review=json.loads(Path('data/oge-reviewed.json').read_text())
assert len(review['records'])==1156
assert [r['row_number'] for r in review['records']]==list(range(1,1157))
assert sum(r['transaction']=='Purchase' for r in review['records'])==440
assert reviewed_oge(b'%PDF-changed',review['source_url']) is None
with patch('cli.hashlib.sha256') as digest:
    digest.return_value.hexdigest.return_value=review['sha256']
    assert reviewed_oge(b'%PDF-test','https://www.oge.gov/other.pdf') is None
    rows=reviewed_oge(b'%PDF-test',review['source_url'])
    assert len(rows)==1156 and rows[0]['filed_date']=='2026-09-14'
    assert all(not r['needs_review'] and r['ticker'] is None for r in rows)
print('PASS: reviewed PDF identity guard, complete row numbering and purchase counts')
