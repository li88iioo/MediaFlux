#!/usr/bin/env python3
"""从锁定的 Python 依赖清单生成范围明确的 SPDX 2.3 JSON SBOM。"""
from __future__ import annotations
import argparse, hashlib, json, re
from datetime import datetime,timezone
from pathlib import Path
from typing import Sequence
REQ=re.compile(r'^([A-Za-z0-9_.-]+)\s*([<>=!~]{1,2})?\s*([^;\s#]+)?')
def dependencies(files:list[Path])->list[dict[str,str]]:
 result={}
 for file in files:
  for raw in file.read_text(encoding='utf-8').splitlines():
   line=raw.strip()
   if not line or line.startswith('#') or line.startswith('-'): continue
   m=REQ.match(line)
   if m: result[m.group(1).lower()]={'name':m.group(1),'versionInfo':m.group(3) or 'unspecified'}
 return [result[k] for k in sorted(result)]
def generate_sbom(files:list[Path],output:Path,namespace:str)->Path:
 packages=[]
 for index,item in enumerate(dependencies(files),1): packages.append({'SPDXID':f'SPDXRef-Package-{index}','name':item['name'],'versionInfo':item['versionInfo'],'downloadLocation':'NOASSERTION','filesAnalyzed':False,'licenseConcluded':'NOASSERTION','licenseDeclared':'NOASSERTION'})
 payload={'spdxVersion':'SPDX-2.3','dataLicense':'CC0-1.0','SPDXID':'SPDXRef-DOCUMENT','name':'MediaFlux locked Python dependencies','documentNamespace':namespace,'creationInfo':{'created':datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z'),'creators':['Tool: MediaFlux-generate_sbom']},'packages':packages}
 output.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n',encoding='utf-8'); return output
def main(argv:Sequence[str]|None=None)->int:
 p=argparse.ArgumentParser(description=__doc__); p.add_argument('requirements',nargs='+',type=Path); p.add_argument('--output',type=Path,required=True); p.add_argument('--namespace',default='https://mediaflux.invalid/sbom/development'); a=p.parse_args(argv); print(generate_sbom(a.requirements,a.output,a.namespace)); return 0
if __name__=='__main__': raise SystemExit(main())
