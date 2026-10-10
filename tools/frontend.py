"""Biblioteca isolada: install, build, watch, catalog, serve e check. Não importa app.py."""
import argparse
import hashlib
import http.server
import json
import platform
import re
import shutil
import subprocess
import urllib.request
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
VERSION = '4.3.3'
FIGMA = ROOT / 'frontend/figma'
OUT = ROOT / 'output/ui-catalog'


def read(name):
    return json.loads((FIGMA / name).read_text(encoding='utf-8'))


def slug(value):
    value = unicodedata.normalize('NFKD', value.replace('․','.')).encode('ascii','ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')


def executable():
    return ROOT / '.tools' / ('tailwindcss.exe' if platform.system() == 'Windows' else 'tailwindcss')


def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={'User-Agent': 'DataForge-frontend'})
    with urllib.request.urlopen(req, timeout=60) as response:
        dest.write_bytes(response.read())
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def install():
    lock_path = ROOT / 'frontend/assets.lock.json'
    previous = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    os_name = {'Windows': 'windows', 'Linux': 'linux', 'Darwin': 'macos'}[platform.system()]
    arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'
    asset = f'tailwindcss-{os_name}-{arch}' + ('.exe' if os_name == 'windows' else '')
    cli_url = f'https://github.com/tailwindlabs/tailwindcss/releases/download/v{VERSION}/{asset}'
    known = previous.get('compilers', {}).get(asset, previous.get('tailwind', {}))
    if executable().exists() and known.get('url') == cli_url:
        sha = hashlib.sha256(executable().read_bytes()).hexdigest()
    else:
        sha = download(cli_url, executable())
    if known.get('url') == cli_url and known.get('sha256') != sha:
        raise RuntimeError('Checksum do compilador difere do lock')
    executable().chmod(0o755)
    font_base = 'https://raw.githubusercontent.com/vercel/geist-font/10dc7658f13c38a474cde201bb09a4617267545b/'
    jobs = [(font_base + 'fonts/Geist/webfonts/' + f'Geist-{weight}.woff2', ROOT / 'static/ui/fonts' / f'Geist-{weight}.woff2')
            for weight in ('Regular', 'SemiBold')]
    jobs += [(font_base + 'fonts/GeistMono/webfonts/GeistMono-Regular.woff2', ROOT / 'static/ui/fonts/GeistMono-Regular.woff2'),
             (font_base + 'OFL.txt', ROOT / 'static/ui/fonts/OFL.txt')]
    icon_names = ('check', 'x', 'caret-down', 'caret-right', 'dots-three', 'magnifying-glass', 'plus',
                  'paper-plane-right', 'bell', 'camera', 'map-pin', 'star', 'user', 'warning', 'spinner',
                  'clock', 'arrow-right', 'arrow-down', 'minus', 'house', 'chat-circle', 'gear', 'trophy', 'play', 'pause',
                  'image', 'download-simple', 'chart-bar', 'shield-check', 'users', 'sparkle', 'copy',
                  'share-network', 'thumbs-up', 'thumbs-down', 'speaker-high', 'arrows-clockwise',
                  'list-checks', 'file-text', 'info', 'lock-simple', 'coins', 'fire', 'calendar-blank')
    source_icons = {name for ref in read('design-context.json') for name in re.findall(r'## Icon/([a-z0-9-]+)',ref.get('reference',''))}
    icon_names = sorted(set(icon_names) | source_icons)
    jobs += [(f'https://raw.githubusercontent.com/phosphor-icons/core/2b75f3ad12b420c9504ef05df8d2564a28f8500e/assets/{"fill" if name.endswith("-fill") else "regular"}/{name}.svg',
              ROOT / 'static/ui/icons' / f'{name}.svg') for name in icon_names]
    jobs += [('https://raw.githubusercontent.com/phosphor-icons/core/2b75f3ad12b420c9504ef05df8d2564a28f8500e/LICENSE', ROOT / 'static/ui/icons/LICENSE')]
    old_assets = {a['path']: a for a in previous.get('assets', [])}
    def install_asset(job):
        url, path = job
        old = old_assets.get(path.relative_to(ROOT).as_posix())
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else download(url, path)
        if old and digest != old['sha256']:
            raise RuntimeError('Checksum do asset difere do lock: '+str(path))
        return digest
    with ThreadPoolExecutor(max_workers=6) as pool:
        hashes = list(pool.map(install_asset, jobs))
    record = {'tailwind': {'version': VERSION, 'url': cli_url, 'sha256': sha},
              'assets': [{'path': str(path.relative_to(ROOT)).replace('\\', '/'), 'url': url, 'sha256': digest}
                         for (url, path), digest in zip(jobs, hashes)]}
    record['compilers'] = previous.get('compilers', {})
    record['compilers'][asset] = record['tailwind']
    lock_path.write_text(json.dumps(record, indent=2), encoding='utf-8')
    print('Tailwind standalone e assets locais instalados.')


