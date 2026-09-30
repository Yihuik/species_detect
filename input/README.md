# 用户照片收件箱

把用户主动提供的照片放入一个批次目录。顶层目录名必须是
`config/species.txt` 中逐字相同的物种名称；可以直接放图片，或放入 ZIP。

```text
input/inbox/2026-09-18-batch-a/
  石磺/
    field-001.jpg
  锯缘青蟹/
    phone-photos.zip
```

导入会复制合格的非重复图片到 `photos/`。这里的原始用户文件不会被移动、
删除或修改。未知物种、无效图片、精确重复和近似重复都保存到 `photos/pending/`
的不同原因目录，供人工后续抽查。

执行 `tools/start_agent.ps1` 创建新标注任务时，会在公开来源采集后自动导入
`input/inbox/`，并在调用模型前更新 `photos/workflow_metadata.csv`。继续已有
标注任务的 `tools/resume_agent.ps1` 不导入新照片，避免改变该任务的输入。
