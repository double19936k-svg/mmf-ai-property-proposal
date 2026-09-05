# Output Medium Profile V0.1

状态：candidate_pending_todd_media_profile_review

适用范围：当前MVP仅定义PPT正式方案与Word正式方案两种输出媒介。HTML、Web、PDF、Excel及其他媒介不在本版本范围内。

## Profile-P：PPT正式方案

PPT以“页面表达”为基本单位，每页只承担一个明确主题，采用“页标题 + 一句核心结论/主旨 + 主体视觉结构 + 少量补充文字”的基本语法。

主体视觉结构优先使用真实表格、流程、模块、责任矩阵、时间轴、组织关系或对比结构。不得把Word长段落直接塞入页面，不得以网页卡片、Dashboard或Word彩色排版冒充PPT。信息过多时拆页，不通过压缩字号维持一页完整。

最低交付特征：清晰层级、合理留白、明确页面重心、可识别的信息模块，以及真实存在的表格或流程。

## Profile-W：Word正式方案

Word以“正式文档”为基本单位，采用标题体系、连续专业论述、必要列表与真实表格组织内容。正文应适合连续阅读、编辑、打印与审阅，黑白打印时结构仍须成立。

表格只用于真实的重复行列信息，例如专业分组、责任分工、计划或问题清单。连续说明性内容使用自然段落，不以大量卡片、视觉模块或口号式短句替代正文。

最低交付特征：明确的一级/二级/三级标题层级、自然连续的正文段落、必要且真实的Word表格、稳重克制的页面样式。

## 内容与治理边界

本Profile只控制输出介质的结构与呈现，不修改Knowledge Layer、Generation Layer、Delivery Rendering业务规则、Guardrail、Writing Style、Schema或知识内容。

本轮Case 4固定为PPT，Case 5固定为Word。两者均以现有Rendered V2为content_baseline，要求 `content_semantics_changed=false`。

本轮未调用Grok Build。
