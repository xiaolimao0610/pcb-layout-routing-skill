# 嘉立创EDA适配、原生铜与写回契约

对应pcb_bridge.py、eda_adapter.mjs和pcb_native_copper.py。只使用已有连接与明确窗口；安装见 [桥接说明](../bridge/README.md)。原生操作成功、保存成功、地网连通及整板DRC通过分别报告。

## 实际来源与参考设计

导出采用实时API与来源明确的sidecar。context.document_uuid指向当前PCB，components用真实primitive ID保存footprint_source、局部body/courtyard、provenance，必要时含primitive_layers。不能用位号猜ID或用旋转后的世界AABB反推封装。

参考板默认只提供面积、占用率与明确工艺标准，不提供可直接移植的工程、器件坐标、路线或铜区。即使同拓扑，也禁止据此默认调用`eda.dmt_Pcb.copyPcb(referenceUuid)`或从样板移植对象；“参考样板”不是这种授权。只有用户明确授权复用对应内容后才允许，并记录范围、重新采集工作文档身份与几何。未获得该授权时，必须在当前工程独立计算布局布线。任何路径都不能把参考源UUID/指纹手填到当前板，复制也不是全局最优求解。

pad_coordinates必须用当前客户端互证的world或component_local。已知尺寸getter量化时用footprint_source模式，按UUID、PAD ID、实例字段及真实尺寸互证；pad_positions=footprint_source只容纳机器精度的变换差，不是几何统一舍入。槽孔保留实际长宽、角度与偏移。

copper_layers是实际物理顺序，隐藏层也保留；rules是经核对的离线规则，不能声称完整自动翻译所有原生规则。板框与cutouts须有真实来源，test_only禁止实板写回。metadata.source的sourceFingerprint仅规范化已知DOCHEAD顶层易变字段，任何其他变化都要求重新取证，不手换摘要。

## 布局、附属铜和板框

普通封装附属过孔默认固定父器件；只有当前客户端证据成立时启用footprint_via_movement=rigid同面刚体运动。翻面和独立改附属孔不在该支持范围。

pcb_compact.py只输出紧凑提案与统计，普通trace/pose计划不修改板框。按授权应用真实板框及姿态后必须重导来源，再路由。原生泪滴是实际铜；任意弧、复杂path及未知schema不能静默丢弃或变成弦多边形。

pcb_relayout.py为独立布局提供来源绑定的原生写回。它只清理显式列出的独立LINE/Pour/Via，并保护附属Via；板框使用先删全部旧LINE、再建最终四条矩形边的方式，避免逐边修改时原生合并导致ID消失。清理后核对相关TEARDROP/POURED自动消失，不独立删除派生铜。仅允许同面元件姿态，要求完整当前器件集合；原文备份和job_key持久化后一次后台派发。部分失败保留状态，以新来源准备剩余工作，不能重放旧任务。

relayout回读允许0.0001mil坐标序列化误差和模360的等价角度，层/网络/孔径保持严格核对。附属Via只在该阶段验证ID/网络/尺寸保留；成功仍要求fresh canonical export与独立结果对比其世界坐标及全部真实几何。calculation-only的路由结果在此之前不能写回，不能手填新指纹绕过live rebase。

## 原生线宽调整

`pcb_native_copper.py inspect`读取真实LINE/ARC的ID、网络、层、端点、线宽、锁定和弧角，另保留完整原生铜路径。prepare-widths接受明确default_width及net_widths策略；默认10 mil，本次+10V30 mil是用户给定工程规则。apply-widths只把不足的未锁定线改宽，保留原更宽线和几何路径，再按save保存。原生编辑可能合并共线LINE并改变ID；因此每次修改后读取全部实际线，通过同网/层/宽的双向区间覆盖证明整组线铜等价，ARC保留完整几何比较，不能只凭旧ID消失判断失败。加宽使旧铺铜过期，之后必须刷新铜区与DRC；它本身不承诺净距或载流通过。

## 原生铺铜与地网闭环

信号阶段使用ground_strategy:plane_then_drc和显式ground_nets时，只暂缓地网搜索，完整网表与已有铜保留。signal_stage_passed/status:awaiting_native_ground是阶段结果，full_board_accepted=false。先应用已授权信号预览，再从实板重新取来源准备铜计划。

