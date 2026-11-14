"""One-time cleanup of retired relations through the normal publish transaction."""
import json
import os
import urllib.request


def run(base):
    def request(path, value=None):
        body=None if value is None else json.dumps(value).encode()
        req=urllib.request.Request(base+path,data=body,headers={'Content-Type':'application/json'},method='GET' if value is None else 'PUT')
        with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
    results=[]
    for domain in request('/ontology/configurations'):
        path='/ontology/configurations/'+domain['domain']
        config=request(path)
        relations=config.get('relations') or []
        def retired(r):
            metadata=r.get('metadata') or {}
            record=metadata.get('relationshipRecord') or {}
            return any(v in {'pending','pending_review','stale','rejected'} for v in [r.get('status'),metadata.get('status'),record.get('status')])
        kept=[r for r in relations if not retired(r)]
        if len(kept)!=len(relations):
            config['relations']=kept
            request(path,config)
        results.append({'domain':domain['domain'],'removed':len(relations)-len(kept),'remaining':len(kept)})
    return results

if __name__=='__main__':
    print(json.dumps(run(os.getenv('SEMORA_API_URL','http://localhost:8000/api/v1')),ensure_ascii=False,indent=2))
