#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取论文材料或准备精读材料。已有原始文件复用；准备模式会重建文本、大纲、元数据与自动图表。

  python3 fetch_paper.py 2410.24164                          # arXiv ID
  python3 fetch_paper.py https://arxiv.org/abs/2410.24164     # 任何带 arXiv ID 的链接（abs/pdf/html/alphaxiv/hf papers）
  python3 fetch_paper.py https://example.com/paper.pdf        # 非 arXiv 的 PDF 直链
  python3 fetch_paper.py ~/Downloads/2410.24164v3.pdf         # 本地 PDF（文件名里有 arXiv ID 会顺带拉源码）
  python3 fetch_paper.py related_work/pi0-flow-vla            # 复用原始材料，重建派生材料
  python3 fetch_paper.py PMID:23193287 --dest related_work/genbank --download-only
  python3 fetch_paper.py --pubmed-query 'protein design' --limit 20

放哪（取第一个成立的）：
  --dest DIR         就放这
  --root DIR         放到 DIR/<slug>/
  ./related_work/    当前目录有它（research-workflow 项目）→ related_work/<slug>/
  $PAPERS_DIR        → $PAPERS_DIR/<slug>/
  ./papers/<slug>/   兜底
代码放哪：--code-dir；否则当前目录有 ./复现/ → 复现/<repo>/code；否则 <论文目录>/code。--no-clone 不克隆。

产出：
  paper.pdf     原文
  source/       arXiv 的 LaTeX 源码（有的话）
  paper.tex     源码拍平成一个文件：\\input 展开、注释和参考文献去掉，开头附正文用到的自定义宏
  paper.txt     PDF 逐页文本（带页码）
  outline.md    章节、公式在 paper.tex 里的行号（没有源码就用 PDF 书签 + 页码）
  meta.json     标题、作者、日期、摘要、版本、代码 / 项目页链接
  figures/      每张图（和表）一个 PNG + index.md + _contact.png（见 extract_figures.py）
  code/         官方代码浅克隆（找得到官方仓库时）
