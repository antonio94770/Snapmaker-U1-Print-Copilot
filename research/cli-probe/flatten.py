import json, sys, os, glob
root, kind, name, out = sys.argv[1:5]
idx = {}
for p in glob.glob(os.path.join(root, kind, '*.json')):
    try: d = json.load(open(p, encoding='utf-8'))
    except Exception: continue
    if 'name' in d: idx[d['name']] = d
def resolve(n):
    d = idx[n]; base = resolve(d['inherits']) if d.get('inherits') else {}
    base.update(d); return base
d = resolve(name); d.pop('inherits', None); d['from'] = 'User'
json.dump(d, open(out, 'w', encoding='utf-8'), indent=1)
print(out, len(d))