def tokens():
    variables = {v['id']: v for v in read('variables.json')}
    collections = {c['id']: c for c in read('collections.json')}
    def value(v, mode, trail=()):
        if v['id'] in trail:
            raise ValueError('Alias circular: ' + v['name'])
        c = collections[v['collectionId']]
        m = next((m['modeId'] for m in c['modes'] if m['name'].lower() == mode), c['defaultModeId'])
        val = v['valuesByMode'][m]
        if isinstance(val, dict) and val.get('type') == 'VARIABLE_ALIAS':
            return value(variables[val['id']], mode, trail + (v['id'],))
        if v['type'] == 'COLOR':
            return f"rgb({round(val['r']*255)} {round(val['g']*255)} {round(val['b']*255)} / {val.get('a',1):.5g})"
        if v['type'] == 'FLOAT':
            return f'{val:g}px'
        return json.dumps(val, ensure_ascii=False)
    css = ['/* Gerado do snapshot Figma; não editar manualmente. */']
    for mode in ('light', 'dark'):
        css.append(f'.df-ui[data-theme="{mode}"], [data-theme="{mode}"] .df-ui {{')
        for v in variables.values():
            c = collections[v['collectionId']]
            if c['name'] == 'Device Sizes':
                continue
            css.append(f"  --df-{slug(c['name'])}-{slug(v['name'])}: {value(v,mode)};")
        css.append(f'  color-scheme: {mode};\n}}')
    # Icon Color tem múltiplos contextos, e não apenas Light/Dark.
    for c in collections.values():
        if c['name'] == 'Icon Color':
            for mode in c['modes']:
                v = variables[c['variableIds'][0]]
                raw = v['valuesByMode'][mode['modeId']]
                target = variables[raw['id']]
                css.append(f'.df-ui [data-icon-color="{slug(mode["name"])}"] {{ color: var(--df-{slug(collections[target["collectionId"]]["name"])}-{slug(target["name"])}); }}')
    css.append('@theme inline {\n  --font-sans: "Geist", sans-serif;\n  --font-mono: "Geist Mono", monospace;')
    for v in variables.values():
        c = collections[v['collectionId']]
        if c['name'] == 'Brand Semantic':
            css.append(f'  --color-{slug(v["name"])}: var(--df-brand-semantic-{slug(v["name"])});')
        elif c['name'] == 'Tailwind Dimensions':
            category, name = v['name'].split('/')
            if category == 'Border Radius':
                name = name.replace('rounded-', '').replace('rounded', 'DEFAULT')
                css.append(f'  --radius{"" if name=="DEFAULT" else "-"+name}: {value(v,"light")};')
            elif category == 'Spacing' and name=='1':
                css.append(f'  --spacing: {value(v,"light")};')
            elif category == 'Screens':
                css.append(f'  --breakpoint-{name}: {value(v,"light")};')
    css.append('}')
    # Escala completa de texto Geist: métricas originais com unidade explícita.
    for style in read('text-styles.json'):
        lh = style['lineHeight']
        line = 'normal' if lh['unit'] == 'AUTO' else str(lh['value']/100) if lh['unit'] == 'PERCENT' else f"{lh['value']}px"
        ls = style.get('letterSpacing', {'unit':'PIXELS','value':0})
        spacing = f"{ls['value']/100:g}em" if ls['unit']=='PERCENT' else f"{ls['value']:g}px"
        css.append(f'.df-ui .df-type-{slug(style["name"])} {{ font-family: "{style["font"]["family"]}"; font-size: {style["size"]}px; font-weight: {600 if "SemiBold" in style["name"] else 400}; line-height: {line}; letter-spacing: {spacing}; }}')
    (ROOT / 'frontend/tokens.generated.css').write_text('\n'.join(css)+'\n', encoding='utf-8')