`pcb_native_copper.py prepare`的新区域spec使用实际layer/net及真实outline；刷新spec还带实际pour_id，保持原分区、复杂轮廓、优先级与孤岛策略，新建区域才应用默认去孤岛策略。apply使用`pcb_PrimitivePour.create`和每个Pour的`rebuildCopperRegion()`生成真实实体，随后通过`getCopperRegion().getState_PourFills()`读取完整path；只创建Pour边框不算铺铜完成。单个既有区域重建后为空可以是合法结果，不中止其他区域；空区域和每层/网络铜覆盖必须明确记录，再由DRC判断连通。API返回成功本身不等于该层/地网已连通。

默认重建后读完整严格详细DRC。ground_targets仅来自明确地网的原生连接问题，随后用对象ID查询Pad/Via/Line/Arc世界坐标。DRC显示position未经坐标证明不能拿来布线。同一缺口两端可能分列，端点条目数不直接等于独立缺口数；已证明接入主平面的另一端不重复加孔。修复实际断开的一侧后，刷新受影响铜区，再跑DRC；未知问题分类、未证明对象关系和无法解析对象保留为未完成。

pcb_ground_repair.py只计算DRC点名焊盘或既有过孔的有限局部短线/全栈过孔，既有孔不移动、不替换，不调用全地网路由器。输入必须包含真实已填Poured铜及绑定当前source的mil坐标证据；参考是已接实际端子的一个连续Poured保守内界，同名浮空岛不成立。检查固定铜和全栈孔碰撞后，证明目标经局部桥接正面积接触参考铜；新孔不切既有固定铜。优先利用已接参考铜的原孔，不重复添加已有完整路径；原生仍报错时不能仅凭离线预测接触跳过。原生getSource/getSourceStrictComplex的数组层级不证明单位已转换；不支持的曲线/孔或未证明单位保留并拒绝计算，不生成导电凸包。写回后重铺并完整DRC复查；局部候选和API成功都不等于全地网已连通。

inspection现在还记录真实poured_id（缺失时不能猜测），与父Pour ID、每个填充区域ID分别保留。已有单位实证后可用pcb_native_poured_geometry把多环L/ARC路径转为保守铜界，支持±180半圆孔，不丢失原弧和孔洞。它是局部规划几何支持，不是原严格adapter复杂填铜签名的扩展。

完整单位取证入口为pcb_native_poured_capture.py。优先via-anchors只读采集原生VIA记录/API坐标/实际两半圆孔，至少8个非共线锚点及全部可识别圆孔必须确定唯一比例和零平移。capture是可选转换实验：完整源先本地备份，再用一次性任务转换、取证、删除唯一临时Fill、重铺并保存；已观察convertToFill改变边界，因此纯比例检验失败时不能强行接受。Fill复杂多边形文档接口为getSource；Poured区域为getSourceStrictComplex。中途中止只用collect查状态，已知尚在的临时Fill用recover清理，不重放转换；删除后总读getAll确认实际ID消失，不把delete布尔返回当事实。成功native-pour-profile.json通过export子命令传入adapter的显式native_pour_geometry分支，在同次现场导出读取新source并核对全部Poured源码/API。Python局部补地独立复核记录和坐标，不能用verified标签代替证据。

若局部GND孔会穿过+10V等派生电源铜，必须用同来源rebuild-spec列出实际Pour父ID；只允许这些Poured在写入后重铺产生避让，不把它们和固定Fill混为一谈。未列铜仍参与碰撞，所有实线、孔、焊盘和固定Fill保持。候选状态candidate_awaiting_rebuild不是成功接地证据；完成写入后重建清单内全部铜区并跑原生DRC，确认地网与非地网均未被新的避让破坏。

`ground_connections_reported_clear`只说明所指定地网没有已识别的残余连接问题且检查证据完整；`native_drc_passed`才描述全报告。DRC有报错时原生工作流会保留操作结果和问题记录，不自动回滚已完成工作。用户当前明确要求纯预览时才可skip-drc，不能用它跳过正在要求的修复闭环。

## 写回模式与恢复

