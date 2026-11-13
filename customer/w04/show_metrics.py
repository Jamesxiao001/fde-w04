import json
from pathlib import Path
r = json.loads(Path('artifacts/w04/metrics_run.json').read_text(encoding='utf-8'))
for k, v in r.items():
    print(k, v.get('units'), v.get('rows'))