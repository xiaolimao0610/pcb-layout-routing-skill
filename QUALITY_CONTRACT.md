# 阶段4：后处理与信号质量契约

热焊盘端帽的制造依据另见 [Gerber补证契约](AUTOMATION_AND_GERBER.md#gerber受限补证)。只有重新验证原生POURED记录、成功导出收据、ZIP成员与逐对象几何后建立的上下文才可改变连接内界；普通对象的原生真实性仍由原采集/适配及读回流程证明。默认未知端帽仍保守检查，补证不减少异网间距外界、不授予整板原生验收；清铜、孔洞、变换及未支持语法按明确profile报告。

本文件补充 [INPUT_CONTRACT.md](INPUT_CONTRACT.md)。新功能由显式字段启用；不提供这些字段时保持原有算法、线宽及验收流程。全部尺寸为 mil，时间为秒。未知字段、非法类型和矛盾声明均报错。下文算法只生成候选，不能替代原 `check_board`、`check_routed_nets`、来源绑定写回及原生 DRC。

## 1. 后处理

流水线可选 `options.cleanup`，或调用 `cleanup_board(board, options=None, *, original_board=None)`，返回新板副本与统计。`validate_options(options, board)` 提前校验；格式、来源错误抛 `PipelineError`。输入普通几何/连通错误时保持原板，仅输出诊断，`passed=false`。

| 字段 | 默认与限制 |
| --- | --- |
| enabled / straighten / regularize_45 / remove_redundant_vias | true，严格布尔 |
| ground_nets / shield_nets / signal_nets / deferred_nets | []，不重复的真实网名 |
| protected_trace_ids / protected_via_ids | []，本板真实对象 ID |
| max_candidates | 500，整数 1..100000 |
| max_passes | 3，整数 1..100，每轮最多接受一项改动 |
| max_seconds | 15，有限正数，合作式总预算 |

只修改 `original_board` 原铜前缀之后新增、未锁定的铜。原板必须具有相同来源、板框、规则、完整网表和器件姿态，并保留全部原铜前缀；不传原板则全部输入铜受保护。流水线自动使用布局后、布线前快照。流水线的 `deferred_nets` 只能等于实际 `plane_then_drc` 地网列表，不能借此暂缓信号网。

橡皮筋拉直在相邻线段间尝试更短路径；45°规整生成横、竖、45°折线候选。每次修改须通过原精确检查并保留全部端子连通。差分、等长、地、屏蔽、附属孔、锁定及来源保护对象不作普通清理。有限搜索停止表示未找到进一步改进，不表示不存在更优布线。

冗余孔自动消除还要求网络在显式 `signal_nets` 中、实际接触超过一层、没有铺铜接触，且删除后原完整信号验收仍通过；不能用“没有 GND 标签”证明没有接地用途。死枝、单层接入孔和悬空孔始终仅列候选，记录逐层实体接触与保护理由、`auto_delete=false`。端点疑似死枝不是完整删除证明，本模块不自动删除这类对象。后续逐对象删除须另行证明不连接铺铜、地或屏蔽。

统计包括 `changes`、`listed_candidates`、`candidate_listing_complete`、预算及原铜保留证据。`passed` 只表示本检查范围，`full_board_accepted` 始终 false；预算未覆盖全部对象时明确标记。

```powershell
python -B pcb_cleanup.py --board "$pcbRunDir/routed.json" --original-board "$pcbRunDir/before-routing.json" --options "$pcbRunDir/cleanup-options.json" --output-dir "$pcbRunDir/cleanup-result"
```

输出预览板与报告，不自动生成原生删除计划。CLI 通过退出 0，检查失败退出 2，输入错误退出 1。

## 2. 铺铜后的双向回流几何复核

流水线可选 `options.return_path`。完整离线流程在铺铜后调用；`plane_then_drc` 信号阶段只验证配置，并在 `pending_stages` 添加 `return_path_after_native_pours`，必须在原生重铺、重新导出实际铜之后执行。不会用计划中的铜区冒充实际参考面。

```json
{
  "signal_nets": ["SIG"],
  "reference_layers": [
    {"signal_layer": 1, "reference_layer": 15, "reference_net": "REF"}
  ],
  "guard_band": 10,
  "min_neck_width": 0,
  "max_checks": 10000,
  "max_seconds": 30
}
```

上例网名与层号必须换成当前输入真实值。`signal_nets`、`reference_layers` 为必填非空列表，映射项恰含上述三个字段；参考层必须在真实物理叠层中与信号层相邻，参考网与信号不同。保护带和最小颈宽有限非负；检查数为 1..1000000 整数，总秒数有限正数。不按网名猜测参考层。

`check_return_paths(board, options, *, before=None)` 返回报告，不改变板。只使用已物理接入真实端子的填充铜，排除浮空同名岛；投影含原圆端线铜保守外界及端帽。保护带取线宽投影之外的环带，缺失面积不与投影重复计数。最小颈宽使用腐蚀/膨胀形态复核，不是电磁场求解。

| 报告字段 | 意义 |
| --- | --- |
| trace_projection_missing | 信号线宽实体投影缺铜 |
| guard_band_missing | 外围保护带缺铜，或显式最小参考颈宽不足 |
| via_clearance_change | 新增/变化孔的影响范围与旧信号参考面真实失铜相交 |
| old_signal_reference_changes | 修改前每条信号原位置的真实前后失铜，含新增铜造成的分割 |
| reference_disconnections | 端点不在同一参考面分量，或原连通参考面被分割 |
| unknown / complete | 未锚定铜、缺参考映射、缺前快照、无实际线、预算未完成等证据缺项 |

反向检查仍覆盖已经移动或删除的旧信号原位置。实际失铜来自前后铜面差集；孔/铜归因依照原类型间距矩阵、NPTH 间距及真实钻孔，标为可能成因，不用估算净空生成“已重铺”证据。同参考网孔仅按钻孔影响归因。前后 UUID 和实际叠层必须一致，指纹允许随真实改动更新。

缺前快照时仍可输出正向发现，但不能宣称双向检查完整。无缺失、无参考断裂、无未知且原硬几何合法时 `passed=true`；`full_board_accepted` 仍为 false。几何证据每条最多 4000 顶点，超限保留实际面积/边界并标记 `geometry_evidence_complete=false`。

```powershell
python -B pcb_return_path.py --board "$pcbRunDir/after-repour.json" --before "$pcbRunDir/before-change.json" --options "$pcbRunDir/return-options.json" --output "$pcbRunDir/return-report.json"
```

CLI 通过退出 0，有发现或未完成退出 2，输入错误退出 1。该检查不证明频域回流、阻抗、串扰、温升或原生 DRC 合格。

## 3. 差分与等长

`CHANNEL_SPEC` 增加 `diff_pair:{p,n,gap,width}` 与 `length_group:{name,tolerance}`，子对象必须恰含这些字段。`gap` 是两线铜边间距，`width` 是两线相同宽度，均有限正数；`tolerance` 为有限非负的组内最大长度差。P/N 为不同真实网，外层 `net` 必须属于该对；同一对只需声明一次。同一网不能加入两个差分对或两个等长组，重复声明必须一致。等长组在每个成员的规则中声明。

```json
[
  {"operator":"CHANNEL_SPEC","net":"DP","allowed_layers":[1],
   "diff_pair":{"p":"DP","n":"DN","gap":10,"width":10},
   "length_group":{"name":"PAIR_A","tolerance":5}},
  {"operator":"CHANNEL_SPEC","net":"DN","allowed_layers":[1],
   "length_group":{"name":"PAIR_A","tolerance":5}}
]
```

网名仅示范结构，不凭名称识别差分。层限制等其他 `CHANNEL_SPEC` 字段作用于其外层 `net`，需要两网相同时应分别声明。差分宽度不能低于任一成员的既有下限或载流计算下限，间距不能低于原 DRC；不能为了耦合放宽规则。

默认实现支持两个真实端子的、无支路、无过孔/铜区的单层路径。端对按唯一最短跨网端距匹配，对称歧义明确拒绝。差分候选先生成中心线，再按 `(width+gap)/2` 两侧偏移，加入有限端部扇出；经原 `_commit` 逐网精检后，从实际成对折线独立重建偏移关系，避免把 45°转角当成失配。端部豁免区受端距限制且必须存在非端部耦合核心。生成时 metadata 不能代替实际铜证据。

可选 `routing.diff_layer_change` 支持一或两次成对全栈通孔换层，范围与配置见 [密集区域契约](DENSE_ROUTING.md)。独立检查器从实际铜重建各层链和配对孔，拒绝断连、支路、错误跨度、非配对孔及同层不同分段形成的铜捷径。仅报告可证明的平面长度；孔段未知使length_complete=false，跨层等长组仍拒绝。return_path_verified=false必须在铺铜后另行复核；几何耦合通过不表示回流、阻抗或电气延迟已验证。

中心线搜索枚举直线、45°折线和侧向绕行走廊，是有界候选搜索，不保证所有可行通道均能发现。先构造耦合对，再由所选 sequential/negotiated 方法处理普通网。不能与同一差分成员的 `topology_orders`、前置铺铜或延期地网混用；冲突明确拒绝。已有铜符合声明时保留，否则报错，不暗中拆掉旧铜。选网 `pcb_continue.py` 暂不执行差分/等长联合构造，检测到这些声明即拒绝并要求使用完整 solve 入口。

等长以组内最长实际路径为目标，尝试在本轮新增线段内插入有限 45°蛇形；不修改原铜，也不破坏差分耦合。独立测量从实体线段重建路径，拒绝分支、重叠、跨层歧义、非相邻宽铜捷径及中途再次接触端子。合法共线拆分不改变测量意义。没有空间或预算耗尽则报告未找到，不能只增加统计长度。单成员组明确 `matching_required=false`，不宣称完成了多网匹配。

| routing 新字段 | 默认与限制 |
| --- | --- |
| diff_max_candidates | 128，每轮每对中心线/层组合上限，每组合至多两种左右分配，整数 1..4096 |
| length_max_candidates | 128，每轮每网补长候选上限，整数 1..4096 |
| length_max_amplitude | 200，有限正数，蛇形最大幅度 |

上述搜索共享原 `max_seconds` 与 `max_net_seconds`；GEOS/精确检查单次调用不能被合作式预算抢占。统计在 `differential`、`length_matching`、`signal_quality` 中记录候选数、方法、实际长度、偏差、耦合核心及扇出段。`quality_report` 独立重算 `measured_lengths_mil`、`measured_spread_mil`；最终规则恢复后交原检查器验收。长度是走线中心线几何长度，尚不包含封装内延迟、介质速度或电气相位补偿。

## 4. IPC-2152 数据拟合线宽

`CHANNEL_SPEC` 可同时提供 `current_a`、`temp_rise_c`、`copper_oz`。不提供时完全保持已有选宽规则；只提供部分参数报错，不猜温升或铜厚。

```json
{"operator":"CHANNEL_SPEC","net":"BUS","min_width":30,
 "allowed_layers":[1],"current_a":3,"temp_rise_c":10,"copper_oz":1}
```

实现采用 Brooks/Adam 2015 年对 IPC-2152 数据的公开静止空气拟合，**不是 IPC 发布的通用闭式公式，也不是 IPC-2221 公式改名**。按物理层顺序识别外层/内层，在全部允许层分别计算并取最大；最终下限取计算值、现有规则及显式网络/差分宽度下限的最大值，没有显式宽度声明时保留原默认10mil。显式细线不能低于计算值或真实硬规则，详见 [选宽修复](REVIEW_FIXES.md)。编译时写入网络下限及 `metadata.current_width_calculations`；独立验收从原参数重算，不信任缓存或篡改的标签。既有线不自动改宽，低于新要求即报错。

公式为 `ΔT=K·I^a·W_mil^b·Th_mil^c`，厚度沿用论文 `Th_mil=1.3×copper_oz`。外层用 `(K,a,b,c)=(215.3,2,-1.15,-1)`；内层依 0.5/1/2/3 oz 分别用 `(130,2,-1.10,-1.52)`、`(200,1.9,-1.10,-1.52)`、`(300.3,2,-1.15,-1.52)`、`(300,1.9,-1.15,-1.52)`，有区间的系数取上端，不给铜面散热减宽收益。该选取及环境局限随每层证据输出。

本实现参数范围为 0..20 A、1..100 ℃、0.5/1/2/3 oz；这些是实现边界，不是标准强制限额。正电流的拟合宽度小于 5 mil 时取 5 mil，超过 200 mil 明确拒绝外推；最终再合并既有下限。计算宽度向上保留四位小数。3 A、10 ℃、1 oz 外层基线结果为 **77.6004 mil**。0 A 计算值为 0，既有下限仍保留。

输出明确 `standard_compliance_certified=false`。实际成品铜厚、板材/厚度、铜面、邻近热源、空气流动、真空、孔壁与焊盘铜颈、并联/瞬态均需另行验证。不能用本结果宣称实板载流或温升已验收。

可追溯依据：[IPC-2152 官方范围与目录](https://www.ipc.org/TOC/IPC-2152.pdf)；[Brooks/Adam 原论文式 3-2、表 3-1、附录 6](https://www.mathscinotes.com/wp-content/uploads/2016/06/pcbtempr.pdf)；[KiCad 官方实现说明对该数据拟合的引用](https://docs.kicad.org/doxygen/tracks__width__versus__current__formula_8h_source.html)。本包使用公式事实，未复制第三方实现或完整标准。

```powershell
python -B pcb_ampacity.py --current-a 3 --temp-rise-c 10 --copper-oz 1
```

## 5. 逐层 PNG 与实际视觉复核

`render_board(board, output_dir, options=None, *, before=None)` 从完整真实几何导出图，不修改板。输出目录必须全新且父目录存在；失败保留未完成清单，不覆盖旧证据。使用 NumPy、Shapely 和标准库 PNG 编码，不新增图形依赖。

| 配置字段 | 默认与限制 |
| --- | --- |
| layers | 全部真实铜层，不重复 |
| width / height | 1200 / 900，每幅 128..4096 像素 |
| margin_pixels | 24，0..256整数，须满足视口边界校验 |
| geometry | inner，可选 outer，分别使用保守内/外界 |
| show_components | true，严格布尔 |
| max_pixels | 60000000，1..268435456整数，全部计划输出 PNG 的像素预算 |
| max_seconds | 60，合作式渲染总时限 |

```powershell
python -B pcb_render.py --board "$pcbRunDir/after-repour.json" --before "$pcbRunDir/before-change.json" --output-dir "$pcbRunDir/layer-review"
```

每层输出 after PNG；提供 before 时另输出 before 与“前/后/几何差异”三栏图，使用同一世界坐标与比例，不自动翻转底层。图中包括真实铺铜、走线、焊盘、孔环、钻孔及板内切孔，不以中心线或实心外边界冒充铜。差异图绿色新增、红色删除、紫色网络重分配。`manifest.json` 保存来源、视口、逐层面积、文件哈希与完成状态，`index.html` 提供总览。

`render_complete=true` 仅表示文件已生成。AI 必须实际打开逐层及改动邻域 PNG，记录查看的路径、发现与未确认事项后才能给视觉结论；低分辨率图不能替代尺寸检查。清单不自动声称“AI 已查看”。最终仍须原生 DRC、真实保存/读回及适用实板验证，所有新增检查均不提高 `full_board_accepted` 的原有语义。

## 6. 装配净距与丝印质量筛查

从实际封装与装配图取得连接器的插合方向、线束和锁扣活动范围，以及高件周围焊接、夹持和返修所需的包络。用这些数据设置位置和净距，避免小件沿大电容紧贴一圈。调整后重新检查完整几何、布线和原生图像。

`pcb_assembly.check_assembly(board, options)` 返回独立质量报告，不修改板、配置、器件、铜、文字或验收规则。CLI 如下；退出0仅为声明范围筛查通过，2为发现/未知/未完成，1为非法输入。报告不能覆盖输入板或配置。未知字段拒绝；不将这些字段加入 solver options。

```powershell
python -B pcb_assembly.py --board "$pcbRunDir/current-board.json" --options "$pcbRunDir/assembly-options.json" --output "$pcbRunDir/assembly-report.json"
```

| 独立配置字段 | 约定 |
| --- | --- |
| source | 与当前 `board.metadata.source` 完全一致的原生来源对象；不符拒绝，缺少则 unknown |
| high_components | 可选列表；每项 `component_id/envelope/clearance/provenance`，ID必须真实且唯一 |
| silkscreen | 可选实际对象列表；每项字段见下文，原生 ID 必须唯一，不静默忽略未知几何 |
| silkscreen_expected_ids | 同次原生采集、同检查范围的完整原生丝印 ID 清单；合并outline按source_ids展开核对，缺少、漏项或额外项均 unknown |
| silkscreen_clearance | 工程明确的同层丝印实体边缘净距，有限非负 mil；未提供只报告重叠，并标 unknown |
| silkscreen_pad_clearance | 工程明确的丝印到真实焊盘外缘净距，有限非负 mil；缺省语义同上 |
| max_checks / max_seconds | 默认100000个同层对象对、30秒；前者整数1..1000000，后者有限正数。合作式预算，不抢占单次GEOS调用及输入规范化 |

至少声明 high_components 或 silkscreen 一项；未声明的范围不检查，不能据此称整板装配/丝印合格。显式空范围、缺少非空 provenance 或必要证据都列入 unknown，不视为没有问题。工艺净距没有通用默认，不从电容位号、封装名称、网名或照片像素猜测；工程数值与依据保存在项目配置或独立示例中。

高件 `envelope` 是至少三个二维点的局部简单多边形，按该真实器件的底层镜像、旋转和平移变换，必须覆盖实际本体，不得以缩小包络制造通过。`clearance` 是明确要求的外壳/高件包络到周围器件完整本体、courtyard及焊盘包络的焊接净距；同安装面逐件检查，异面不混报。`provenance` 写包络与净距的实际依据。候选只建议重排邻近器件；固定/锁定器件转为人工复核提示，不擅自改变任何姿态。该二维检查不证明烙铁三维接近角度、工具尺寸或完整装配可达性。

丝印世界坐标必须来自匹配来源的原生几何，不从截图推算，不能拿旧姿态文字框证明新布局通过：

- 线段：`{id,layer,kind:"line",sx,sy,ex,ey,width,provenance}`。真实非零线段、正笔宽，按现有圆端线的保守外界检查，不能只用零宽中心线。
- 文字框：`{id,layer,kind:"text_bounds",points,bounds_are_conservative,provenance}`。points 是包含旋转后字形的保守世界坐标多边形；AABB可以提供，但不能由字数和字号估算后声称已实测。`bounds_are_conservative` 必须显式true并有依据才可通过筛查；缺失/false列为unknown。报告始终标明文字框发现为潜在字形冲突，不宣称字体轮廓相交或精确字形净距。
- 合并轮廓：`{id,layer,kind:"outline",geometry,source_ids,bounds_are_conservative,provenance,owner_id?}`。geometry恰含GeoJSON的type/coordinates，type为Polygon或MultiPolygon，有限世界坐标环必须显式闭合且拓扑有效；保留全部内孔和分离岛，不取凸包、不补洞、不用自动修复猜测真实几何。source_ids是同次采集被合并的全部原生图元ID，非空、唯一且不能与其他对象重复；可选owner_id须为真实器件ID。同一封装固定轮廓可合并成一个实体，其内部相连线段不互查；文字对该轮廓、轮廓对其他对象及焊盘仍检查。转换曲线/笔宽必须有保守外界证据，bounds_are_conservative缺失或false为unknown，不能仅改变kind绕过证据。

丝印层3为顶层、4为底层，仅比较同层对象。丝印到焊盘使用原真实焊盘外界、底面变换及实际铜层；有跨层孔的焊盘检查对应两面，不以器件安装面错误覆盖实际焊盘层。未经有证据转换的圆弧、字体或复杂对象须保留为未决证据，不能省略其 ID 或伪装成直线。单独提供的线段间有意相接也会列为质量提示，封装轮廓应优先按同owner合并为outline再查，不能将这种提示称为硬DRC失败或自动删除依据。

报告含完整 `input_inventory`（输入对象与展开后的silkscreen_observed_source_ids分别保存）、缺失/额外丝印ID、`processed_object_ids`、`checks/complete/budget_exhausted/unknown`、最近点、双方边界、实际几何距离、要求距离及重叠面积。清单只证明传入范围的覆盖，不能自证原生采集无遗漏。`candidate_rules` 默认 `auto_apply=false`，保持对象、字号并要求修改后重跑硬检查。`passed` 要求范围内无发现、无未知且预算完整；`full_board_accepted` 永远false。预算耗尽只说明未完成，不称无解。

位号位置候选由 `pcb_silkscreen.place_labels(labels, obstacles, board_polygon, *, gap, max_seconds, max_candidates)` 独立生成。labels 每项恰含 `id/owner/x/y/width/height/owner_bounds`，坐标及宽高与板同单位，width/height为保守字框尺寸，owner_bounds为当前器件世界边界；obstacles是调用方按同面组织的完整 Shapely 障碍列表。gap由工程显式提供，两个预算也必填。函数在器件附近有限位置尝试0/90度，仅返回候选中心/角度/边界、not_found及预算统计，不改变文字内容或字号，也不负责来源绑定和原生写回。调用方必须核对清单完整；部分未安置时把其原位置字框作为障碍再次复核，不能直接将部分新位置写到未移动文字上。候选仍要结合真实字体/原生读回和图像确认，不保证全局最优。

非重叠不代表位号归属清楚。显式成对配置 `association_max_distance/association_advantage` 后，真实本体距离筛查会拒绝“距自身过远、且另一器件明显更近”的候选；两项默认 `None`，完整本体映射必需，有限非负阈值在工程中声明。统计区分几何阻挡、归属拒绝及预算耗尽；此规则不放宽任何几何间距。对未找到的位置保留或恢复真实已备份属性时，必须以当前来源产生明确显示差量，核对所有未移动文字并单列原有潜在冲突，不能称其已通过质量检查。实际图像仍需确认邻件误认、排布顺序和极性标记。

默认保留未安置位号。用户明确允许省略放不下的丝印后，可逐对象记录候选失败依据，并用 `action:"omit"` 与非空 `reason` 关闭对应Designator的显示；逻辑属性、器件和网表不删除。省略清单与仍可见文字分别统计，回读后确认实际不可见，仍可见文字按原间距复查。封装外框、极性、1脚和接口标记不在此自动省略范围。省略不是找到合法位置，也不令原生DRC或 `full_board_accepted` 自动通过。
