"""Read-only source and health verification for the local Docker MVP."""
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.request import urlopen

root=Path(__file__).resolve().parents[2]
files={}
for local,remote in [
    ('services/api/app','/app/app'),
    ('services/agent/semora_agent','/app/semora_agent'),
    ('packages/contracts/python/semora_contracts','/app/packages/contracts/python/semora_contracts'),
]:
    for path in (root/local).rglob('*.py'):
        files[remote+'/'+path.relative_to(root/local).as_posix()]=path
code=('import hashlib,json;from pathlib import Path;files='+repr(list(files))+
      ';print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in files}))')
process=subprocess.run(['docker','exec','-i','semora-api-1','python','-'],
    input=code.encode(),capture_output=True,check=True)
remote=json.loads(process.stdout)
assert all(remote[r]==hashlib.sha256(path.read_bytes()).hexdigest() for r,path in files.items())
for port in (8000,8001):
    with urlopen(f'http://127.0.0.1:{port}/healthz',timeout=20) as response:
        assert response.status==200
containers=json.loads(subprocess.check_output(['docker','inspect','semora-api-1','semora-semantic-planner-1']))
report={'api_source_files_verified':len(files),'healthy':True,
    'images':{container['Name']:container['Image'] for container in containers}}
target=root/'artifacts/prompt-refactor/deployment.json'
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps(report))
