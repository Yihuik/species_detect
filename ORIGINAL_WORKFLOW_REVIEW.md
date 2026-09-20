# 原流程只读审查

来源：D:/codex/qwen-vl/src/qwen_grounding。

- metadata_workflow.py `_metadata_for_relative_path` 优先 CSV，随后使用可信物种表对子路径做名称推断；`_group_for_species` 要求知识库存在名称。
- evaluation.py `load_ground_truth_manifest` 读取 source_image,species，验证 species 清单和冲突。`infer_expected_species` 使用最长子串匹配。
- metadata_workflow.py `_route_visibility` 调用 client.request_many；`_localize_route` 再按可见性分组定位。该流程只有单次定位，没有独立双次/IoU一致性门控。
- 这两个函数可在部分 API 错误时递归拆分批次；client.py ClientConfig.max_retries 默认 2，由 OpenAI SDK 执行传输重试。
- `_write_result` 将 item.species 写回每个框，执行像素转换、JSON写出和标注图绘制；模型不负责改名。
- geometry.py bbox_to_pixels 对 qwen_0_999 使用 /1000，而不是 /999。
- run_metadata_grounding 以已有标注图和 JSON 的存在性判断跳过；没有本实现所需的逐阶段持久化状态和调用意图。

新实现边界：不修改或导入原写出流程，独立定义受约束协议；保留可信名称、可见性分流和坐标约定，增加双次定位、Python匹配、有限异常规划、事务审计及恢复。为避免含糊推断，新目录来源要求显式目录到名称映射，不复制原最长子串启发式。CSV本身可作为可信名称来源，不要求再读取原知识库。
