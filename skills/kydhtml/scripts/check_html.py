"""Static checks for a teaching HTML deck; never executes the deck or fetches URLs."""
import argparse
from collections import Counter
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class DeckParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.scripts = []
        self.script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if tag == 'script':
            self.script = {'attrs': attrs, 'text': ''}

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self.script is not None:
            self.script['text'] += data

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            self.scripts.append(self.script)
            self.script = None


def inspect(path, node=None, temp_dir=None):
    source = path.read_text(encoding='utf-8-sig')
    parser = DeckParser()
    parser.feed(source)
    errors, warnings = [], []
    ids = [a['id'] for _, a in parser.elements if a.get('id')]
    duplicates = [key for key, count in Counter(ids).items() if count > 1]
    if duplicates:
        errors.append('Duplicate IDs: ' + ', '.join(duplicates))
    slides = [a for _, a in parser.elements if 'slide' in a.get('class', '').split()]
    slide_ids = {a.get('id') for a in slides if a.get('id')}
    if not slides:
        errors.append('No elements with class="slide" were found.')
    for number, slide in enumerate(slides, 1):
        if not slide.get('data-title'):
            warnings.append(f'Slide {number} has no data-title for the generated table of contents.')
        if not slide.get('id'):
            warnings.append(f'Slide {number} has no stable ID; verify numbered navigation after insertion.')
    payloads = {}
    executable = []
    for index, script in enumerate(parser.scripts):
        attrs, text = script['attrs'], script['text']
        kind = attrs.get('type', '').lower()
        if kind in ('application/json', 'application/ld+json', 'importmap'):
            try:
                payloads[attrs.get('id', f'script-{index}')] = json.loads(text)
            except json.JSONDecodeError as error:
                errors.append(f'Invalid JSON in {attrs.get("id", index)}: {error}')
        elif not attrs.get('src') and kind in ('', 'text/javascript', 'application/javascript', 'module'):
            executable.append((index, kind, text))
    for tag, attrs in parser.elements:
        if 'data-jump' in attrs and attrs['data-jump'] not in slide_ids:
            errors.append('Missing slide for data-jump=' + attrs['data-jump'])
        if 'data-go' in attrs:
            try:
                valid = 1 <= int(attrs['data-go']) <= len(slides)
            except ValueError:
                valid = False
            if not valid:
                errors.append('Numbered slide target is outside the deck: ' + attrs['data-go'])
        for attribute, dataset in (('data-copy', 'copy-data'), ('data-view', 'copy-data'), ('data-lab-copy', 'lab-copy-data'), ('data-lab-view', 'lab-copy-data')):
            if attribute in attrs:
                texts = payloads.get(dataset)
                if not isinstance(texts, dict) or not isinstance(texts.get(attrs[attribute]), str):
                    errors.append(f'Missing copy text: {dataset}/{attrs[attribute]}')
        if tag == 'a':
            href = attrs.get('href', '')
            if not href:
                warnings.append('An anchor has no destination.')
            if href.startswith('#') and href[1:] not in ids and not (href[1:].isdigit() and 1 <= int(href[1:]) <= len(slides)):
                errors.append('Missing fragment target: ' + href)
            if attrs.get('target') == '_blank' and 'noopener' not in attrs.get('rel', '').split():
                warnings.append('New-tab link is missing rel="noopener": ' + href)
        if tag in ('script', 'img', 'iframe', 'video', 'audio', 'source', 'link'):
            url = attrs.get('src') or (attrs.get('href') if tag == 'link' else '') or ''
            if url.startswith(('http://', 'https://', '//')):
                warnings.append(f'External runtime asset ({tag}): {url}')
            elif url and not url.startswith(('data:', '#', 'blob:')):
                warnings.append(f'Separate local asset ({tag}): {url}')
    if re.search(r'@import\s|url\([\s\x22\x27]*https?://', source, re.I):
        warnings.append('Review CSS imports or remote CSS assets for offline use.')
    checked = 0
    if node:
        if temp_dir is not None:
            temp_dir = Path(temp_dir).resolve()
            temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='html-deck-check-', dir=temp_dir) as directory:
            for index, kind, text in executable:
                script_path = Path(directory) / (f'script-{index}' + ('.mjs' if kind == 'module' else '.js'))
                script_path.write_text(text, encoding='utf-8')
                try:
                    result = subprocess.run([node, '--check', str(script_path)], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
                except (OSError, subprocess.TimeoutExpired) as error:
                    errors.append(f'Could not check JavaScript: {error}')
                    break
                checked += 1
                if result.returncode:
                    errors.append(f'JavaScript syntax error in script {index}: {result.stderr.strip()}')
    else:
        warnings.append('JavaScript syntax was not checked; pass --node to enable it.')
    return {'file': str(path.resolve()), 'slides': len(slides), 'javascript_checked': checked,
            'errors': errors, 'warnings': list(dict.fromkeys(warnings)),
            'limits': 'Static analysis only. Visual layout, live copy, keyboard behavior and remote sites require separate verification.'}


def main():
    argument = argparse.ArgumentParser(description=__doc__)
    argument.add_argument('html', type=Path)
    argument.add_argument('--node', help='Node executable path; used only for --check, never to execute the deck.')
    argument.add_argument('--temp-dir', type=Path, help='Writable temporary parent folder, useful in restricted environments.')
    argument.add_argument('--report', type=Path, help='Optional JSON report output.')
    args = argument.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = inspect(args.html, args.node, args.temp_dir)
    except (OSError, UnicodeError) as error:
        argument.exit(2, f'Could not complete checks: {error}\n')
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded+'\n', encoding='utf-8')
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
