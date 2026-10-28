import json, subprocess, sys, os
C = sys.argv[1]
exe = r"C:\Program Files\Snapmaker_Orca\snapmaker-orca.exe"
base = json.load(open(os.path.join(C,'pmin.json'),encoding='utf-8'))
full = json.load(open(os.path.join(C,'p.json'),encoding='utf-8'))
extra = [k for k in full if k not in base]
f = os.path.join(C,'f.json')
def crashes(keys):
    d = dict(base); d.update({k: full[k] for k in keys})
    pt = os.path.join(C,'pb.json'); json.dump(d, open(pt,'w',encoding='utf-8'))
    r = subprocess.run([exe,'--slice','0','--load-settings',os.path.join(C,'m_s.json')+';'+pt,'--load-filaments',';'.join([f]*4),'--outputdir',os.path.join(C,'out_b'),os.path.join(C,'cube.stl')],capture_output=True,timeout=180)
    return r.returncode not in (0,)
bad = []
def search(keys):
    if not crashes(keys): return
    if len(keys)==1: bad.append(keys[0]); return
    h=len(keys)//2; a,b=keys[:h],keys[h:]
    ca,cb = crashes(a), crashes(b)
    if ca: search(a)
    if cb: search(b)
    if not ca and not cb: bad.append(('combo',keys))
search(extra)
print('crashing keys:', bad)
for k in bad:
    if isinstance(k,str): print(k, '=', json.dumps(full[k])[:200])