EXCLUDED = {'Date Wheel', 'Time Wheel', 'Date Dialog', 'Status Bar', 'Home Indicator', 'Android Navigation', 'Browser Chrome', 'Screen'}


def manifest():
    components = read('components.json')
    groups = {}
    references = {r['nodeId']: r for r in read('design-context.json')}
    additional_groups = {'Rating':'27 · Avaliação','Board Column':'28 · Quadro e prioridade', '.Board Card':'28 · Quadro e prioridade',
        'Priority Chip':'28 · Quadro e prioridade','Priority Row':'28 · Quadro e prioridade','Ticket Header':'28 · Quadro e prioridade',
        'Text Area':'03 · Formulários','JSON Editor':'03 · Formulários','Error State':'15 · Feedback e estados',
        'Push Permission':'15 · Feedback e estados','AI Info Panel':'06 · Conversa e assistente','Skeleton Column':'15 · Feedback e estados'}
    for c in components:
        c['slug'] = slug(c['name'])
        section = c['section'] or additional_groups[c['name']]
        c['section'] = section
        group = slug(section)
        c['classification'] = 'platform-reference' if c['name'] in EXCLUDED else 'internal' if c['name'].startswith('.') else 'public'
        c['implementation'] = None if c['name'] in EXCLUDED else f'templates/ui/{group}.html#{c["slug"].replace("-","_")}'
        c['exclusion_reason'] = 'Representação nativa ou moldura de dispositivo; não renderizar na aplicação web.' if c['name'] in EXCLUDED else None
        ref = references.get(c['id'], {})
        c['visual_review'] = 'reference-captured' if ref.get('reference') else 'pending-mcp-limit' if c['implementation'] else 'native-reference'
        c['web_adaptation'] = c['name'] in ('Switch','Navigation Bar','Tab Bar','Action Sheet','Date Picker','Time Field','Audio Player')
        c['native_variants_excluded'] = [v['id'] for v in c['variants'] if (v.get('properties') or {}).get('Platform') not in (None,'Web') and any((w.get('properties') or {}).get('Platform')=='Web' for w in c['variants'])]
        if c['implementation']:
            groups.setdefault(group, []).append(c)
    dest = ROOT / 'frontend/components.manifest.json'
    dest.write_text(json.dumps({'fileKey':'uJ1MNIY2FcYyJH98nSDCRd','pageId':'2118:12','capturedAt':'2026-10-09','components':components}, ensure_ascii=False,indent=2), encoding='utf-8')
    folder = ROOT / 'templates/ui'
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob('*.html'):
        if old.stem not in groups and old.read_text(encoding='utf-8').startswith('{# Gerado: wrappers públicos;'):
            old.unlink()
    for group, entries in groups.items():
        code = ['{# Gerado: wrappers públicos; comportamento compartilhado em _renderer.html. #}', '{% import "ui/_renderer.html" as r with context %}']
        for c in entries:
            macro = c['slug'].replace('-', '_')
            code.append('{% macro '+macro+"(id, label='Chamado de exemplo', value='', items=[], props={}, disabled=false, error='', helper='', href='#', media='') -%}\n"+
                        "{{ r.component('"+c['name'].replace("'","\\'")+"', id, label, value, items, props, disabled, error, helper, href, media) }}\n{%- endmacro %}")
        (folder / (group+'.html')).write_text('\n'.join(code)+'\n',encoding='utf-8')
    return components


