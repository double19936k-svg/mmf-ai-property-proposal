# REAL_ARTIFACT_REGRESSION_FIXTURE

- Run: `tender_20260910_001831_f113c9`
- Provider: Zhipu / GLM
- Mode: 快速
- Word: `青岛海信国际中心物业顾问服务项目_完整物业服务方案_20260910_011206.docx`

Do not modify the original Word in `dist/.../output`. This folder is a frozen copy.

## Problem A — conditionalization post-process pollution

Provider raw (`S07-01_provider_raw.json`) is grammatical:

> 未启用会议室执行通风15至20分钟标准，杯具及毛巾消毒不少于20分钟，茶叶等物料会前30分钟备妥，服务人员会前10分钟到岗迎候。

Factory/local post-process then produced unreadable debris in `S07-01_fragment.json` and the delivered Word:

> 通风按项目确认的安排标准 / 不少于可根据项目确认要求确定响应安排 / 会前按项目确认的安排

This is post-processing, not GLM.
