"""Export a new, inspected Desktop history without importing historical creation code.

Run from any directory: python scripts/export-source.py <new-empty-destination>
The original repository and its history are never rewritten.
"""
import hashlib
import json
from pathlib import Path
import re
import string
import subprocess
import sys

source = Path(__file__).resolve().parents[1]
destination = Path(sys.argv[1]).resolve()
destination.mkdir(parents=True, exist_ok=False)
paths = set(subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).decode().split('\0'))
excluded = re.compile(r'(^|/)(data|deps|node_modules|build|dist|\.venv|\.git)(/|$)|(^|/)(settings|face|credentials|secrets).*\.json$|rig-build\.js$', re.I)
secrets = re.compile(rb'AIza[0-9A-Za-z_-]{35}|(?:sk-proj-|sk-ant-)[A-Za-z0-9_-]{25,}|gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
private = re.compile(rb'function\s+(?:buildRig|fitShape|prepareTransfer|fitKeypoints)\b')
manifest = {}
for name in sorted(paths):
    if not name or excluded.search(name):
        continue
    path = source / name
    if not path.is_file():
        continue
    data = path.read_bytes()
    for match in secrets.finditer(data):
        # The redaction test deliberately uses alphabet+digits, not a real GitHub credential.
        fixture = name == 'tests/test_harness_tools.py' and match[0][4:] in [(letters + numbers).encode() for letters, numbers in [(string.ascii_lowercase, string.digits), (string.ascii_uppercase, string.digits), (string.digits, string.ascii_lowercase), (string.digits, string.ascii_uppercase)]]
        if not fixture:
            raise SystemExit('Possible secret: ' + name)
    if path.suffix in ('.js', '.mjs', '.py') and private.search(data):
        raise SystemExit('Private creation function: ' + name)
    target = destination / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    manifest[name] = hashlib.sha256(data).hexdigest()
subprocess.run(['git', 'init', '-q', '-b', 'main', str(destination)], check=True)
subprocess.run(['git', '-C', str(destination), 'add', '--all'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(['git', '-C', str(destination), '-c', 'user.name=Wireface', '-c', 'user.email=build@wireface.dev',
                'commit', '-qm', 'Wireface Desktop 0.1.3 source release'], check=True)
manifest_path = destination.parent / 'desktop-source-manifest.json'
manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
subprocess.run(['git', '-C', str(destination), 'archive', '--format=zip', '--prefix=wireface-desktop-0.1.3/',
                '-o', str(source / 'dist/wireface-desktop-0.1.3-source.zip'), 'HEAD'], check=True)
print(f'Exported {len(manifest)} files, one clean commit; manifest: {manifest_path}')