def catalog():
    entries = manifest()
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'),autoescape=select_autoescape(['html']))
    env.filters['slug'] = slug
    env.globals['ui_asset_base'] = 'assets'
    examples = []
    for c in entries:
        if not c['implementation']:
            continue
        path, macro = c['implementation'].split('#')
        fn = getattr(env.get_template(path.replace('templates/', '')).module, macro)
        for index,v in enumerate(c['variants']):
            props = v.get('properties') or {}
            # Somente variante Web em famílias que possuem seleção de plataforma.
            plat = next((val for key,val in props.items() if key.lower() in ('platform','plataforma')),None)
            has_web = any(any(str(val).lower()=='web' for key,val in (variant.get('properties') or {}).items() if key.lower() in ('platform','plataforma')) for variant in c['variants'])
            if plat and has_web and plat.lower() not in ('web','all','todos'):
                continue
            uid = c['slug'] + '-' + str(index)
            demo_items=[{'label':'Novo','value':20,'secondary':12},{'label':'Encaminhado','value':35,'secondary':18},{'label':'Executado','value':25,'secondary':15},{'label':'Concluído','value':20,'secondary':8}]
            if c['name'] in ('Tab Bar','Sidebar Navigation'):
                demo_items=[{'label':label,'value':i} for i,label in enumerate(['Conversa','Notas','Ideias','Metas','Biblioteca','Menu'],1)]
            html = fn(uid,label='Atendimento de TI',value='50' if c['name'] in ('Progress Bar','Progress Ring','Slider') else '',
                      items=demo_items,props=props,
                      disabled='Disabled' in props.values(), error='Confira este campo.' if 'Error' in props.values() else '',helper='Dados fictícios para demonstração.')
            examples.append({'component':c,'variant':v,'html':html,'id':uid,'firstOfFamily':not any(e['component']['id']==c['id'] for e in examples)})
        if c['name'] in ('Button', 'Icon Button'):
            base = c['variants'][0]
            for feature in ('Focus Visible', 'Loading', 'Pressed'):
                props = dict(base['properties'], **{feature: True})
                uid = c['slug'] + '-' + slug(feature)
                example = dict(base, name=base['name'] + ', ' + feature + '=True')
                examples.append({'component':c,'variant':example,'html':fn(uid,label='Salvar',props=props),'id':uid,'firstOfFamily':False})
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'index.html').write_text(env.get_template('ui/catalog.html').render(examples=examples,entries=entries),encoding='utf-8')
    shutil.copytree(ROOT/'static/ui',OUT/'assets',dirs_exist_ok=True)
    print(f'Catálogo: {len(examples)} exemplos; {len(entries)} entradas no manifesto.')


def build(watch=False):
    tokens()
    manifest()
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    sprite = ET.Element('{http://www.w3.org/2000/svg}svg')
    for path in sorted((ROOT/'static/ui/icons').glob('*.svg')):
        original = ET.fromstring(path.read_bytes())
        symbol = ET.SubElement(sprite, '{http://www.w3.org/2000/svg}symbol', id=path.stem, viewBox=original.attrib.get('viewBox','0 0 256 256'))
        symbol.extend(list(original))
    ET.ElementTree(sprite).write(ROOT/'static/ui/phosphor.svg',encoding='utf-8',xml_declaration=True)
    if not executable().exists():
        raise RuntimeError('Execute primeiro: python tools/frontend.py install')
    args = [str(executable()),'-i',str(ROOT/'frontend/input.css'),'-o',str(ROOT/'static/ui/library.css'),'--minify']
    if watch:
        args.append('--watch')
    subprocess.run(args,check=True,cwd=ROOT)
    if not watch:
        catalog()


def check():
    protected = json.loads((ROOT/'frontend/protected-files.json').read_text())
    for path, expected in protected.items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest() == expected, 'Arquivo protegido alterado: '+path
    data = json.loads((ROOT/'frontend/components.manifest.json').read_text())['components']
    assert len(data)==164 and len({c['id'] for c in data})==164
    assert sum(len(c['variants']) for c in data)==520
    assert (ROOT/'static/ui/library.css').stat().st_size>0
    lock=json.loads((ROOT/'frontend/assets.lock.json').read_text())
    for asset in lock['assets']:
        assert hashlib.sha256((ROOT/asset['path']).read_bytes()).hexdigest()==asset['sha256'], asset['path']
    print('Snapshot, assets e arquivos protegidos conferidos.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['install','build','watch','catalog','serve','check'])
    parser.add_argument('--port',type=int,default=8002)
    args=parser.parse_args()
    if args.command=='install': install()
    elif args.command=='build': build()
    elif args.command=='watch': build(True)
    elif args.command=='catalog': catalog()
    elif args.command=='check': check()
    else:
        handler=lambda *a,**kw: http.server.SimpleHTTPRequestHandler(*a,directory=str(OUT),**kw)
        print(f'Catálogo isolado: http://127.0.0.1:{args.port}',flush=True)
        http.server.ThreadingHTTPServer(('127.0.0.1',args.port),handler).serve_forever()
