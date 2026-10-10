import hashlib
import importlib.util
import json
from html.parser import HTMLParser
from pathlib import Path
import unittest
import re
import xml.etree.ElementTree as ET

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('frontend_tool',ROOT/'tools/frontend.py')
tool=importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class HTMLInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids=[]; self.references=[]; self.labels=[]; self.components=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if attrs.get('id'): self.ids.append(attrs['id'])
        for prop in ('aria-labelledby','aria-describedby','aria-controls','for','data-dialog-open'):
            if attrs.get(prop): self.references.extend(attrs[prop].split())
        if attrs.get('data-component'): self.components.append(attrs['data-component'])


class FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries=json.loads((ROOT/'frontend/components.manifest.json').read_text(encoding='utf-8'))['components']
        cls.env=Environment(loader=FileSystemLoader(ROOT/'templates'),autoescape=select_autoescape(['html']))
        cls.html=(ROOT/'output/ui-catalog/index.html').read_text(encoding='utf-8')

    def test_manifest_has_every_snapshot_entry(self):
        self.assertEqual({c['id'] for c in self.entries},{c['id'] for c in tool.read('components.json')})
        self.assertEqual(sum(len(c['variants']) for c in self.entries),520)
        self.assertEqual(sum(not c['implementation'] for c in self.entries),8)
        self.assertEqual(sum(c['visual_review']=='reference-captured' for c in self.entries),156)

    def test_every_web_family_rendered(self):
        parser=HTMLInventory();parser.feed(self.html)
        self.assertEqual(set(parser.components),{c['name'] for c in self.entries if c['implementation']})
        self.assertNotIn('Família sem implementação',self.html)

    def test_unique_ids_and_accessible_references(self):
        parser=HTMLInventory();parser.feed(self.html)
        self.assertEqual(len(parser.ids),len(set(parser.ids)))
        self.assertFalse(set(parser.references)-set(parser.ids))

    def test_text_and_attribute_escaping(self):
        for c in self.entries:
            if not c['implementation']: continue
            path,macro=c['implementation'].split('#')
            fn=getattr(self.env.get_template(path.removeprefix('templates/')).module,macro)
            output=str(fn('safe',label='<script>alert(1)</script>',value='" onfocus="alert(1)',items=[{'label':'<img src=x onerror=alert(1)>','value':20}],helper='<iframe>'))
            self.assertNotIn('<script>',output,c['name'])
            self.assertNotIn('<img src=x',output,c['name'])
            self.assertNotIn('<iframe>',output,c['name'])
            self.assertNotIn(' onfocus="alert',output,c['name'])

    def test_sprite_is_valid_and_contains_rendered_icons(self):
        sprite = ET.parse(ROOT/'static/ui/phosphor.svg').getroot()
        symbols = {s.attrib['id'] for s in sprite.findall('{http://www.w3.org/2000/svg}symbol')}
        used = set(re.findall(r'phosphor\.svg#([^"\s]+)', self.html))
        self.assertTrue(used)
        self.assertFalse(used - symbols)

    def test_product_and_ai_unchanged(self):
        for path,sha in json.loads((ROOT/'frontend/protected-files.json').read_text()).items():
            self.assertEqual(hashlib.sha256((ROOT/path).read_bytes()).hexdigest(),sha,path)

    def test_no_remote_runtime_assets_or_api_hooks(self):
        self.assertNotIn('figma.com/api/mcp/asset',self.html)
        js=(ROOT/'static/ui/library.js').read_text()
        for forbidden in ('fetch(', 'XMLHttpRequest', 'api.anthropic.com', 'api.typesafe.ai'):
            self.assertNotIn(forbidden,js)

    def test_aliases_all_resolve_and_themes_differ(self):
        variables=tool.read('variables.json')
        ids={v['id'] for v in variables}
        for variable in variables:
            for value in variable['valuesByMode'].values():
                if isinstance(value,dict) and value.get('type')=='VARIABLE_ALIAS':
                    self.assertIn(value['id'],ids)
        css=(ROOT/'frontend/tokens.generated.css').read_text()
        self.assertIn('[data-theme="light"]',css)
        self.assertIn('[data-theme="dark"]',css)
        self.assertIn('--radius-sm: 2px',css)
        self.assertIn('--radius-xl: 12px',css)

    def test_asset_hashes_match(self):
        for asset in json.loads((ROOT/'frontend/assets.lock.json').read_text())['assets']:
            self.assertEqual(hashlib.sha256((ROOT/asset['path']).read_bytes()).hexdigest(),asset['sha256'])

    def test_loading_preserves_accessible_name_and_prevents_repeat(self):
        render = self.env.get_template('ui/_renderer.html').module.component
        for family in ('Button', 'Icon Button'):
            html = str(render(family, 'busy', label='Salvar solicitação', props={'Loading': True}))
            self.assertIn('Salvar solicitação', html)
            self.assertIn('aria-busy="true"', html)
            self.assertIn('aria-disabled="true"', html)
            self.assertIn('data-busy', html)
            disabled = str(render(family, 'disabled', label='Salvar', disabled=True, props={'Loading': True}))
            self.assertNotIn('aria-busy="true"', disabled)

    def test_metric_direction_does_not_determine_sentiment(self):
        render = self.env.get_template('ui/_renderer.html').module.component
        up = str(render('KPI Card', 'up', props={'Trend': 'Up', 'Sentiment': 'Negative'}))
        down = str(render('KPI Card', 'down', props={'Trend': 'Down', 'Sentiment': 'Positive'}))
        self.assertIn('df-sentiment-negative', up)
        self.assertIn('Aumento · desfavorável', up)
        self.assertIn('df-sentiment-positive', down)
        self.assertIn('Redução · favorável', down)

    def test_danger_toast_offers_recovery(self):
        render = self.env.get_template('ui/_renderer.html').module.component
        html = str(render('Toast', 'failure', props={'Tone': 'Danger'}))
        self.assertIn('Não foi possível salvar.', html)
        self.assertIn('data-action="retry"', html)
        self.assertNotIn('sucesso', html)
