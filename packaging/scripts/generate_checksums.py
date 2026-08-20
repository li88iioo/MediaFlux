#!/usr/bin/env python3
"""为发布目录生成稳定排序的 SHA256SUMS。"""
from __future__ import annotations
import argparse, hashlib
from pathlib import Path
from typing import Sequence
EXCLUDED={'SHA256SUMS','SHA256SUMS.sig'}
def generate_checksums(directory:Path,output:Path|None=None)->Path:
 directory=directory.resolve(); output=(output or directory/'SHA256SUMS').resolve(); lines=[]
 for path in sorted((p for p in directory.rglob('*') if p.is_file()),key=lambda p:p.relative_to(directory).as_posix()):
  if path.resolve()==output or path.name in EXCLUDED: continue
  h=hashlib.sha256()
  with path.open('rb') as f:
   for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
  lines.append(f'{h.hexdigest()}  {path.relative_to(directory).as_posix()}')
 output.write_text('\n'.join(lines)+'\n',encoding='utf-8',newline='\n'); return output
def main(argv:Sequence[str]|None=None)->int:
 p=argparse.ArgumentParser(description=__doc__); p.add_argument('directory',type=Path); p.add_argument('--output',type=Path); a=p.parse_args(argv); print(generate_checksums(a.directory,a.output)); return 0
if __name__=='__main__': raise SystemExit(main())
