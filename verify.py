"""Offline acceptance: tests, resumable demo, original-project integrity."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parent
ORIGINAL=Path('D:/codex/qwen-vl')
ENV={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'}


def run(arguments):
    result=subprocess.run([sys.executable,*arguments],cwd=ROOT,env=ENV,capture_output=True,text=True,encoding='utf-8')
    print(result.stdout,flush=True)
    if result.returncode:
        print(result.stderr,flush=True)
        raise RuntimeError('verification command failed: '+str(arguments))
    return result.stdout


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):
            result.update(block)
    return result.hexdigest()


def main():
    evidence=ROOT/'verification'
    tests=run(['-m','pytest','tests','-q','--basetemp='+str(ROOT/'.test_tmp')])
    (evidence/'pytest.txt').write_text(tests,encoding='utf-8')
    args=['-m','agentized_workflow.cli','--input-dir',str(ROOT/'examples/images'),
        '--metadata-csv',str(ROOT/'examples/metadata.csv'),'--fixture',str(ROOT/'examples/responses.json'),
        '--run-dir',str(ROOT/'runs/demo')]
    first=json.loads(run(args))
    before=(ROOT/'runs/demo/audit.json').read_bytes()
    second=json.loads(run(args))
    assert first['done']==2 and first['needs_review']==1
    assert second==first and (ROOT/'runs/demo/audit.json').read_bytes()==before
    audit=json.loads(before)
    intents=[e for e in audit if e['event']=='localization_intent']
    assert len(intents)==8
    review=json.loads((ROOT/'runs/demo/needs_review.json').read_text(encoding='utf-8'))
    assert len(review)==1 and len(review[0]['attempts'])==3
    print('Demo verified: 2 done, 1 needs_review, 8 localization intents; resume added zero events.',flush=True)
    baseline=json.loads((evidence/'original_sha256.json').read_text(encoding='utf-8'))
    current={str(p.relative_to(ORIGINAL)):p for p in ORIGINAL.rglob('*') if p.is_file()}
    changed=[]
    for index,(name,expected) in enumerate(baseline.items(),1):
        if name in current and digest(current[name])!=expected:
            changed.append(name)
        if index%1000==0: print(f'Original integrity checked {index}/{len(baseline)}',flush=True)
    report={'time_utc':datetime.now(timezone.utc).isoformat(),'baseline_files':len(baseline),
        'changed':changed,'missing':sorted(set(baseline)-set(current)),
        'added':sorted(set(current)-set(baseline)),
        'demo':first,'resume_added_events':0,'localization_intents':len(intents),
        'tests':tests.strip()}
    (evidence/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    assert not report['changed'] and not report['missing'] and not report['added'],report
    print('Original project unchanged: '+str(len(baseline))+' file hashes match, zero additions/deletions.',flush=True)

if __name__=='__main__': main()
