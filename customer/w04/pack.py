import zipfile, pathlib
z = zipfile.ZipFile('w04-submit.zip', 'w', zipfile.ZIP_DEFLATED)
n = 0
for d in ('customer/w04', 'docs/w04', 'artifacts/w04'):
    for p in sorted(pathlib.Path(d).rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts:
            z.write(p); n += 1
z.close()
print('files', n)