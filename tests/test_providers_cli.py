import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from PIL import Image
from agentized_workflow.metadata import load_tasks
from agentized_workflow.models import VisionRequest
from agentized_workflow.providers import HttpVision, ChatPlanner, HttpChat


def test_vision_requests_are_stateless_and_schema_bounded(tmp_path):
    Image.new('RGB',(100,80)).save(tmp_path/'a.png')
    csv=tmp_path/'metadata.csv'; csv.write_text('source_image,species\na.png,可信名称\n',encoding='utf-8')
    spec=load_tasks(tmp_path,csv)[0]
    payloads=[]
    def transport(payload):
        payloads.append(payload)
        return '{"boxes":[{"bbox":[1,1,10,10]}]}'
    vision=HttpVision(transport,model='vision-test')
    for number in (1,2):
        result=vision.localize(VisionRequest(task=spec,request_id=str(number),pass_number=number,
              route='partially_visible',max_targets=5))
        assert result['boxes'][0]['bbox']==[1,1,10,10]
    assert payloads[0]==payloads[1]
    assert len(payloads[0]['messages'])==2
    assert [m['role'] for m in payloads[0]['messages']]==['system','user']
    assert '可信名称' in json.dumps(payloads[0],ensure_ascii=False)
    assert 'species' not in payloads[0]['response_format']['json_schema']['schema']['properties']


def test_planner_has_no_tool_or_vision_access():
    payloads=[]
    def transport(payload): payloads.append(payload); return '{"action":"needs_review"}'
    planner=ChatPlanner(transport,model='planner-test')
    assert json.loads(planner({'remaining_localizations':1}))['action']=='needs_review'
    payload=payloads[0]
    assert 'tools' not in payload
    assert payload['response_format']['json_schema']['schema']['properties']['action']['enum']==['retry_once','needs_review']


def test_http_failure_is_not_retried(monkeypatch):
    calls=[]
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise TimeoutError('offline')
    monkeypatch.setenv('TEST_AGENT_KEY','secret')
    client=HttpChat('https://example.invalid/v1',api_key_env='TEST_AGENT_KEY')
    monkeypatch.setattr(client,'opener',Opener())
    with pytest.raises(TimeoutError): client({'messages':[]})
    assert len(calls)==1


def test_insecure_endpoint_rejected(monkeypatch):
    monkeypatch.setenv('TEST_AGENT_KEY','secret')
    with pytest.raises(ValueError): HttpChat('http://example.invalid',api_key_env='TEST_AGENT_KEY')


def test_cli_offline_and_resume(tmp_path):
    Image.new('RGB',(100,80)).save(tmp_path/'a.png')
    csv=tmp_path/'metadata.csv'; csv.write_text('source_image,species\na.png,可信名称\n',encoding='utf-8')
    fixture=tmp_path/'fixture.json'
    fixture.write_text(json.dumps({'a.png': {'visibility':{'route':'whole_or_mostly_visible'},
        'localizations':[{'boxes':[{'bbox':[1,1,10,10]}]}]*2}}),encoding='utf-8')
    project=Path(__file__).resolve().parents[1]
    command=[sys.executable,'-m','agentized_workflow.cli','--input-dir',str(tmp_path),
        '--metadata-csv',str(csv),'--fixture',str(fixture),'--run-dir',str(tmp_path/'run')]
    env={**os.environ,'PYTHONPATH':str(project/'src'),'PYTHONDONTWRITEBYTECODE':'1'}
    first=subprocess.run(command,cwd=project,env=env,capture_output=True,text=True,encoding='utf-8')
    assert first.returncode==0, first.stderr
    audit=(tmp_path/'run/audit.json').read_bytes()
    old_mtime_ns=1_000_000_000
    os.utime(tmp_path/'run/audit.json',ns=(old_mtime_ns,old_mtime_ns))
    second=subprocess.run(command,cwd=project,env=env,capture_output=True,text=True,encoding='utf-8')
    assert second.returncode==0, second.stderr
    assert (tmp_path/'run/audit.json').read_bytes()==audit
    assert (tmp_path/'run/audit.json').stat().st_mtime_ns==old_mtime_ns
    assert json.loads(second.stdout)['done']==1