严格pcb_bridge apply保存原文备份，应用完整计划并回读目标，原生DRC通过后按save保存。线段拆合只有同网/层/宽和受限共线双向覆盖可接受，不能接受新偏移、间隙或额外铜。preview_only计划只走已授权的pcb_preview入口；该入口不做完整目标或DRC签核。

原生铜/线宽任务与preview都在EDA存储保留job key与备份，后台只派发一次。等待超时只查询status，不关闭正在操作的文档、不重发。必须分别看final、success、saved、errors、原生DRC状态。部分失败或未知结果先看当前状态，不能自动重放。

原生恢复不是原子事务。严格目标差异若只有新增泪滴，review-native-copper仅接受绑定原计划和完整恢复证据的失败收据；不能手改receipt/指纹以接续。复杂铜无法进入canonical时可保留原生inspect路径与DRC结果，不能宣称离线验收通过。

制造资料只在当前来源稳定、原生DRC条件满足后导出；文件长度/SHA-256可校验，但生成文件不等于实板功能、温升、SI/PI或EMC合格，也不会自动下单。

## 独立原生位号显示整理

`pcb_native_silkscreen.py` 默认动作 `place` 修改已有顶层 Designator 属性的显示位置、-90/0/90/180度角、字号、笔宽及已验证的对齐方式，保留可见性及其他属性。计划顶层恰含 `source/changes`；source 使用当前 `document_uuid/fingerprint`（可选usage:physical）。place 项必含 `id/parent_id/value/x/y/rotation/font_size/line_width`，仅允许额外字段 `action:"place"`、`align_mode`：省略为居中5，也支持明确左下对齐3。后者用于在新来源上精确恢复已备份的原显示位置，不重放旧任务或替换来源摘要。value是用于核对的原文字，不是改名指令；ID唯一、数字有限且非布尔，字号和笔宽为正，未知字段拒绝。工艺字号及净距须由工程要求与实际字框检查决定，入口不自动缩字。

用户明确允许省略放不下的位号时，可在同一计划使用 `{"id":"实际属性ID","parent_id":"实际父ID","value":"原位号","action":"omit","reason":"本对象的省略原因"}`。omit 项仅允许这五个字段，reason必须为非空字符串；原生只调用 `setState_ValueVisible(false)`，不删除属性对象、不改逻辑位号及网表，也不修改位置、角度、字号或笔宽。默认不省略；不能仅凭候选失败自动推定授权。此入口仅支持Designator，封装轮廓、极性、1脚和接口标记不在自动省略范围。

```powershell
python -B pcb_native_silkscreen.py --window-id "$windowId" apply --plan "$pcbRunDir/label-plan.json" --audit-dir "$pcbRunDir/label-writeback-new"
python -B pcb_native_silkscreen.py --window-id "$windowId" status --job-key "$jobKey" --output "$pcbRunDir/label-status.json"
```

审计目录必须全新。入口复用现有 dispatch：先保存 request.json 和 before-native.epru，再验证实际来源，随后只派发一次后台任务。原生侧再次检查文档UUID/指纹、目标ID/父ID/Designator/value及显示支持范围，保存原文和全部属性到 `job_key:backup` 后才修改。两个动作均只接受已有可见、未锁定、非镜像/反转、key隐藏且value显示的顶层位号，超出范围明确拒绝；已省略目标不能重放该计划。

收据的 applied、saved、attribute_readback.passed、unrelated_source_unchanged、success 分别核对，full_board_accepted始终false。写后读取全部属性，逐ID检查数量与全部字段；place 只允许计划中的几何变化，omit 只允许 `valueVisible` 从true变false。完整原文比较也逐动作限制字段：omit目标仅排除真实ATTR的valueVisible及ticket，place目标保持原几何字段白名单，可见性仍严格。其余记录须不变，属性读取前后的来源必须稳定；写后原文和属性保存在 `job_key:after`。这些证据只证明请求的显示改动及其他内容保留，不证明字体实际净距、原生DRC或整板验收。

超时、pending、保存失败、差异或结果未知时只查原 job_key；request.json 即使没有 dispatch.json 也保留任务键。不得因未知结果再调用apply或换新审计目录重放。部分完成保留现场，以新来源准备具体差异的新计划。写入前运行显式字框/焊盘净距检查，并核对未安置文字的原位置；写后实际查看原生丝印效果，不把属性回读成功替代视觉检查。
