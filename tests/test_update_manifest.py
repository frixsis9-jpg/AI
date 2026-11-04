import hashlib
import json
from pathlib import Path


def test_update_manifest_has_only_verified_runtime_files_and_no_user_data():
    root=Path(__file__).resolve().parents[1]
    manifest=json.loads((root/'UPDATE_MANIFEST.json').read_text())
    assert manifest['schema']==1
    files=manifest['files']
    paths=[f['path'] for f in files]
    assert len(paths)==len(set(paths))
    for required in ['scanner/__init__.py','scanner/service.py','storage/fsm.py','analysis/entry.py','market_data/snapshot.py','requirements.txt']:
        assert required in paths
    for f in files:
        p=Path(f['path'])
        assert not p.is_absolute() and '..' not in p.parts
        assert not p.name.startswith('.env')
        assert p.name not in ('model_weights.json','signals.db')
        assert hashlib.sha256((root/p).read_bytes()).hexdigest()==f['sha256']