"""
import argparse
import datetime
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import unicodedata
import urllib.request
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True  # import extract_figures 时别在 skill 目录里留 __pycache__

UA = "kelip-paper-reading/0.1 (academic paper reader)"
NEW_ID = r'\d{4}\.\d{4,5}(?:v\d+)?'
OLD_ID = r'[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?'
ARXIV_RE = re.compile(rf'(?<![\d.])({NEW_ID}|{OLD_ID})(?!\d)')
GREEK = {"π": "pi", "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "λ": "lambda",
         "μ": "mu", "σ": "sigma", "τ": "tau", "φ": "phi", "ω": "omega", "θ": "theta", "ρ": "rho"}
STOP = {"a", "an", "the", "of", "for", "and", "to", "in", "on", "with", "via", "from", "by", "is", "are", "towards",
        "toward", "at", "as", "its", "using", "we", "our"}


def log(msg):
    print(msg, flush=True)


# ---------------- 网络 ----------------

def http_get(urls, timeout=120, tries=2):
    """依次试这几个地址，返回 (内容, 最终地址)。全失败就抛最后一个错。"""
    if isinstance(urls, str):
        urls = [urls]
    last = None
    for url in urls:
        for i in range(tries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read(), r.geturl()
            except Exception as e:  # noqa: BLE001 —— 网络错误五花八门，统一重试
                last = e
                time.sleep(1.5 * (i + 1))
    raise last


# ---------------- 输入解析 ----------------

def parse_target(target):
    p = Path(os.path.expanduser(target))
    if p.is_dir():
        return {"kind": "dir", "path": p.resolve()}
    if p.is_file() and p.suffix.lower() == ".pdf":
        m = ARXIV_RE.search(p.name)
        return {"kind": "local-pdf", "path": p.resolve(), "arxiv": m.group(1) if m else None}
    m = re.fullmatch(r'(?:PMID\s*:\s*)?(\d+)', target.strip(), re.I)
    if not m:
        m = re.search(r'^https?://pubmed\.ncbi\.nlm\.nih\.gov/(\d+)(?:/|$)', target)
    if m:
        return {"kind": "pubmed", "pmid": m.group(1)}
    m = re.fullmatch(r'(?:PMCID\s*:\s*)?(PMC\d+)', target.strip(), re.I)
    if not m:
        m = re.search(r'^https?://(?:pmc\.ncbi\.nlm\.nih\.gov|www\.ncbi\.nlm\.nih\.gov/pmc)/articles/(PMC\d+)', target, re.I)
    if m:
        return {"kind": "pmc", "pmcid": m.group(1).upper()}
    m = re.search(r'^https?://(?:www\.)?(biorxiv|medrxiv)\.org/content/(10\.(?:1101|64898)/[^?#]+)', target, re.I)
    if m:
        doi = re.sub(r'(?:\.full)?(?:\.pdf|\.source\.xml|\.full)?/?$', '', m.group(2))
        version = re.search(r'v(\d+)$', doi)
        return {"kind": "preprint", "server": m.group(1).lower(),
                "doi": re.sub(r'v\d+$', '', doi), "version": version.group(1) if version else None}
    doi = re.sub(r'^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)', '', target.strip(), flags=re.I)
    if re.fullmatch(r'10\.\d{4,9}/\S+', doi):
        return {"kind": "doi", "doi": urllib.parse.unquote(doi)}
    m = ARXIV_RE.search(target)
    if m and ("arxiv" in target.lower() or "huggingface.co/papers" in target or "alphaxiv" in target
              or "papers.cool" in target or re.fullmatch(r'\s*(arXiv:)?' + m.group(1) + r'\s*', target, re.I)):
        return {"kind": "arxiv", "arxiv": m.group(1)}
    if target.startswith(("http://", "https://")):
        return {"kind": "url-pdf", "url": target}
    sys.exit(f"认不出这个输入：{target}（要 arXiv ID / 链接 / PDF 路径 / 已有目录）")


# Biomedical metadata and full text share this entry point with routine fetching.
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
PMC_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com/"


def ncbi_get(operation, **params):
    params['tool'] = 'research-workflow'
    if os.environ.get('NCBI_EMAIL'):
        params['email'] = os.environ['NCBI_EMAIL']
    if os.environ.get('NCBI_API_KEY'):
        params['api_key'] = os.environ['NCBI_API_KEY']
    time.sleep(0.35)
    return http_get(EUTILS + operation + '.fcgi?' + urllib.parse.urlencode(params), timeout=60)[0]


def node_text(node):
    return ''.join(node.itertext()).strip() if node is not None else ''


def parse_pubmed(raw):
    root = ET.fromstring(raw)
    if root.find('ERROR') is not None:
        raise ValueError(node_text(root.find('ERROR')))
    records = []
    for item in root.findall('PubmedArticle'):
        article = item.find('MedlineCitation/Article')
        ids = {i.get('IdType'): node_text(i) for i in item.findall('PubmedData/ArticleIdList/ArticleId')}
        authors = []
        for a in article.findall('AuthorList/Author'):
            authors.append(node_text(a.find('CollectiveName')) or ' '.join(filter(None, [
                node_text(a.find('ForeName')) or node_text(a.find('Initials')), node_text(a.find('LastName'))])))
        date = article.find('Journal/JournalIssue/PubDate')
        published = ' '.join(node_text(e) for e in date) if date is not None else ''
        records.append({
            'source': 'pubmed', 'pmid': node_text(item.find('MedlineCitation/PMID')),
            'pmcid': ids.get('pmc'), 'doi': ids.get('doi'), 'title': node_text(article.find('ArticleTitle')),
            'authors': authors, 'published': published, 'journal_ref': node_text(article.find('Journal/Title')),
            'abstract': '\n'.join((p.get('Label', '') + ': ' if p.get('Label') else '') + node_text(p)
                                  for p in article.findall('Abstract/AbstractText')),
            'publication_types': [node_text(p) for p in article.findall('PublicationTypeList/PublicationType')],
            'abs_url': 'https://pubmed.ncbi.nlm.nih.gov/' + node_text(item.find('MedlineCitation/PMID')) + '/',
        })
    return records


def pubmed_search(query, limit):
    result = json.loads(ncbi_get('esearch', db='pubmed', term=query, retmode='json', retmax=limit))['esearchresult']
    if result.get('ERROR') or result.get('errorlist'):
        raise ValueError(str(result.get('ERROR') or result['errorlist']))
    ids = result.get('idlist', [])
    records = []
    for start in range(0, len(ids), 200):
        records.extend(parse_pubmed(ncbi_get('efetch', db='pubmed', id=','.join(ids[start:start + 200]), retmode='xml')))
    return {'query': query, 'query_translation': result.get('querytranslation'),
            'total': int(result['count']), 'returned': len(records), 'records': records}


def biomedical_meta(target):
    if target['kind'] == 'pubmed':
        records = parse_pubmed(ncbi_get('efetch', db='pubmed', id=target['pmid'], retmode='xml'))
        if not records:
            raise ValueError('PubMed returned no article for PMID ' + target['pmid'])
        return records[0]
    if target['kind'] == 'pmc':
        params = {'ids': target['pmcid'], 'format': 'json', 'tool': 'research-workflow'}
        if os.environ.get('NCBI_EMAIL'):
            params['email'] = os.environ['NCBI_EMAIL']
        raw, _ = http_get('https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?' + urllib.parse.urlencode(params))
        records = json.loads(raw).get('records', [])
        if not records or records[0].get('status') == 'error':
            raise ValueError('PMC identifier conversion failed: ' + str(records))
        record = records[0]
        if record.get('pmid'):
            return biomedical_meta({'kind': 'pubmed', 'pmid': str(record['pmid'])})
        return {'source': 'pmc', 'pmcid': target['pmcid'], 'doi': record.get('doi')}
    doi = target['doi']
    if target['kind'] == 'preprint' or doi.startswith(('10.1101/', '10.64898/')):
        servers = [target['server']] if target.get('server') else ['biorxiv', 'medrxiv']
        errors = []
        for server in servers:
            try:
                raw, _ = http_get('https://api.biorxiv.org/details/' + server + '/' + doi + '/na/json', timeout=60)
                records = [r for r in json.loads(raw).get('collection', []) if r.get('doi', '').lower() == doi.lower()]
                if target.get('version'):
                    records = [r for r in records if str(r.get('version')) == target['version']]
                if not records:
                    continue
                r = max(records, key=lambda x: int(x['version']))
                url = 'https://www.' + server + '.org/content/' + doi + 'v' + str(r['version'])
                return {'source': server, 'doi': doi, 'title': r['title'], 'authors': r['authors'].split('; '),
                        'published': r['date'], 'abstract': r['abstract'], 'version': str(r['version']),
                        'published_doi': r.get('published'), 'license': r.get('license'), 'abs_url': url,
                        'pdf_url': url + '.full.pdf', 'xml_url': r.get('jatsxml')}
            except Exception as e:
                errors.append(server + ': ' + str(e))
        raise ValueError('Preprint metadata unavailable: ' + ('; '.join(errors) or doi))
    result = pubmed_search('"' + doi + '"[AID]', 2)
    matches = [r for r in result['records'] if (r.get('doi') or '').lower() == doi.lower()]
    if len(matches) == 1:
        return matches[0]
    raw, _ = http_get('https://api.crossref.org/works/' + urllib.parse.quote(doi, safe=''), timeout=60)
    r = json.loads(raw)['message']
    date = r.get('published', {}).get('date-parts', [[]])[0]
    return {'source': 'crossref', 'doi': r['DOI'], 'title': (r.get('title') or [''])[0],
            'authors': [' '.join(filter(None, [a.get('given'), a.get('family')])) for a in r.get('author', [])],
            'published': '-'.join(str(n).zfill(2 if i else 4) for i, n in enumerate(date)),
            'journal_ref': (r.get('container-title') or [''])[0],
            'abstract': re.sub('<[^>]+>', '', r.get('abstract', '')), 'abs_url': r.get('URL'),
            'pdf_urls': [l['URL'] for l in r.get('link', []) if l.get('content-type') == 'application/pdf']}


def jats_article(raw):
    root = ET.fromstring(raw)
    article = root if root.tag == 'article' else root.find('.//article')
    if article is None or article.find('body') is None:
        raise ValueError('Response does not contain a JATS full-text article body')
    return article


def jats_text(article):
    lines, sections = [], []
    for tag in ('front', 'body', 'back'):
        part = article.find(tag)
        if part is None:
            continue
        for node in part.iter():
            if node.tag in ('article-title', 'title', 'p', 'table', 'ref'):
                text = ' '.join(node_text(node).split())
                if text:
                    if node.tag == 'title':
                        sections.append((len(lines) + 1, text))
                    lines.append(text)
    return '\n'.join(lines) + '\n', sections


def pmc_manifest(pmcid, version):
    raw, _ = http_get(PMC_BUCKET + '?' + urllib.parse.urlencode({'list-type': 2, 'prefix': pmcid + '.', 'delimiter': '/'}))
    ns = {'s': 'http://s3.amazonaws.com/doc/2006-03-01/'}
    prefixes = [p.text for p in ET.fromstring(raw).findall('s:CommonPrefixes/s:Prefix', ns)]
    if not prefixes:
        return None
    if version:
        prefixes = [p for p in prefixes if p == pmcid + '.' + version + '/']
    if len(prefixes) != 1:
        raise ValueError('PMC versions require an explicit --pmc-version: ' + ', '.join(prefixes))
    prefix = prefixes[0]
    raw, _ = http_get(PMC_BUCKET + prefix + prefix.rstrip('/') + '.json')
    record = json.loads(raw)
    if record.get('pmcid') != pmcid:
        raise ValueError('PMC manifest identifier mismatch')
    return record


def cloud_url(url):
    return url.replace('s3://pmc-oa-opendata/', PMC_BUCKET, 1)


def save_meta(dest, meta):
    (dest / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


def pdf_doi(pdf):
    import fitz
    with fitz.open(str(pdf)) as document:
        text = document[0].get_text()
    matches = set(d.rstrip('.,;)').lower() for d in re.findall(r'10\.\d{4,9}/[^\s<>]+', text))
    if len(matches) > 1:
        raise ValueError('Multiple first-page DOI candidates; verify article identity: ' + ', '.join(sorted(matches)))
    return next(iter(matches), None)


class PublisherLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.dois, self.titles, self.links, self.anchor = set(), set(), [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name', '').lower() in ('citation_title', 'dc.title'):
            self.titles.add(attrs.get('content', ''))
        if tag == 'meta' and attrs.get('name', '').lower() in ('citation_doi', 'dc.identifier'):
            value = re.sub(r'^(?:doi:|https?://(?:dx\.)?doi.org/)', '', attrs.get('content', ''), flags=re.I)
            if value.startswith('10.'):
                self.dois.add(value.lower())
        if tag == 'a' and attrs.get('href'):
            self.anchor = [attrs['href'], []]

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.links.append((self.anchor[0], ' '.join(' '.join(self.anchor[1]).split())))
            self.anchor = None


def publisher_supplements(meta):
    doi = meta.get('doi')
    if not doi:
        raise ValueError('No verified DOI; identify the article before checking publisher attachments')
    urls = [u for u in [meta.get('bibliographic_source'), meta.get('publisher_url')]
            if u and urllib.parse.urlsplit(u).hostname not in ('pubmed.ncbi.nlm.nih.gov', 'pmc.ncbi.nlm.nih.gov')]
    urls.append('https://doi.org/' + doi)
    attempts = []
    for url in dict.fromkeys(urls):
        try:
            raw, actual = http_get(url, timeout=45, tries=1)
            parser = PublisherLinks()
            parser.feed(raw.decode('utf-8', errors='replace'))
            if doi.lower() not in parser.dois:
                raise ValueError('Publisher page does not confirm the requested DOI')
            if meta.get('doi_source') == 'paper.pdf:first_page':
                title = re.sub(r'\W+', '', meta.get('title', '').casefold())
                if not title or title not in {re.sub(r'\W+', '', t.casefold()) for t in parser.titles}:
                    raise ValueError('First-page DOI candidate does not match the local article title; verify identity manually')
            items = []
            for href, label in parser.links:
                link = urllib.parse.urljoin(actual, href)
                parsed = urllib.parse.urlsplit(link)
                decoded = urllib.parse.unquote(parsed.path)
                is_esm = (parsed.hostname == 'media.springernature.com' and '/esm/' in decoded
                          and ('art:' + doi.lower() + '/') in decoded.lower())
                labelled = re.search(r'supplement|supporting information|source data', label, re.I)
                is_file = Path(parsed.path).suffix.lower() in ('.pdf', '.zip', '.xlsx', '.xls', '.csv', '.tsv', '.docx', '.doc', '.txt', '.gz')
                if parsed.scheme in ('http', 'https') and is_file and (is_esm or labelled):
                    items.append({'reference': link, 'url': link, 'label': label, 'discovered_on': actual})
            attempts.append({'url': url, 'final_url': actual, 'status': 'checked', 'items': len(items)})
            if items:
                return items, attempts
        except ValueError as e:
            attempts.append({'url': url, 'status': 'identity_unresolved', 'error': str(e)})
        except Exception as e:
            attempts.append({'url': url, 'status': 'access_failed', 'error': str(e)})
    return [], attempts


def validate_supplement(raw, name):
    prefix = raw[:8192].decode('utf-8-sig', errors='replace').lstrip()
    prefix = re.sub(r'^(?:(?:<!--.*?-->|<\?.*?\?>)\s*)+', '', prefix, flags=re.S)
    if not raw or re.match(r'<(?:!doctype\s+html|html|head|body)(?:\s|>)', prefix, re.I):
        raise ValueError('Empty file or HTML response instead of an attachment: ' + name)
    suffix = Path(name).suffix.lower()
    if suffix == '.pdf':
        import fitz
        if not raw.startswith(b'%PDF'):
            raise ValueError('Invalid PDF signature: ' + name)
        with fitz.open(stream=raw, filetype='pdf') as document:
            if not len(document):
                raise ValueError('PDF has no pages: ' + name)
    elif suffix in ('.zip', '.xlsx', '.docx'):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if not archive.namelist() or archive.testzip() is not None:
                raise ValueError('Empty or corrupt ZIP container: ' + name)
            required = {'xlsx': 'xl/workbook.xml', 'docx': 'word/document.xml'}.get(suffix[1:])
            if required and required not in archive.namelist():
                raise ValueError('Wrong document format: ' + name)


def acquire_supplements(dest, meta, article=None, manifest=None, download=False):
    previous = meta.setdefault('materials', {}).get('supplements', {})
    items = {i['reference']: dict(i) for i in previous.get('items', [])}
    media = [cloud_url(u) for u in (manifest or {}).get('media_urls', [])]
    if article is not None:
        for node in article.findall('.//supplementary-material'):
            for entry in node.iter():
                href = entry.get('{http://www.w3.org/1999/xlink}href')
                if href:
                    item = items.setdefault(href, {'reference': href})
                    name = Path(urllib.parse.urlsplit(href).path).name
                    matches = [u for u in media if Path(urllib.parse.urlsplit(u).path).name == name]
                    if len(matches) == 1:
                        item['url'] = matches[0]
    attempts = []
    if download:
        try:
            candidate = dict(meta)
            if not meta.get('doi') and (dest / 'paper.pdf').exists():
                doi = pdf_doi(dest / 'paper.pdf')
                if doi:
                    candidate.update(doi=doi, doi_source='paper.pdf:first_page',
                                     title=meta.get('title') or title_from_pdf(dest / 'paper.pdf'))
            discovered, attempts = publisher_supplements(candidate)
            if not meta.get('doi') and any(a['status'] == 'checked' for a in attempts):
                meta.update({k: candidate[k] for k in ('doi', 'doi_source', 'title')})
            for item in discovered:
                items.setdefault(item['reference'], {}).update(item)
        except Exception as e:
            attempts.append({'status': 'identity_unresolved', 'error': str(e)})
    names = {}
    for item in items.values():
        item['verified'] = False
        name = Path(urllib.parse.urlsplit(item['reference']).path).name
        if not name or name in ('.', '..'):
            item.update(status='failed', error='Attachment has no usable filename')
            continue
        path = dest / 'supplements' / name
        collision = name in names and names[name] != item.get('url')
        names[name] = item.get('url')
        try:
            if collision and path.is_file():
                if not download or not item.get('url'):
                    item.update(status='identified_not_downloaded', reason='Same filename; mirror identity not verified')
                    continue
                other, actual = http_get(item['url'], timeout=180)
                validate_supplement(other, name)
                if other != path.read_bytes():
                    raise ValueError('Different attachments share filename; existing file preserved: ' + name)
                item['final_url'] = actual
            if path.is_file():
                raw = path.read_bytes()
                validate_supplement(raw, name)
            elif download and item.get('url'):
                raw, actual = http_get(item['url'], timeout=180)
                validate_supplement(raw, name)
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(raw)
                item['final_url'] = actual
            else:
                item['status'] = 'identified_not_downloaded'
                continue
            item.update(status='available', file=str(path.relative_to(dest)), bytes=len(raw), verified=True)
            item.pop('error', None)
            item.pop('reason', None)
        except Exception as e:
            item.update(status='failed', error=str(e))
    available = sum(i['status'] == 'available' for i in items.values())
    status = ('available' if items and available == len(items) else 'partial' if available else
              'failed' if any(i['status'] == 'failed' for i in items.values()) else 'identified_not_downloaded' if items else
              'not_located' if any(a['status'] == 'checked' for a in attempts) else
              'identity_unresolved' if any(a['status'] == 'identity_unresolved' for a in attempts) else
              'access_failed' if attempts else 'not_checked')
    state = dict(previous, status=status, items=list(items.values()))
    if download:
        state.update(checked_on=datetime.date.today().isoformat(), publisher_checks=attempts)
    meta['materials']['supplements'] = state
    return state


def check_cached_identity(previous, incoming, dest):
    if dest and any((dest / name).exists() for name in ('paper.pdf', 'paper.xml')):
        for key in ('doi', 'pmid', 'pmcid', 'arxiv_id', 'version'):
            if previous.get(key) and incoming.get(key) and str(previous[key]).lower() != str(incoming[key]).lower():
                raise ValueError('Cached article ' + key + ' differs; use its existing identifier/version or a separate destination')


def fetch_biomedical(target, args):
    dest = target.get('path') if target['kind'] == 'dir' else None
    if dest is None and args.dest:
        dest = Path(args.dest).expanduser().resolve()
    previous = json.loads((dest / 'meta.json').read_text()) if dest and (dest / 'meta.json').exists() else {}
    meta = dict(previous)
    try:
        if target['kind'] != 'dir':
            incoming = biomedical_meta(target)
            check_cached_identity(previous, incoming, dest)
            meta.update(incoming)
        elif not any((dest / name).exists() for name in ('paper.xml', 'paper.pdf')):
            raise ValueError('Local article directory has no paper.xml or paper.pdf')
        if dest and args.pmc_version and previous.get('pmc_version') and (
                (dest / 'paper.pdf').exists() or (dest / 'paper.xml').exists()) and args.pmc_version != str(previous['pmc_version']):
            raise ValueError('Cached PMC version differs; use its existing version or a separate destination')
    except Exception as e:
        if dest is None:
            slug = args.slug or slugify('', target.get('pmid') or target.get('pmcid') or target.get('doi'))
            dest = pick_dest(args, slug)
        dest.mkdir(parents=True, exist_ok=True)
        if previous:
            meta = dict(previous)
            meta['last_attempt'] = {'status': 'failed', 'error': str(e), 'target': args.target if hasattr(args, 'target') else str(target)}
            save_meta(dest, meta)
            log('Metadata retrieval failed: ' + str(e))
            return 1
        meta.update({k: v for k, v in target.items() if k in ('pmid', 'pmcid', 'doi')})
        meta['materials'] = dict(previous.get('materials', {}))
        meta['materials']['metadata'] = {'status': 'failed', 'error': str(e)}
        save_meta(dest, meta)
        log('Metadata retrieval failed: ' + str(e))
        return 1
    if dest is None:
        fallback = meta.get('pmid') or meta.get('pmcid') or meta.get('doi')
        dest = pick_dest(args, args.slug or slugify(meta.get('title'), fallback))
    dest.mkdir(parents=True, exist_ok=True)
    materials = dict(previous.get('materials', {}))
    meta.pop('last_attempt', None)
    meta.update({'materials': materials, 'fetched': datetime.date.today().isoformat()})
    materials['metadata'] = {'status': 'available', 'source': meta.get('source')}
    materials['abstract'] = {'status': 'available' if meta.get('abstract') else 'not_available'}
    materials['fulltext'] = {'status': 'not_available'}
    save_meta(dest, meta)
    pdf, xml = dest / 'paper.pdf', dest / 'paper.xml'
    errors, manifest = [], None
    if meta.get('pmcid') and (not (xml.exists() and pdf.exists()) or args.supplements or args.pmc_version):
        try:
            manifest = pmc_manifest(meta['pmcid'], args.pmc_version or previous.get('pmc_version'))
        except ValueError as e:
            materials['fulltext'] = {'status': 'failed', 'error': str(e)}
            save_meta(dest, meta)
            log(str(e))
            return 1
        except Exception as e:
            errors.append('PMC cloud: ' + str(e))
        if manifest:
            meta.update({'pmc_version': str(manifest['version']), 'license': manifest.get('license_code'),
                         'is_manuscript': manifest.get('is_manuscript'), 'is_retracted': manifest.get('is_retracted')})
            meta['xml_url'] = cloud_url(manifest['xml_url'])
            if manifest.get('pdf_url'):
                meta['pdf_url'] = cloud_url(manifest['pdf_url'])
    if not xml.exists() and (meta.get('xml_url') or meta.get('pmcid')):
        try:
            if meta.get('xml_url'):
                raw, actual = http_get(meta['xml_url'])
            else:
                raw = ncbi_get('efetch', db='pmc', id=meta['pmcid'][3:], retmode='xml')
                actual = 'PMC EFetch'
            jats_article(raw)
            xml.write_bytes(raw)
            materials['xml'] = {'status': 'available', 'source': actual}
        except Exception as e:
            errors.append('Full-text XML: ' + str(e))
    urls = ([meta['pdf_url']] if meta.get('pdf_url') else []) + meta.get('pdf_urls', [])
    if not pdf.exists():
        for url in urls:
            try:
                raw, actual = http_get(url, timeout=180)
                if not raw.startswith(b'%PDF'):
                    raise ValueError('Response is not a PDF: ' + actual)
                import fitz
                with fitz.open(stream=raw, filetype='pdf') as document:
                    if not len(document):
                        raise ValueError('PDF has no pages')
                pdf.write_bytes(raw)
                materials['pdf'] = {'status': 'available', 'source': actual}
                break
            except Exception as e:
                errors.append('PDF: ' + str(e))
    article = None
    try:
        if xml.exists():
            article = jats_article(xml.read_bytes())
            info = article.find('front/article-meta')
            if info is not None:
                meta.setdefault('title', node_text(info.find('title-group/article-title')))
                meta.setdefault('abstract', node_text(info.find('abstract')))
            materials['xml'] = dict(materials.get('xml', {}), status='available', file='paper.xml')
        if pdf.exists():
            import fitz
            with fitz.open(str(pdf)) as document:
                if not len(document):
                    raise ValueError('Cached PDF has no pages')
                meta['pdf_pages'] = len(document)
            materials['pdf'] = dict(materials.get('pdf', {}), status='available', file='paper.pdf')
    except Exception as e:
        materials['fulltext'] = {'status': 'failed', 'error': str(e)}
        save_meta(dest, meta)
        log('Cached full text is invalid: ' + str(e))
        return 1
    materials['abstract'] = {'status': 'available' if meta.get('abstract') else 'not_available'}
    formats = [name for name, path in [('pdf', pdf), ('xml', xml)] if path.exists()]
    for name in ('pdf', 'xml'):
        if name not in formats:
            materials[name] = {'status': 'not_available'}
    materials['fulltext'] = {'status': 'available' if formats else 'not_available', 'formats': formats, 'errors': errors}
    supplements = acquire_supplements(dest, meta, article, manifest, args.supplements)
    materials.setdefault('latex', {'status': 'not_checked'})
    save_meta(dest, meta)
    log('Article: ' + meta.get('title', '') + '\nDirectory: ' + str(dest))
    if not formats:
        log('Metadata/abstract saved; full text was not acquired. See meta.json for sources and errors.')
        return 2
    if not args.download_only:
        if pdf.exists():
            build_txt(dest)
            build_outline(dest, meta)
            import extract_figures
            extract_figures.extract(dest, dpi=args.dpi, quiet=True)
        else:
            text, sections = jats_text(article)
            (dest / 'paper.txt').write_text(text, encoding='utf-8')
            (dest / 'outline.md').write_text('# Article outline\n\nLocations refer to paper.txt lines.\n\n' +
                '\n'.join('- L' + str(line) + ' ' + title for line, title in sections) + '\n', encoding='utf-8')
        links = collect_links(dest, meta, {})
        meta['links'] = links
        if args.code:
            repo = args.code.rstrip('/').removesuffix('.git').split('/')[-1]
            code_dir = Path(args.code_dir).expanduser().resolve() if args.code_dir else (
                Path('复现') / repo.lower() / 'code' if Path('复现').is_dir() else dest / 'code')
            state = clone(args.code, code_dir) if not args.no_clone else 'not_requested'
            meta.update(official_code=args.code, official_code_from='--code',
                        code_dir=str(code_dir.resolve()) if state in ('ok', 'exists') else None)
            materials['code'] = {'status': state}
        save_meta(dest, meta)
    if args.supplements and supplements['status'] != 'available':
        log('Full text saved; requested supplementary files are incomplete. See meta.json for status.')
        return 2
    log('Materials saved. Intensive reading starts only when explicitly requested.')
    return 0


def tidy_title(t):
    """arXiv 标题里会带 LaTeX：'$π_0$: A Vision-...' → 'π0: A Vision-...'"""
    def inline(m):
        x = m.group(1)
        for k, v in {"\\pi": "π", "\\alpha": "α", "\\beta": "β", "\\lambda": "λ", "\\mu": "μ", "\\tau": "τ"}.items():
            x = x.replace(k, v)
        return re.sub(r'[_^{}\\]', '', x)
    return " ".join(re.sub(r'\$([^$]*)\$', inline, t).split())


def slugify(title, fallback):
    if not title:
        return fallback.replace("/", "-")
    t = "".join(GREEK.get(c, c) for c in title)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    head, _, rest = t.partition(":")
    words = lambda s: [w for w in re.split(r'[^A-Za-z0-9]+', s.lower()) if w and w not in STOP]
    if rest and len(head.split()) <= 3:  # "π0: A Vision-Language-Action ..." → 方法名在冒号前
        name = []
        for w in words(head):
            if w.isdigit() and name:
                name[-1] += w
            else:
                name.append(w)
        parts = ["-".join(name)] + words(rest)[:3]
    else:
        parts = words(t)[:5]
    slug = "-".join(p for p in parts if p)[:60].strip("-")
    return slug or fallback.replace("/", "-")


def pick_dest(args, slug):
    if args.dest:
        return Path(os.path.expanduser(args.dest)).resolve()
    if args.root:
        return (Path(os.path.expanduser(args.root)) / slug).resolve()
    if Path("related_work").is_dir():
        return (Path("related_work") / slug).resolve()
    if os.environ.get("PAPERS_DIR"):
        return (Path(os.path.expanduser(os.environ["PAPERS_DIR"])) / slug).resolve()
    return (Path("papers") / slug).resolve()


# ---------------- arXiv：元数据、PDF、源码 ----------------

def arxiv_meta(aid):
    try:
        xml, _ = http_get([f"https://export.arxiv.org/api/query?id_list={aid}",
                           f"http://export.arxiv.org/api/query?id_list={aid}"], timeout=40)
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠ arXiv API 没连上（{e}），元数据从 PDF 里凑")
        return {}
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    try:
        e = ET.fromstring(xml).find("a:entry", ns)
    except ET.ParseError:
        return {}
    if e is None:
        return {}
    txt = lambda tag: " ".join((e.findtext(tag, default="", namespaces=ns) or "").split())
    idurl = txt("a:id")
    if "arxiv.org/abs/" not in idurl:
        return {}
    ver = re.search(r'v(\d+)$', idurl)
    return {
        "arxiv_id": aid.split("v")[0] if re.match(NEW_ID, aid) else re.sub(r'v\d+$', '', aid),
        "version": f"v{ver.group(1)}" if ver else "",
        "title": tidy_title(txt("a:title")),
        "authors": [" ".join(a.findtext("a:name", default="", namespaces=ns).split()) for a in e.findall("a:author", ns)],
        "published": txt("a:published")[:10],
        "updated": txt("a:updated")[:10],
        "abstract": txt("a:summary"),
        "comment": txt("x:comment"),
        "journal_ref": txt("x:journal_ref"),
        "categories": [c.get("term") for c in e.findall("a:category", ns)],
        "abs_url": f"https://arxiv.org/abs/{aid}",
    }


def hf_paper(aid):
    """Hugging Face Papers 收录了作者认领的代码仓库和项目页；连不上就算了。"""
    try:
        raw, _ = http_get(f"https://huggingface.co/api/papers/{aid.split('v')[0]}", timeout=12, tries=1)
        d = json.loads(raw)
        return {"github": d.get("githubRepo") or "", "project": d.get("projectPage") or ""}
    except Exception:  # noqa: BLE001
        return {}


def safe_extract(raw, dest):
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
        for m in tf.getmembers():
            target = (dest / m.name).resolve()
            if m.issym() or m.islnk() or m.isdev() or not str(target).startswith(str(root)):
                continue  # 符号链接、设备文件、../ 越界：一律不解
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)


def fetch_source(aid, src_dir):
    """返回 'tar' / 'single-tex' / 'pdf-only' / 'failed'。"""
    try:
        data, _ = http_get([f"https://arxiv.org/e-print/{aid}", f"https://export.arxiv.org/e-print/{aid}"], timeout=240)
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠ 源码没下下来（{e}）")
        return "failed"
    if data[:4] == b"%PDF":
        return "pdf-only"
    raw = data
    if data[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(data)
        except OSError:
            pass
    try:
        safe_extract(raw, src_dir)
        if any(src_dir.rglob("*.tex")):
            return "tar"
    except tarfile.TarError:
        pass
    if raw[:4] == b"%PDF":
        return "pdf-only"
    text = raw.decode("utf-8", errors="replace")
    if "\\documentclass" in text or "\\begin{document}" in text:
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "main.tex").write_text(text, encoding="utf-8")
        return "single-tex"
    return "failed"


def arxiv_id_from_pdf(pdf):
    """arXiv 生成的 PDF 第一页左边有竖排的 'arXiv:2410.24164v4 [cs.LG] ...' 水印。"""
    try:
        import fitz
        with fitz.open(str(pdf)) as d:
            m = re.search(r'arXiv:\s*(' + NEW_ID + ')', d[0].get_text())
            return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None


def title_from_pdf(pdf):
    """非 arXiv 的 PDF：先看文档属性里的标题，不像标题就取第一页上半部分字号最大的那几行。"""
    try:
        import fitz
        with fitz.open(str(pdf)) as d:
            t = (d.metadata or {}).get("title") or ""
            if len(t) > 12 and not re.search(r'untitled|microsoft word|\.pdf$|\.dvi$|^p\d+$', t, re.I):
                return " ".join(t.split())
            page = d[0]
            spans = []
            for b in page.get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    for sp in l["spans"]:
                        if sp["text"].strip() and sp["bbox"][1] < 0.45 * page.rect.height:
                            spans.append((round(sp["size"], 1), sp["bbox"][1], sp["text"].strip()))
            if not spans:
                return ""
            big = max(x[0] for x in spans)
            words = [x[2] for x in sorted(spans, key=lambda x: x[1]) if x[0] >= big - 0.5]
            return " ".join(" ".join(words).split())[:200]
    except Exception:  # noqa: BLE001
        return ""


# ---------------- LaTeX：拍平、宏、大纲 ----------------

VERB_ENVS = r'verbatim|Verbatim|lstlisting|minted|alltt'


def strip_comments(text):
    text = re.sub(r'\\begin\{comment\}.*?\\end\{comment\}', '', text, flags=re.S)
    out, verb = [], False
    for line in text.split("\n"):
        if re.search(r'\\begin\{(' + VERB_ENVS + r')\}', line):
            verb = True
        if verb:
            out.append(line)
            if re.search(r'\\end\{(' + VERB_ENVS + r')\}', line):
                verb = False
            continue
        if re.match(r'\s*%', line):  # 整行注释直接丢，不留空行
            continue
        out.append(re.sub(r'(?<!\\)%.*$', '', line))
    return "\n".join(out)


def drop_iffalse(text):
    """去掉 \\iffalse ... \\fi 包起来的废稿（会数嵌套的 \\ifxxx）。"""
    out, i = [], 0
    for m in re.finditer(r'\\iffalse\b', text):
        if m.start() < i:
            continue
        depth, j = 1, m.end()
        for t in re.finditer(r'\\(if(?!thenelse)[a-zA-Z@]*|fi)\b', text[m.end():]):
            depth += 1 if t.group(1) != "fi" else -1
            if depth == 0:
                j = m.end() + t.end()
                break
        else:
            continue
        out.append(text[i:m.start()])
        i = j
    out.append(text[i:])
    return "".join(out)


INPUT_RE = re.compile(r'\\(?:input|include|subfile)\s*\{([^}]+)\}|\\(?:sub)?import\s*\{([^}]*)\}\s*\{([^}]+)\}'
                      r'|\\input\s+([^\s{}\\]+)')


def flatten(path, root, seen=None, depth=0):
    seen = seen or set()
    if depth > 12 or path in seen:
        return ""
    seen.add(path)
    text = drop_iffalse(strip_comments(path.read_text(encoding="utf-8", errors="ignore")))

    def repl(m):
        if m.group(1):
            name, base = m.group(1).strip(), path.parent
        elif m.group(3):
            name, base = m.group(3).strip(), (path.parent / m.group(2).strip())
        else:
            name, base = m.group(4).strip(), path.parent
        for cand in (base / name, root / name, base / (name + ".tex"), root / (name + ".tex")):
            if cand.is_file() and cand.suffix in (".tex", ".bbl", ".tikz", ".pgf", ""):
                if cand.suffix in (".tikz", ".pgf"):
                    return f"% [图：{cand.relative_to(root)}]"
                return flatten(cand.resolve(), root, seen, depth + 1)
        return m.group(0)

    return INPUT_RE.sub(repl, text)


def find_main_tex(src):
    readme = src / "00README.json"
    if readme.exists():
        try:
            for s in json.loads(readme.read_text()).get("sources", []):
                if s.get("usage") == "toplevel" and (src / s["filename"]).exists():
                    return src / s["filename"]
        except (ValueError, KeyError):
            pass
    cands = []
    for p in src.rglob("*.tex"):
        if ".stale" in p.parts:
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r'^\s*\\documentclass', txt, re.M):
            score = (10 if "\\begin{document}" in txt else 0) \
                + (3 if p.stem.lower() in ("main", "paper", "ms", "arxiv", "manuscript", "root") else 0) \
                + (2 if p.parent == src else 0) + min(len(txt) / 20000, 3)
            cands.append((score, str(p)))
    return Path(max(cands)[1]) if cands else None


DEF_RE = re.compile(r'\\(?:(?:re)?newcommand|providecommand|DeclareRobustCommand|DeclareMathOperator)\*?|\\def\s*(?=\\)')


def macro_defs(text):
    """正文前面定义的宏：返回 [(名字, 完整定义)]。"""
    from extract_figures import read_group
    out = []
    for m in DEF_RE.finditer(text):
        i = m.end()
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if i >= len(text):
            break
        if text[i] == "{":
            j, name = read_group(text, i)
            name = name.strip()
        elif text[i] == "\\":
            nm = re.match(r'\\[A-Za-z@]+|\\.', text[i:])
            name, j = nm.group(0), i + nm.end()
        else:
            continue
        if not name.startswith("\\"):
            continue
        k = j
        if m.group(0).startswith("\\def"):
            pm = re.match(r'[^{]*', text[k:])
            k += pm.end()
        else:
            for _ in range(2):  # [参数个数][默认值]
                ws = re.match(r'\s*\[[^\]]*\]', text[k:])
                if ws:
                    k += ws.end()
        ws = re.match(r'\s*', text[k:])
        k += ws.end()
        if k < len(text) and text[k] == "{":
            end, _ = read_group(text, k)
            out.append((name, " ".join(text[m.start():end].split())))
    return out


def strip_bib(body):
    body = re.sub(r'\\begin\{thebibliography\}.*?\\end\{thebibliography\}', '', body, flags=re.S)
    body = re.sub(r'\\(?:bibliography|bibliographystyle|addbibresource)\s*\{[^}]*\}', '', body)
    return re.sub(r'\\printbibliography(\[[^\]]*\])?', '', body)


def build_tex(dest):
    src = dest / "source"
    main = find_main_tex(src) if src.is_dir() else None
    if not main:
        return None
    full = flatten(main.resolve(), src.resolve())
    b = full.find("\\begin{document}")
    e = full.rfind("\\end{document}")
    pre, body = (full[:b], full[b + len("\\begin{document}"):e if e > b else None]) if b >= 0 else ("", full)
    body = strip_bib(body)
    body = re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', body).strip("\n")
    used = []
    seen = set()
    for name, d in macro_defs(pre):
        if name in seen:
            continue
        if re.search(re.escape(name) + r'(?![A-Za-z@])', body):
            seen.add(name)
            used.append(d)
    head = [f"% paper.tex —— 由 source/{main.relative_to(src)} 拍平：\\input 已展开，注释、废稿（\\iffalse）、参考文献已去掉。",
            "% 读正文就读这个文件；行号和 outline.md 对得上。",
            f"% ==== 正文用到的自定义宏（{len(used)} 个；看不懂的记号先查这里）===="]
    head += used
    head += ["% ==== 正文 ===="]
    (dest / "paper.tex").write_text("\n".join(head) + "\n" + body + "\n", encoding="utf-8")
    return {"main": str(main.relative_to(dest)), "macros": len(used),
            "lines": len(head) + body.count("\n") + 1}


EQ_ENVS = r'equation|align|gather|multline|eqnarray|flalign'


def build_outline(dest, meta):
    import extract_figures as ef  # noqa: F811
    L = [f"# 大纲 · {meta.get('title', '')}", ""]
    tex = dest / "paper.tex"
    if tex.exists():
        lines = tex.read_text(encoding="utf-8").split("\n")
        secs = ef.latex_sections(lines)
        levels = {}
        for i, line in enumerate(lines, 1):
            mm = re.search(r'\\(section|subsection|subsubsection)\*?\s*[\[{]', line)
            if mm:
                levels[i] = ["section", "subsection", "subsubsection"].index(mm.group(1))
        n = len(lines)
        L.append("行号指 `paper.tex`。按实际文件读取工具定位到对应行，或用 sed 读取指定范围。")
        L.append("")
        abs_line = next((i for i, l in enumerate(lines, 1) if "\\begin{abstract}" in l), None)
        if abs_line:
            L.append(f"- L{abs_line} 摘要")
        app_line = next((i for i, l in enumerate(lines, 1) if re.match(r'\s*\\appendix\b', l)), None)
        for k, (i, num, title) in enumerate(secs):
            lvl = levels.get(i, 0)
            end = n
            for j, _, _ in secs[k + 1:]:
                if levels.get(j, 0) <= lvl:
                    end = j - 1
                    break
            if app_line and i > app_line and (k == 0 or secs[k - 1][0] < app_line):
                L.append(f"- L{app_line} —— 附录 ——")
            shown = title or ("（无标题，多半是附录开头）" if app_line and i > app_line else "（无标题）")
            L.append(f"{'  ' * lvl}- L{i}–{end} {(num + ' ') if num else ''}{shown}")
        # 公式：训练那一站找 loss 用
        eqs = []
        for m in re.finditer(r'\\begin\{(' + EQ_ENVS + r')(\*?)\}(.*?)\\end\{\1\2\}', "\n".join(lines), re.S):
            ln = "\n".join(lines).count("\n", 0, m.start()) + 1
            content = m.group(3)
            labels = re.findall(r'\\label\{([^}]+)\}', content)
            content = " ".join(re.sub(r'\\label\{[^}]+\}', '', content).split())
            eqs.append((ln, labels, content))
        for m in re.finditer(r'(?m)^\s*\\\[(.*?)\\\]', "\n".join(lines), re.S):
            ln = "\n".join(lines).count("\n", 0, m.start()) + 1
            eqs.append((ln, [], " ".join(m.group(1).split())))
        eqs.sort()
        if eqs:
            L += ["", f"## 公式（{len(eqs)} 个，按出现顺序；L 后面是所在小节）", ""]
            for ln, labels, content in eqs:
                lab = f" `{', '.join(labels)}`" if labels else ""
                prev = content[:110] + ("…" if len(content) > 110 else "")
                L.append(f"- L{ln} {ef.section_of(secs, ln)}{lab}：{prev}")
        L += ["", f"图表的页码、caption、在哪节被引用：见 `{ef.figures_dir(dest).name}/index.md`。"]
    else:
        L.append("没有 LaTeX 源码：正文读 `paper.txt`（按页分好了），公式只能看 PDF 渲染（`crop.py paper.pdf --page N`）。")
        L.append("")
        try:
            import fitz
            with fitz.open(str(dest / "paper.pdf")) as d:
                toc = d.get_toc()
            for lvl, title, page in toc:
                L.append(f"{'  ' * (lvl - 1)}- 第 {page} 页 · {title}")
            if not toc:
                L.append("（PDF 没有书签。用 `grep -n` 在 paper.txt 里找 Introduction / Method / Experiments 这类标题。）")
        except Exception:  # noqa: BLE001
            pass
        L += ["", f"图表的页码和 caption：见 `{ef.figures_dir(dest).name}/index.md`。"]
    (dest / "outline.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def build_txt(dest):
    try:
        import fitz
    except ImportError:
        if shutil.which("pdftotext"):
            subprocess.run(["pdftotext", "-layout", str(dest / "paper.pdf"), str(dest / "paper.txt")], check=False)
        return None
    from extract_figures import clean
    parts = []
    with fitz.open(str(dest / "paper.pdf")) as d:
        n = len(d)
        for i, page in enumerate(d, 1):
            text = "\n".join(l for l in clean(page.get_text()).split("\n")
                             if not l.strip() or l.count("\ufffd") < 0.3 * len(l.strip()))  # 图里字体抽出来的乱码行
            parts.append(f"\n===== 第 {i} 页 =====\n" + text)
    (dest / "paper.txt").write_text("".join(parts), encoding="utf-8")
    return n


# ---------------- 代码 / 项目页链接 ----------------

URL_RE = re.compile(r'https?://[^\s<>"\'{}|\\^`\[\]]+')
SKIP_OWNERS = {"sponsors", "features", "orgs", "topics", "settings", "marketplace", "about", "login"}


def norm_url(u):
    u = u.rstrip(".,;:)]}>'\"")
    u = re.sub(r'#.*$', '', u)
    return u.rstrip("/")


def classify(u):
    m = re.search(r'github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)', u)
    if m:
        owner, repo = m.group(1), re.sub(r'\.git$', '', m.group(2))
        if owner.lower() in SKIP_OWNERS or repo.lower().endswith(".github.io"):
            return None, None  # 项目页模板（nerfies.github.io 之类）的致谢链接
        return "code", f"https://github.com/{owner}/{repo}"
    if re.search(r'huggingface\.co/(?!papers)', u):
        return "hf", u
    if ".github.io" in u or "sites.google.com" in u or re.search(r'project|page|website', u, re.I):
        return "project", u
    return "other", u


def collect_links(dest, meta, hf):
    score = defaultdict(lambda: {"score": 0, "from": []})

    def add(u, w, src, hint=""):
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        kind, url = classify(norm_url(u))
        if kind == "other" and re.search(r'project|website|videos|homepage', hint, re.I):
            kind = "project"
        if not kind or re.search(r'arxiv\.org|doi\.org|creativecommons', url or ""):
            return
        if kind == "other" and src in ("arXiv 备注", "摘要", "HF Papers"):
            kind = "project"
        s = score[(kind, url)]
        s["score"] += w
        if src not in s["from"]:
            s["from"].append(src)

    for u in URL_RE.findall(meta.get("comment", "")):
        add(u, 5, "arXiv 备注")
    for u in URL_RE.findall(meta.get("abstract", "")):
        add(u, 5, "摘要")
    if hf.get("github"):
        add(hf["github"], 6, "HF Papers")
    if hf.get("project"):
        add(hf["project"], 4, "HF Papers")
    xml = dest / 'paper.xml'
    if xml.exists():
        article = jats_article(xml.read_bytes())
        for node in article.iter():
            if node.tag in ('ext-link', 'uri'):
                url = node.get('{http://www.w3.org/1999/xlink}href') or node_text(node)
                if url.startswith(('http://', 'https://')):
                    add(url, 3, 'Article XML')
    try:
        import fitz
        with fitz.open(str(dest / "paper.pdf")) as d:
            for pno, page in enumerate(d):
                first = pno < 2
                for ln in page.get_links():
                    if ln.get("uri"):
                        add(ln["uri"], 4 if first else 0.5, "首页链接" if first else "正文链接")
                if first:
                    for u in URL_RE.findall(page.get_text()):
                        add(u, 3, "首页文字")
    except Exception:  # noqa: BLE001
        pass
    tex = dest / "paper.tex"
    if tex.exists():
        for line in tex.read_text(encoding="utf-8", errors="ignore").split("\n"):
            if re.search(r'code|github|release|available|project|website|open[- ]?source', line, re.I):
                for u in set(re.findall(r'\\(?:url|href)\{([^}]+)\}', line) + URL_RE.findall(line)):
                    add(u, 2, "正文提到", hint=line)
    out = defaultdict(list)
    merged = {}
    for (kind, url), s in score.items():  # 同一个地址先后被归成 other / project 时合并到 project
        key = url
        if key in merged and merged[key][0] == "project":
            merged[key][1]["score"] += s["score"]
            continue
        if key in merged and kind == "project":
            s["score"] += merged[key][1]["score"]
        merged[key] = (kind, s)
    for url, (kind, s) in sorted(merged.items(), key=lambda kv: -kv[1][1]["score"]):
        if kind == "other" and s["score"] < 2:  # 正文里顺手提到的商品 / 数据集网页，不是这篇的东西
            continue
        out[kind].append({"url": url, "score": round(s["score"], 1), "from": s["from"]})
    return dict(out)


def code_from_project_page(url):
    try:
        raw, _ = http_get(url, timeout=15, tries=1)
    except Exception:  # noqa: BLE001
        return None
    cnt = defaultdict(int)
    for u in URL_RE.findall(raw.decode("utf-8", errors="ignore")):
        kind, norm = classify(norm_url(u))
        if kind == "code":
            cnt[norm] += 1
    return max(cnt, key=cnt.get) if cnt else None


def clone(url, code_dir):
    if code_dir.exists() and any(code_dir.iterdir()):
        return "exists"
    if not shutil.which("git"):
        return "no-git"
    code_dir.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1", GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", "--single-branch", url, str(code_dir)],
                           env=env, capture_output=True, text=True, timeout=300)
        return "ok" if r.returncode == 0 else "failed: " + r.stderr.strip().split("\n")[-1][:160]
    except subprocess.TimeoutExpired:
        shutil.rmtree(code_dir, ignore_errors=True)
        return "timeout"


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="把一篇论文拉到本地，备好精读材料")
    ap.add_argument("target", nargs='?', help="arXiv / PMID / PMCID / DOI / bioRxiv / PDF / article directory")
    ap.add_argument("--dest", help="论文目录（完整路径）")
    ap.add_argument("--root", help="论文目录的上一级，目录名自动用 slug")
    ap.add_argument("--slug", help="目录名，默认从标题生成")
    ap.add_argument("--code-dir", help="代码克隆到哪")
    ap.add_argument("--no-clone", action="store_true", help="只找代码链接，不克隆")
    ap.add_argument("--code", help="官方代码仓库地址（论文里没写、自己搜到的时候用；会克隆并记进 meta.json）")
    ap.add_argument("--dpi", type=int, default=220, help="裁图分辨率")
    ap.add_argument('--download-only', action='store_true', help='Fetch original materials and metadata without text extraction, figures or code')
    ap.add_argument('--pubmed-query', help='Search PubMed and print metadata as JSON without fetching full text')
    ap.add_argument('--limit', type=int, help='Required result limit for --pubmed-query (1..10000)')
    ap.add_argument('--pmc-version', help='Choose a PMC cloud version when more than one is distributed')
    ap.add_argument('--supplements', action='store_true', help='Discover and fetch verified publisher and PMC supplementary files')
    ap.add_argument('--supplements-only', action='store_true', help='Only update supplements and metadata in an existing article directory')
    args = ap.parse_args()

    if args.pubmed_query:
        if args.target or args.limit is None or not 1 <= args.limit <= 10000:
            ap.error('--pubmed-query requires --limit 1..10000 and no positional target')
        print(json.dumps(pubmed_search(args.pubmed_query, args.limit), ensure_ascii=False, indent=1))
        return 0
    if not args.target or args.limit is not None:
        ap.error('provide an article target; --limit applies only to --pubmed-query')

    t = parse_target(args.target)
    if args.supplements_only:
        if t['kind'] != 'dir' or not any((t['path'] / n).is_file() for n in ('paper.pdf', 'paper.xml')):
            ap.error('--supplements-only requires an existing article directory with paper.pdf or paper.xml')
        dest = t['path']
        meta = json.loads((dest / 'meta.json').read_text()) if (dest / 'meta.json').exists() else {}
        if args.pmc_version and meta.get('pmc_version') and args.pmc_version != str(meta['pmc_version']):
            meta['last_attempt'] = {'status': 'failed', 'error': 'Cached PMC version differs; use its existing version or a separate destination', 'target': args.target}
            save_meta(dest, meta)
            log(meta['last_attempt']['error'])
            return 1
        article = jats_article((dest / 'paper.xml').read_bytes()) if (dest / 'paper.xml').exists() else None
        manifest = None
        if meta.get('pmcid'):
            try:
                manifest = pmc_manifest(meta['pmcid'], args.pmc_version or meta.get('pmc_version'))
                if manifest:
                    meta['pmc_version'] = str(manifest['version'])
                    meta.setdefault('materials', {}).setdefault('supplements', {}).pop('pmc_error', None)
            except Exception as e:
                meta.setdefault('materials', {}).setdefault('supplements', {})['pmc_error'] = str(e)
        state = acquire_supplements(dest, meta, article, manifest, download=True)
        save_meta(dest, meta)
        log(json.dumps(state, ensure_ascii=False, indent=1))
        return 0 if state['status'] == 'available' else 2
    cached_meta = {}
    if t['kind'] == 'dir' and (t['path'] / 'meta.json').exists():
        cached_meta = json.loads((t['path'] / 'meta.json').read_text(encoding='utf-8'))
    if t['kind'] in ('pubmed', 'pmc', 'doi', 'preprint') or (
            t['kind'] == 'dir' and ((t['path'] / 'paper.xml').exists() or
                cached_meta.get('source') in ('pubmed', 'pmc', 'crossref', 'biorxiv', 'medrxiv') or
                cached_meta.get('pmid') or cached_meta.get('pmcid'))):
        return fetch_biomedical(t, args)
    aid = t.get("arxiv")
    dest = t["path"] if t["kind"] == "dir" else None
    if dest and not aid:
        mj = dest / "meta.json"
        if mj.exists():
            cached = json.loads(mj.read_text(encoding="utf-8"))
            aid = cached.get('arxiv_id')
            if aid:
                aid += cached.get('version', '')
        elif (dest / "paper.pdf").exists():
            aid = arxiv_id_from_pdf(dest / "paper.pdf")

    tmp_pdf = None
    if t["kind"] in ("url-pdf", "local-pdf"):
        if t["kind"] == "url-pdf":
            data, _ = http_get(t["url"], timeout=180)
            if data[:4] != b"%PDF":
                sys.exit("  ✗ 这个链接下下来不是 PDF（可能是网页）。给 PDF 直链，或者先手动下载再传本地路径。")
            import tempfile
            tmp_pdf = Path(tempfile.mkstemp(suffix=".pdf")[1])
            tmp_pdf.write_bytes(data)
        else:
            tmp_pdf = t["path"]
        aid = aid or arxiv_id_from_pdf(tmp_pdf)  # 改过名的 arXiv PDF 第一页上有水印，照样能拉源码

    meta_dest = dest or (Path(args.dest).expanduser().resolve() if args.dest else None)
    meta = json.loads((meta_dest / 'meta.json').read_text(encoding='utf-8')) if meta_dest and (meta_dest / 'meta.json').exists() else {}
    incoming = dict(arxiv_meta(aid), arxiv_id=re.sub(r'v\d+$', '', aid)) if aid else {}
    if aid and re.search(r'v\d+$', aid):
        incoming['version'] = re.search(r'v\d+$', aid).group(0)
    try:
        check_cached_identity(meta, incoming, dest or (Path(args.dest).expanduser().resolve() if args.dest else None))
    except ValueError as e:
        cached_dest = dest or Path(args.dest).expanduser().resolve()
        previous = json.loads((cached_dest / 'meta.json').read_text(encoding='utf-8'))
        previous['last_attempt'] = {'status': 'failed', 'error': str(e), 'target': args.target}
        save_meta(cached_dest, previous)
        log(str(e))
        return 1
    meta.update(incoming)
    if not meta.get("title"):
        pdf_for_title = tmp_pdf or (dest / "paper.pdf" if dest else None)
        if pdf_for_title and Path(pdf_for_title).exists():
            meta["title"] = title_from_pdf(pdf_for_title)
    if dest is None:
        fallback = aid or (Path(t["path"]).stem if t.get("path") else Path(t.get("url", "paper")).stem)
        dest = pick_dest(args, args.slug or slugify(meta.get("title", ""), fallback))
    dest.mkdir(parents=True, exist_ok=True)
    shown = (re.sub(r'v\d+$', '', aid) + meta.get("version", "")) if aid and meta.get("version") else aid
    head = f"▸ {shown + ' · ' if aid else ''}{meta.get('title', '')}"
    log(head.rstrip(" ·"))
    if meta.get("authors"):
        au = meta["authors"]
        log(f"  {', '.join(au[:4])}{' 等 %d 人' % len(au) if len(au) > 4 else ''} · {meta.get('published', '')}"
            + (f" · {meta['journal_ref']}" if meta.get("journal_ref") else "")
            + (f" · {meta['comment'][:80]}" if meta.get("comment") else ""))
    log(f"  → {dest}")

    # PDF
    pdf = dest / "paper.pdf"
    if not pdf.exists() or pdf.stat().st_size < 1000:
        if tmp_pdf:
            shutil.copy(tmp_pdf, pdf)
            if t["kind"] == "url-pdf":
                Path(tmp_pdf).unlink(missing_ok=True)
        elif aid:
            data, _ = http_get([f"https://arxiv.org/pdf/{aid}", f"https://export.arxiv.org/pdf/{aid}"], timeout=240)
            if data[:4] != b"%PDF":
                sys.exit("  ✗ arXiv 返回的不是 PDF，稍后再试")
            pdf.write_bytes(data)
        else:
            sys.exit(f"  ✗ {dest} 里没有 paper.pdf")
    try:
        import fitz  # noqa: F401
    except ImportError:
        sys.exit("  ✗ 需要 PyMuPDF：pip install pymupdf（裁图、抽文本都靠它）")
    if args.supplements:
        acquire_supplements(dest, meta, download=True)
    if args.download_only:
        with fitz.open(str(pdf)) as document:
            meta['pdf_pages'] = len(document)
        meta.update(source='arxiv' if aid else meta.get('source') or 'local-pdf' if t['kind'] in ('dir', 'local-pdf') else 'pdf-url',
                    fetched=datetime.date.today().isoformat())
        meta['materials'] = dict(meta.get('materials', {}),
            metadata={'status': 'available' if meta.get('title') else 'not_available'},
            abstract={'status': 'available' if meta.get('abstract') else 'not_available'},
            pdf={'status': 'available', 'file': 'paper.pdf'},
            fulltext={'status': 'available', 'formats': ['pdf']})
        save_meta(dest, meta)
        log('Original PDF and metadata saved. Intensive reading starts only when explicitly requested.')
        return 2 if args.supplements and meta['materials']['supplements']['status'] != 'available' else 0
    npages = build_txt(dest)
    log(f"  ✓ paper.pdf（{npages} 页）+ paper.txt")

    # LaTeX
    src = dest / "source"
    tex_info = None
    source_state = 'available' if src.is_dir() and any(src.rglob('*.tex')) else 'not_checked' if not aid else 'not_available'
    if aid and not (src.is_dir() and any(src.rglob("*.tex"))):
        kind = fetch_source(aid, src)
        source_state = 'available' if kind in ('tar', 'single-tex') else kind
        if kind == "pdf-only":
            log("  · arXiv 上没有 LaTeX 源码（作者只传了 PDF）：公式和表格只能看 PDF")
    if src.is_dir() and any(src.rglob("*.tex")):
        tex_info = build_tex(dest)
        if tex_info:
            log(f"  ✓ LaTeX 源码 → source/，拍平成 paper.tex（{tex_info['lines']} 行，主文件 {tex_info['main']}，"
                f"正文用到的自定义宏 {tex_info['macros']} 个）")
    if not meta.get("title"):
        meta.setdefault("title", "")
    build_outline(dest, meta)

    # 链接 + 代码
    hf = hf_paper(aid) if aid else {}
    links = collect_links(dest, meta, hf)
    code = [c for c in links.get("code", []) if c["score"] >= 3]
    official, official_from = (code[0]["url"], "、".join(code[0]["from"])) if code else (None, None)
    if args.code:
        official, official_from = args.code.rstrip("/").removesuffix(".git"), " --code 手动指定"
    old_meta = dest / "meta.json"
    if not official and old_meta.exists():  # 上一轮手动指定过 / 克隆过，重跑别丢
        prev = json.loads(old_meta.read_text(encoding="utf-8"))
        official, official_from = prev.get("official_code"), prev.get("official_code_from")
    if not official:
        for p in links.get("project", [])[:2]:
            found = code_from_project_page(p["url"])
            if found:
                official, official_from = found, f"项目页 {p['url']}"
                links.setdefault("code", []).insert(0, {"url": found, "score": 3, "from": ["项目页"]})
                break
    code_dir, clone_state = None, None
    if official:
        repo = official.rstrip("/").split("/")[-1]
        if args.code_dir:
            code_dir = Path(os.path.expanduser(args.code_dir)).resolve()
        elif Path("复现").is_dir():
            code_dir = (Path("复现") / repo.lower() / "code").resolve()
        else:
            code_dir = dest / "code"
        if not args.no_clone:
            clone_state = clone(official, code_dir)
        elif code_dir.exists() and any(code_dir.iterdir()):
            clone_state = "exists"

    meta.update({
        "pdf_pages": npages, "has_latex": bool(tex_info), "main_tex": tex_info["main"] if tex_info else None,
        "links": links, "official_code": official, "official_code_from": official_from,
        "code_dir": str(code_dir) if code_dir and clone_state in ("ok", "exists") else None,
        "fetched": datetime.date.today().isoformat(),
    })
    meta['materials'] = dict(meta.get('materials', {}),
        abstract={'status': 'available' if meta.get('abstract') else 'not_available'},
        pdf={'status': 'available', 'file': 'paper.pdf'},
        fulltext={'status': 'available', 'formats': ['pdf']},
        latex={'status': source_state} if aid or tex_info else meta.get('materials', {}).get('latex', {'status': 'not_checked'}))
    if aid and not meta.get("arxiv_id"):
        meta["arxiv_id"] = aid
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    # 图
    import extract_figures
    items = extract_figures.extract(dest, dpi=args.dpi, quiet=True)
    nfig = sum(1 for it in items if it["kind"] == "figure")
    ntab = sum(1 for it in items if it["kind"] == "table" and it.get("file"))
    nfull = sum(1 for it in items if it.get("mode") == "整页")
    fd = extract_figures.figures_dir(dest).name
    log(f"  ✓ 图 {nfig} 张、表 {ntab} 张 → {fd}/（整页兜底 {nfull} 张）· 全部裁剪拼在 {fd}/_contact.png")
    if fd != "figures":
        log("    （figures/ 里已经有你自己整理的图，自动裁的放进了 figures-auto/，原来的没动）")
    log(f"  ✓ outline.md · meta.json · {fd}/index.md")

    if official:
        state = {"ok": f"已浅克隆到 {code_dir}", "exists": f"已在 {code_dir}", None: "没克隆（--no-clone）"}.get(
            clone_state, f"克隆没成功（{clone_state}），可以手动 git clone --depth 1")
        log(f"  代码：{official}（来自{official_from}）→ {state}")
    else:
        log("  代码：论文、arXiv 备注、HF Papers、项目页里都没找到官方仓库 —— 可以搜「<方法名> github」再确认，或者就不对照代码")
    for p in links.get("project", [])[:2]:
        log(f"  项目页：{p['url']}")
    log('Materials saved. Intensive reading starts only when explicitly requested.')
    return 2 if args.supplements and meta['materials']['supplements']['status'] != 'available' else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
