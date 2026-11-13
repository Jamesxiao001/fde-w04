import json, sys
from pathlib import Path
run = Path(sys.argv[1])
for trace in sorted(p for p in run.iterdir() if p.is_dir()):
    r = json.loads((trace / 'result.json').read_text(encoding='utf-8'))
    print('trace_id:', trace.name)
    print('status  :', r.get('status'), r.get('reason', ''))
    print('question:', r.get('question'))
    print('rows    :', r.get('rows'))
    print('sql     :', (r.get('sql') or '')[:300])
    print('answer  :', r.get('answer'))