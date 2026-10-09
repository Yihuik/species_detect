"""Version 2 prompts; original provider remains available to historical runs."""
import base64
from copy import deepcopy
import io
import json
from PIL import Image, ImageOps
from .providers import BOX_SCHEMA, HttpVision, response_format
from .models import Localization

VISIBILITY_SCHEMA_V2 = {'type': 'object', 'additionalProperties': False, 'required': ['route'],
    'properties': {'route': {'type': 'string', 'enum':
        ['whole_or_mostly_visible', 'partially_visible', 'mixed']}}}

VISIBILITY_PROMPT = (
    '只判断可信元数据指定物种的目标可见性，不改名。先独立检查全图和边缘。'
    '身体主体完整、仅少量肢足遮挡可视为完整或大部分可见；身体主体被明显遮挡或被画面裁断视为局部可见。'
    '所有可确认个体均完整或大部分可见使用 whole_or_mostly_visible；'
    '所有可确认个体均局部可见使用 partially_visible；'
    '至少一个完整或大部分可见个体且至少一个局部可见个体使用 mixed。'
    '不得因主要个体完整而忽略其他局部个体。无法确认存在时使用 partially_visible，后续不得虚构目标。'
    '只输出 JSON {"route":"whole_or_mostly_visible或partially_visible或mixed"}。')

CARAPACE_CHECK_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['status', 'evidence'], 'properties': {
        'status': {'type': 'string', 'enum':
            ['not_applicable', 'symmetric', 'unassessable', 'suspected_asymmetry']},
        'evidence': {'type': 'string', 'maxLength': 300}}}
BOX_SCHEMA_V2 = deepcopy(BOX_SCHEMA)
BOX_SCHEMA_V2['properties']['boxes']['items']['required'].append('carapace_check')
BOX_SCHEMA_V2['properties']['boxes']['items']['properties']['carapace_check'] = CARAPACE_CHECK_SCHEMA


def require_carapace_checks(result):
    """Live v2 responses must not silently bypass the human review gate."""
    localization = Localization.model_validate(result)
    if any(box.carapace_check is None for box in localization.boxes):
        raise ValueError('missing per-box carapace_check')
    return result

CARAPACE_PROMPT = (
    '对于蟹类，先定位可见头胸甲，作为区分个体和关联肢体的锚点；'
    '只纳入可靠归属于该个体的可见螯和步足，不把邻近个体的肢体合并。'
    '最终框为实际可见头胸甲及已确认肢体的最小外接矩形，不额外扩边；'
    '矩形内部可以有肢体之间的背景空隙，不强求整框左右对称。'
    '每个框输出 carapace_check。非蟹类使用 not_applicable。'
    '蟹类只比较头胸甲中线至左右外缘的宽度，沿相同横断面比较，不能用整框宽度或螯、步足比较。'
    '中线和两侧轮廓可可靠比较时，参考头胸甲左右大致对称；未发现明显不对称使用 symmetric。'
    '如果一侧比另一侧宽得明显异常，且不能合理用视角解释，或疑似混入邻体、背景，'
    '使用 suspected_asymmetry，并在 evidence 写明简短可见依据，供人工审核；仍保留实际观察到的候选框。'
    '斜视、遮挡、裁切或轮廓模糊导致不能可靠比较时使用 unassessable，不凭对称性补全或缩小框。'
    '招潮蟹大螯、伸展步足及其造成的整体框不对称不能作为头胸甲不对称的依据。'
    'evidence 最多300字符，只描述可见疑点或无法比较的原因，不输出推理过程。')

OUTPUT_PROMPT = (
    '只输出 JSON {"coordinate_system":"qwen_0_999","boxes":[{"bbox":[1,2,3,4],'
    '"carapace_check":{"status":"not_applicable或symmetric或unassessable或suspected_asymmetry",'
    '"evidence":"简短可见依据，无疑点可为空字符串"}}]}，禁止额外字段或JSON之外的解释。')

LOCALIZATION_PROMPT = (
    '只定位可信元数据指定物种的动物，不改写物种名。独立检查全图及边缘。'
    '无论 visibility_route 是哪一类，都标注所有可可靠区分的目标；mixed 必须同时包含完整和局部可见个体。'
    '每个个体一个紧框，覆盖实际可见身体与连接的肢足；局部个体不推测隐藏或画外身体。'
    '同一个体遮挡两侧只有可靠关联时才合并；不能用群体大框替代个体。'
    '不得框泥洞、石块、植物或阴影，不得将其他物种当作指定物种。'
    '无可靠目标输出空 boxes，最多 max_targets 个可靠目标。'
    'bbox 是 [x_min,y_min,x_max,y_max]，使用当前图像的 Qwen 0到999坐标，必须有正面积。'
    + CARAPACE_PROMPT + OUTPUT_PROMPT)

CROP_PROMPT = (
    '这是原照片的一个带上下文局部区域。重新独立检查该局部图，不推测此前标注。'
    '只定位可信元数据指定物种的所有可可靠区分个体，包括完整和被遮挡、裁断的个体。'
    '每个个体一个紧框，覆盖实际可见身体与连接的肢足，不推测隐藏或画外身体；不得用群体框。'
    '不得将泥洞、石块、植物、阴影或邻近个体纳入同一个体的框。'
    '无可靠目标输出空 boxes，最多 max_targets 个目标。'
    'bbox 必须相对于当前局部图，使用 Qwen 0到999坐标，顺序 [x_min,y_min,x_max,y_max]，正面积。'
    + CARAPACE_PROMPT + OUTPUT_PROMPT)

class TargetHttpVision(HttpVision):
    def visibility(self, request):
        return self._call(request, VISIBILITY_PROMPT, 'visibility', VISIBILITY_SCHEMA_V2)

    def localize(self, request):
        return require_carapace_checks(self._call(request, LOCALIZATION_PROMPT, 'localization', BOX_SCHEMA_V2))

    def review_target(self, request):
        with Image.open(request.task.image_path) as raw:
            image = ImageOps.exif_transpose(raw).convert('RGB')
            if image.size != (request.task.width, request.task.height):
                raise ValueError('source image dimensions changed')
            x, y, r, bottom = request.region
            if not (0 <= x < r <= image.width and 0 <= y < bottom <= image.height):
                raise ValueError('invalid crop region')
            image = image.crop(request.region)
            if image.width*image.height > self.max_pixels:
                scale = (self.max_pixels/(image.width*image.height))**.5
                image = image.resize((max(1, int(image.width*scale)), max(1, int(image.height*scale))))
            output = io.BytesIO(); image.save(output, format='PNG')
        image_url = 'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode('ascii')
        payload = {'model': self.model, 'temperature': 0, 'max_completion_tokens': 4096,
            'messages': [{'role': 'system', 'content': CROP_PROMPT}, {'role': 'user', 'content': [
                {'type': 'text', 'text': json.dumps({'trusted_species': request.task.species,
                                                   'max_targets': request.max_targets}, ensure_ascii=False)},
                {'type': 'image_url', 'image_url': {'url': image_url}}]}],
            'response_format': response_format('localization', BOX_SCHEMA_V2, self.structured)}
        return require_carapace_checks(json.loads(self.transport(payload)))

class TargetFixtureVision:
    def __init__(self, fixtures):
        self.fixtures = fixtures

    def visibility(self, request):
        return self.fixtures[request.task.source_image]['visibility']

    def localize(self, request):
        return self.fixtures[request.task.source_image]['localizations'][request.pass_number-1]

    def review_target(self, request):
        return self.fixtures[request.task.source_image]['target_reviews'][request.target_id]
