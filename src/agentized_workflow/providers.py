from __future__ import annotations
import base64
import io
import json
import os
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from PIL import Image, ImageOps
from .models import VisionRequest

BOX_SCHEMA = {'type':'object','additionalProperties':False,'required':['coordinate_system','boxes'],
    'properties':{'coordinate_system':{'type':'string','enum':['qwen_0_999']},
      'boxes':{'type':'array','items':{'type':'object','additionalProperties':False,'required':['bbox'],
        'properties':{'bbox':{'type':'array','items':{'type':'number','minimum':0,'maximum':999},'minItems':4,'maxItems':4}}}}}}
VISIBILITY_SCHEMA = {'type':'object','additionalProperties':False,'required':['route'],
    'properties':{'route':{'type':'string','enum':['whole_or_mostly_visible','partially_visible']}}}
DECISION_SCHEMA = {'type':'object','additionalProperties':False,'required':['action'],
    'properties':{'action':{'type':'string','enum':['retry_once','needs_review']}}}


def response_format(name, schema, structured):
    if not structured:
        return {'type':'json_object'}
    return {'type':'json_schema','json_schema':{'name':name,'strict':True,'schema':schema}}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class HttpChat:
    """One HTTP request, finite timeout, no SDK retries or redirects."""
    def __init__(self, base_url: str, *, api_key_env='DASHSCOPE_API_KEY', timeout=90):
        parsed=urlsplit(base_url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('base URL must be an HTTPS endpoint without credentials/query/fragment')
        if not 0 < timeout <= 600:
            raise ValueError('timeout must be in (0,600] seconds')
        self.key=os.environ.get(api_key_env)
        if not self.key:
            raise ValueError('missing API key environment variable: '+api_key_env)
        self.url=base_url.rstrip('/')+'/chat/completions'
        self.timeout=timeout
        self.opener=build_opener(NoRedirect())

    def __call__(self, payload: dict) -> str:
        request=Request(self.url,data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
                        headers={'Content-Type':'application/json','Authorization':'Bearer '+self.key},method='POST')
        with self.opener.open(request,timeout=self.timeout) as response:
            raw=response.read(2_000_001)
        if len(raw)>2_000_000:
            raise ValueError('response exceeds size limit')
        content=json.loads(raw)['choices'][0]['message']['content']
        if not isinstance(content,str) or not content:
            raise ValueError('empty model response')
        return content

class HttpVision:
    def __init__(self, transport, *, model: str, max_pixels=1_000_000, structured=True):
        if not 1024 <= max_pixels <= 4_000_000:
            raise ValueError('max_pixels outside supported range')
        self.transport=transport
        self.model=model
        self.max_pixels=max_pixels
        self.structured=structured

    def _image(self, path: str) -> str:
        with Image.open(path) as raw:
            image=ImageOps.exif_transpose(raw).convert('RGB')
            if image.width*image.height>self.max_pixels:
                scale=(self.max_pixels/(image.width*image.height))**.5
                image=image.resize((max(1,int(image.width*scale)),max(1,int(image.height*scale))))
            output=io.BytesIO(); image.save(output,format='PNG')
        return 'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode('ascii')

    def _call(self, request: VisionRequest, instruction: str, name: str, schema: dict) -> dict:
        # Fresh messages per call: no previous boxes, responses, or chat history.
        payload={'model':self.model,'temperature':0,'max_completion_tokens':4096,
            'messages':[{'role':'system','content':instruction},
                {'role':'user','content':[
                    {'type':'text','text':json.dumps({'trusted_species':request.task.species,
                         'visibility_route':request.route,'max_targets':request.max_targets},ensure_ascii=False)},
                    {'type':'image_url','image_url':{'url':self._image(request.task.image_path)}}]}],
            'response_format':response_format(name,schema,self.structured)}
        return json.loads(self.transport(payload))

    def visibility(self, request: VisionRequest) -> dict:
        return self._call(request,
            '你只做图片可见性分流。物种由可信元数据给定，不分类、不改名。'
            '完整或大部分可见使用 whole_or_mostly_visible；裁断、遮挡、密集重叠或无法确认存在使用 partially_visible。'
            '只输出 JSON {"route":"whole_or_mostly_visible或partially_visible"}，不输出框或名称。',
            'visibility',VISIBILITY_SCHEMA)

    def localize(self, request: VisionRequest) -> dict:
        return self._call(request,
            '只定位可信元数据指定的动物，不判断或改写物种。独立检查全图和边缘。'
            '每个可分离个体一个紧框，覆盖实际可见身体与连接的肢足。不得框泥洞、石块、植物或阴影。'
            '局部可见时不推测画外或隐藏身体；同一个体的遮挡两侧只有可靠关联时才合并。'
            '多个个体不能用群体大框代替。无可靠目标输出空 boxes。最多 max_targets 个可靠目标。'
            'bbox 为左上到右下 [x_min,y_min,x_max,y_max]，Qwen 0到999坐标，必须有正面积。'
            '只输出 JSON {"coordinate_system":"qwen_0_999","boxes":[{"bbox":[1,2,3,4]}]}。'
            '禁止输出 species、解释、置信度或额外字段。', 'localization',BOX_SCHEMA)

class ChatPlanner:
    def __init__(self, transport, *, model: str, structured=True):
        self.transport=transport; self.model=model; self.structured=structured

    def __call__(self, context: dict) -> str:
        return self.transport({'model':self.model,'temperature':0,'max_completion_tokens':128,
            'messages':[{'role':'system','content':
                '你仅处理两次定位未达成一致的异常。只能选择 retry_once 或 needs_review。'
                '禁止接受定位、重命名物种、生成代码或调用工具。预算不足时必须 needs_review。'
                '输出且只输出 JSON {"action":"retry_once或needs_review"}。'},
                {'role':'user','content':json.dumps(context,ensure_ascii=False)}],
            'response_format':response_format('decision',DECISION_SCHEMA,self.structured)})

class FixtureVision:
    def __init__(self, fixtures: dict):
        self.fixtures=fixtures

    def visibility(self, request: VisionRequest) -> dict:
        return self.fixtures[request.task.source_image]['visibility']

    def localize(self, request: VisionRequest) -> dict:
        return self.fixtures[request.task.source_image]['localizations'][request.pass_number-1]
