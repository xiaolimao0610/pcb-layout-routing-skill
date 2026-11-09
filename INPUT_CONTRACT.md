# 输入与阶段契约

2026-11-08新增严格字段：`routing.checkpoint`（布尔，默认false）、`routing.escape`、`routing.local_repair`、`placement.functional_cells`（默认空列表）及 `options.automation`。完整嵌套字段、默认、预算、组合限制和续算工件格式见 [自动化与制造铜补证契约](AUTOMATION_AND_GERBER.md)，该文件是本契约的组成部分；未知字段仍报错。Gerber的 `geometry_context` 是从同源实际文件构建的Python上下文，不是可写入board的信任标记；`round_caps/final_image` 的严格能力范围也见该契约。

本轮修复新增的严格字段及旧模式边界见 [REVIEW_FIXES.md](REVIEW_FIXES.md)。字段包括 `routing.geometry_search.sampling`、`routing.geometry_search.bend_penalty`、`routing.negotiated_grid_cache`、`routing.negotiated_bend_penalty`、`placement.rudy_method`、`placement.rudy_widths` 和 `component_tags` 下的 `halo_nets/halo_net_types`；未知字段仍拒绝。

## Board

顶层只接受 `units,board_bbox,components,nets,tags,routes,rules,metadata`；必须显式mil。规则来源和图元必须完整，metadata不自动产生额外检查。

元件：`id,x,y,rotation,layer,pads`；安装面1顶/2底，body_polygon与courtyard_polygon为局部点；或使用中心在定位原点的width/height。底层局部x取反后旋转和平移。焊盘自己的local_rotation先于元件变换，actual layer不被安装面覆盖。

焊盘：padId/net/shape/localX/localY；RECTANGLE、ELLIPSE、OVAL用localW/localH或width/height；CIRCLE用diameter；ROUNDRECT还需radius；POLYGON用自身局部points/holes。绝对坐标只逆变换中心，不能由AABB逆推出真实形状。多层焊盘需实际hole及plated:true；NPTH不导通。

`pad.hole` 可为旧版数值圆孔直径，或 `{type:"ROUND",diameter,offsetX:0,offsetY:0,rotation:0}`、`{type:"SLOT",width,height,offsetX:0,offsetY:0,rotation:0}`。SLOT是圆端胶囊的完整长宽，不是椭圆或等效圆。数值均为mil，rotation为度；先在焊盘自身轴内旋转孔并施加偏移，再应用焊盘local_rotation/localX/localY，最后应用器件镜像/旋转/平移。钻头最小尺寸取槽孔短边，环宽按完整孔轮廓计算；孔在全跨度扣铜并参与孔距、板边、异网钻铜和孔壁连通检查。不能用短边圆替代槽孔。

铜层物理顺序由rules.copper_layers给出，默认[1,2]；12=MULTI，11=板框。layer/layers同时存在必须相容，钻孔跨度不能漏中间铜层。实时写回当前只支持普通全通孔，不能把内层指定跨度伪装成全通孔。

routes：traces为单层圆端直线（net/layer/sx/sy/ex/ey/width）；vias/shield_vias为圆形金属化孔（net/x/y/hole/diameter/layers，可有metadata来源注释）；holes为圆形NPTH（x/y/diameter/layers）；copper_pours为net/layer/filled_polygons，后者是世界坐标points和holes；keepouts为世界points及层、clearance，可用nets/net_types选择受限目标，多个过滤取交集。

原生泪滴以 `source_kind:"Teardrop",locked:true,metadata.source_only:true` 导入copper_pours。每个region保留完整 `native_path`，`points`仅是按命令顺序提取端点、去末重复点的chord预览，不是实际曲线铜。路径为闭环 `[x0,y0,"L",x1,y1,...,"ARC",角度,终点x,终点y,...,x0,y0]`；角度以度计，正为逆时针，ARC要求非零且绝对值小于180。严格解析、有限段数/顶点预算、简单chord环、圆段与其他边/圆段分离等证明条件均须满足。曲线洞和不能证明的弧交叠拒绝。独立验收和构造共用保守inner/outer几何界，不以chord或普通采样代替真实铜；旧points/holes模式不变。

原生规则不会由默认值自动补齐。应将已核对的原生规则写入sidecar，例如真实孔边距对应 `rules.hole_to_hole_clearance`；原生DRC是额外验收。来源相同但输入规则变强时需重新求解，不可沿用按较弱规则生成的目标。

`rules.component_pad_clearance` 为非负有限mil，默认0。不同器件且有共同实际铜层的电气焊盘，不论同网、异网或无网络，都禁止钻孔前保守外包络接触/重叠；正值进一步要求至少该装配净距。失败前缀为 `COMPONENT_PAD_CLEARANCE`。同一器件内焊盘、没有共同铜层的顶底焊盘，以及正常同网走线/铺铜接入焊盘不受此新增项限制，仍执行原检查。它不代替器件本体/庭院、阻焊桥或返修空间检查，也不更改网络电气间距；原生DRC允许同网接触不能覆盖此项。实际项目需依据装配工艺显式设置正净距，不把默认0视为推荐焊接距离。

真实来源在metadata.source。`metadata.board_usage` 与 `metadata.source.usage` 可为physical或test_only。测试板框必须明确test_only；该标记在计划中保留，Python及JS入口均拒绝写回/制造。无板框应补真实设计证据；器件外扩框只能用于离线诊断。来源指纹是 `sourceFingerprint`：仅规范化DOCHEAD里的client/updateTime/version，其他设计字段/记录仍参与摘要，文件字节SHA-256不采用该规范化。

nets示例：`{"SIG":[{"compId":"J1","padId":"1"},{"compId":"U1","padId":"1"}]}`。有名焊盘必须全部出现且一一对应；命名铜岛必须物理连接该网端子，GND同样检查。

## Options

阶段5的接续状态与多轮比较是独立文件/入口，不扩展下表solver options。`project-state.json`字段、文件SHA256及证据范围严格按[PROJECT_STATE.md](PROJECT_STATE.md)；独立workflow policy的methods、max_rounds、max_seconds、stagnation_rounds、objective、min_relative_improvement、target_nets、target_regions按[WORKFLOW.md](WORKFLOW.md)。未知字段仍拒绝，不把关注网络变成删减网表的许可。

装配和丝印质量使用独立 `pcb_assembly.py --board --options --output`，配置不写入下表 solver options 或 board.rules；`check_assembly(board, options)` 的严格字段、来源和对象覆盖要求见 [质量契约第6节](QUALITY_CONTRACT.md#6-装配净距与丝印质量筛查)。显式高件局部包络及焊接净距、同层丝印线/保守字框和丝印到焊盘净距只形成质量发现和候选，不改变旧算法默认或硬DRC。所有工艺下限由工程提供；缺少来源、覆盖清单或保守字框依据为 unknown，不能通过。

固定封装轮廓可显式合并为 `kind:"outline"` 的保守GeoJSON Polygon/MultiPolygon，保留holes与分离岛，以source_ids覆盖原生图元清单，禁止凸包补洞或忽略曲线。合并对象内部不互查，文字对该轮廓及轮廓对焊盘仍检查；未转换或无保守性证据的对象保持unknown，不能声称全部原生丝印已筛查。

独立候选 API `pcb_silkscreen.place_labels(labels, obstacles, board_polygon, *, gap, max_seconds, max_candidates)` 采用有限位置和0/90度方向放置完整字框，不删位号、不缩字、不写原生。调用方提供同面完整障碍、真实来源及保守字框证据，并核对未移动文字仍在原位的冲突；具体输入、预算和局限同见质量契约。它没有扩展 solver options；输入到其他入口的未知字段仍报错。

该独立 API 可传 `method="greedy"|"reassign"`（默认后者）、`search_radius=420`（mil，240～900之间30的整倍数）、`max_reassign_nodes=100000`、`max_reassign_depth=4`、`max_reassign_rounds=3`、`max_target_nodes=6000`，所有步骤共用 `max_seconds`。`greedy` 保留原有限位置分配，`reassign` 有界扩大未找到文字候选并置换阻塞文字，失败分支回滚，不降低已有覆盖数。可选 `owner_shapes={器件ID:真实本体Shapely几何}` 必须覆盖全部标签所属器件；文字不能压在其他器件本体上，自家本体仅作次选。这些是新增独立整理入口的参数，不改变布局/布线默认。

可选 `association_max_distance` 与 `association_advantage` 默认为 `None`，必须成对指定为有限非负距离，并提供完整 `owner_shapes`。若候选字框到所属器件实体的距离大于前者，且大于最近其他器件实体距离加后者，则拒绝该槽位；不能仅靠非重叠或轻微距离评分把位号放到别的器件旁。阈值来自本项目的可读性要求，不写死通用数值。`stats.association_rejections` 记录被归属约束拒绝的候选数，未找到仍如实保留；此筛查不证明丝印归属绝对清晰，最终仍须看实际原生图。未传参数保持旧候选行为，未知参数继续报错。

只读采集入口为 `eda/capture_silkscreen.py --window-id --document-uuid --fingerprint --output-dir`，原生指纹前后稳定才输出完整属性与源码；没有字框 getter 时明确列 unavailable。`eda/prepare_silk_geometry.py --capture-dir --footprint-capture --output` 以本工程已验证封装源码和实时姿态转换顶层固定丝印，保留原路径及笔宽、来源摘要，拒绝未经证实的底面镜像和未知曲线。与候选及质量检查配合时必须核对完整来源和 unsupported 清单，不能把估计字框当作实测字体轮廓。

独立原生显示入口 `pcb_native_silkscreen.py --window-id ... apply --plan ... --audit-dir ...` 使用 `source:{document_uuid,fingerprint,usage?}` 和非空 `changes`（最多1000项）。默认动作 `place`：每项必含 `id/parent_id/value/x/y/rotation/font_size/line_width`，仅可额外含 `action:"place"`、`align_mode`（3为左下，5为居中，省略为5）；几何须有限、字号/笔宽为正，角度限-90/0/90/180，保持可见性。用户明确允许省略后，`omit` 项恰含 `id/parent_id/value/action:"omit"/reason`，reason须为非空字符串，只关闭位号显示，不接受位置、字号或其他显示字段。两个动作均核对原value且不得改名；ID全计划唯一，未知字段拒绝，只支持已有顶层可见、未锁定的Designator属性。封装轮廓、极性、1脚和接口标记不在省略范围。默认保存，Python `apply_labels(..., save=False)` 可显式不保存但必须如实报告。写前备份、一次派发、status查询及全部属性/无关原生记录的双向证据见 [EDA契约](eda/README.md#独立原生位号显示整理)。不新增solver option，不改变原生DRC、来源与整板验收语义。

| 部分 | 字段/默认 |
| --- | --- |
| placement | enabled=true, method="legacy", optimize=false（true时主动降低加权线长并考虑已声明约束）, grid_step=RuleConfig.grid_step, rotations=[0,90,180,270], max_candidates=4000, seed=0, movable_ids=全部可动ID, regions={id:[xmin,ymin,xmax,ymax]}, repair_attempt=0 |
| routing | enabled=true, method="sequential", occupancy_grid=false（negotiated 省略时为 true）, occupancy_max_cells=2000000, occupancy_max_seconds=10.0, occupancy_retries=3, grid_step=RuleConfig.grid_step, width=各网默认max(10,规则/网络最小线宽), via_diameter/via_drill=明确配置或RuleConfig默认生成尺寸, via_cost=网标签或规则默认, via_in_pad_penalty=0（盘中孔软偏好，见下文）, max_expansions=50000, max_passes=3（sequential）, max_seconds=120, max_net_seconds=30, net_order=显式网络顺序, net_order_strategy=fewest_terminals/input/ground_last, ground_nets=[], ground_strategy=route/plane_then_drc（后者必须显式地网列表）, allow_vias=true, diagonal=true, topology_orders=有序端子表；协商与优先方向选项见下表 |
| shielding | enabled默认按nets是否非空, nets, ground_net=GND, spacing=40, offset=30, diameter=24/hole=12（规则默认）, layers=物理全栈, minimum_vias=1（每目标网） |
| pours | 列表，每项 net、单层layer，可选世界outline、clearance、min_area；clearance只能增加本程序规则净距 |
| cleanup | 可选对象；省略不执行后处理。字段、默认与逐对象保护见[阶段4契约](QUALITY_CONTRACT.md) |
| return_path | 可选对象；实际铺铜后执行双向回流几何复核，plane_then_drc延后到原生重铺。严格字段见[阶段4契约](QUALITY_CONTRACT.md) |
| max_iterations | 3，范围1–20；布局反馈轮数，不是无界优化 |

所有未知生成参数都会失败。固定、fixed_pose、locked和已有铜保护不能被movable_ids覆盖。regions限制整个body/courtyard/焊盘，不只定位点。改变固定器件朝向也算改变姿态。

### 优先避开盘中孔

`options.routing.via_in_pad_penalty` 是有限非负数，默认 `0` 保持旧行为；正值为每个新盘中孔增加一次路径代价。它与线长代价、过孔基本代价共同比较，是“可以时尽量避开”的软偏好，不是禁止条件。布尔值、字符串、负数及非有限数拒绝，未知字段继续报错。示例罚分120只是可调整的搜索权重，不是120 mil净距或工艺要求。

判定使用过孔孔洞与钻孔前无孔贴片焊盘的真实形状、局部旋转、底层镜像及共同铜层的保守外界；相交或相切计入偏好，同网也计算。每孔只加一次罚分，不按接触焊盘数量叠加；孔洞有正间隙、仅铜环相接时不罚。器件本身有孔的PTH/NPTH焊盘不计入此偏好，其孔距等原检查保持。

顺序/协商A*、有限快捷逃线、选网续算与局部信号修复共用该偏好。快捷候选可在有限额外比较内优选盘外位置，预算结束保留已有可用候选；仍可能选择更短或唯一可行的盘内路线。独立补地/屏蔽孔算法不自动使用该选项，已有孔不因偏好自动移动或删除。启用后 `stats.via_in_pad` 列出新增盘中孔数量、罚分及保留提示，`affects_acceptance=false`。这些信息不单独形成DRC错误，也不证明工艺合格。

本项不改变 `via_ok`、占用栅格阻塞、`_commit`、`check_board`、`check_routed_nets` 或原生DRC的硬条件。有限搜索不能保证消除全部盘中孔或保持所有工程的布通率；应在相同预算下比较实际结果。将[示例配置](examples/via-in-pad-preference.example.json)中的选项合并到工程solver配置，保留已有约束，不放入board的 `rules`。

### 方法选择与占用栅格

`placement.method` 的方法名为 `legacy`、`analytic_sa`；`routing.method` 的方法名为 `sequential`、`negotiated`。原有 `legacy` / `sequential` 路径保留，阶段2新增 `negotiated`，阶段3新增 `analytic_sa`。其他值和未知字段均报错，不自动替换方法。省略方法及栅格开关保持旧算法。`placement.method` 同样适用于 `pcb_compact.py --placement-config` 中的布局选项。

| 路由字段 | 类型、默认与限额 | 语义 |
| --- | --- | --- |
| `occupancy_grid` | 严格布尔值；`sequential` 默认 `false`，`negotiated` 省略时为 `true` | `true` 时使用占用栅格筛选 A* 网格候选；不能用字符串或数字代替布尔值。协商模式依赖栅格，显式 `false` 报错 |
| `occupancy_max_cells` | 整数，默认 `2000000`，范围 `1..100000000` | 单个栅格中全部物理铜层走线图与一张全叠层过孔图的总逻辑格数上限；不包含精检反馈集合与临时数组的额外内存。超限拒绝构建，不静默增大网格或忽略障碍 |
| `occupancy_max_seconds` | 有限正数，默认 `10.0` 秒 | 每次构建或增量更新的合作式时限，同时受原总、单网和适用的单对时间预算限制 |
| `occupancy_retries` | 整数，默认 `3`，范围 `0..20` | 顺序模式中，每次端点对首次候选精检失败后的额外阻塞反馈与重搜上限；协商模式中，每网跨轮累计允许的精检阻塞反馈上限。`0` 表示首次精检失败即停止该重搜流程 |

格点坐标沿用 `routing.grid_step` 与原路由坐标系，尺寸单位仍为 mil。走线图按目标网络、层和实际线宽构建：异网实体铜按线宽的一半加适用净距膨胀，钻孔、禁区、板边及其各自间距分别处理；保留网络类型、禁区过滤、槽孔与真实板内切孔的语义。通孔图另按孔径、铜环及完整物理层栈处理铜距、孔距和钻孔影响，不使用 `trace_layers` 缩短孔跨度。顺序栅格模式下，同网新增铜提交后，增量接口只重算受影响包围盒内的缓存格；不同目标网络/线宽使用独立栅格，换板、换规则或改变网格规格须重新构建。协商模式按当前单网构造视图建立端点间搜索栅格，动态共享资源另行维护，不保证复用顺序模式的增量缓存。

格点判定不是连续路径或物理连接证明。A* 找到路径后仍由原 `_commit` 精确检查各段、全栈孔及真实铜组连接；失败候选不提交，冲突格被标阻塞后按 `occupancy_retries` 有限重搜。起终点逃线和非网格候选继续使用原几何检查。最终 `check_board`、`check_routed_nets`、完整端子要求和原生 DRC 不受开关影响。精检失败时不能降低间距、删除端子、隐去网络或仅凭查表结果宣称通过。

所有栅格构建、更新与重搜耗时计入原路由时限，单次 Shapely/NumPy 调用不能被合作式时限强行抢占。各次 A* 仍受 `max_expansions` 限制；顺序模式按 `max_passes` 重试，协商模式按下述 `negotiated_max_iterations` 迭代，两者均保留总/单网时限。超出格数、构建时间或重搜预算时如实报告本次未找到，不将栅格未完成区当作空白，不推断连续几何无解。

路由 `stats` 保留 `expansions`（A* 展开数）与 `elapsed_seconds`（本轮总耗时），新增 `astar_seconds`（A* 搜索及候选整理耗时，扣除计入统计的栅格构建/更新时间）、`search_seconds`（含端口、可见性候选、A*及栅格的搜索累计耗时）、`occupancy_build_seconds` / `occupancy_update_seconds`（构建/更新累计耗时，包含失败操作已发生的耗时）、`occupancy_cells`（成功构建的单个栅格逻辑单元数峰值）、`occupancy_updated_cells`（成功更新重算的逻辑单元累计数）和 `occupancy_rejections`（精检拒绝候选次数）。`occupancy_retries` 在顺序模式中统计实际额外重搜次数，在协商模式中统计已执行的精检阻塞反馈次数；末轮反馈后可能已无下一轮预算，不能把它等同于实际协商重布轮数。实际轮次看 `negotiated_iterations` / `negotiated_batches[].rounds`。`occupancy_builds` / `occupancy_updates` 记录成功构建/更新次数，`occupancy_refined_cells` 记录精检反馈新增阻塞点累计数。未触发构建等路径时，对应附加计数可能省略，按零读取。格数口径包含全部物理铜层走线图和一张全叠层过孔图，不代表实际峰值内存；时间单位为秒，计数为整数。以实际结果为准，不从预算上限反推已执行工作量。

`pcb_continue.py --config` 同样校验并传递方法和栅格选项；选择 `negotiated` 后还传递协商、拓扑及方向选项。未知路由字段不会被静默过滤，旧顺序续算的拓扑处理行为本阶段未扩展。续算的 `grid_step/max_expansions/max_seconds/max_net_seconds` 继续由原命令行参数控制；其局部检查点仍是预览，不能把新算法启用或所选网络连通当成整板验收。

这些选项不改变 UUID / `sourceFingerprint` 来源绑定、写前备份、一次派发、几何双向读回或 `full_board_accepted` 的语义。`plane_then_drc` 仍保留完整地网端子，先完成信号，再原生铺铜、按 DRC 局部补地及复查。

### 阶段2：拥塞协商、多端拓扑与内层方向

设置 `routing.method="negotiated"` 启用 `pcb_negotiated`。固定输入的焊盘、旧线、旧孔、旧铜区、钻孔、禁区和板边仍是硬障碍；协商只对本次计算生成的候选线路与过孔进行拆线重布。中间候选允许争用资源以计算拥塞，不能作为写回结果。资源共享消除后，仍须逐网通过原 `_commit` 精确复核，再执行原独立验收；粗格误差、孔距及真实接触不因协商而豁免。

| 路由字段 | 类型、默认与限额 | 语义 |
| --- | --- | --- |
| `negotiated_max_iterations` | 整数，默认 `20`，范围 `1..1000` | 每个协商批次最多迭代轮数；不再套用 `max_passes` 重新运行整个协商流程 |
| `negotiated_history_increment` | 有限正数，默认 `1.0` | 共享资源的历史拥塞累积强度 |
| `negotiated_present_factor` | 有限正数，默认 `1.0` | 当前资源争用的初始惩罚强度 |
| `negotiated_present_growth` | 有限数，默认 `1.3`，不得小于 `1` | 当前争用惩罚随迭代增长的倍率 |
| `steiner_max_candidates` | 整数，默认 `128`，范围 `1..10000` | 每轮 1-Steiner 改进比较的候选点上限 |
| `steiner_max_rounds` | 整数，默认 `2`，范围 `0..100` | 逐次加入一个改进分支点的轮数上限；`0` 只生成直角 MST |
| `preferred_direction` | 对象，默认 `{}`；JSON 键为实际内层 ID 的标准十进制字符串，值为 `"horizontal"` 或 `"vertical"`；Python 调用也接受整数键 | 只设置 `rules.copper_layers[1:-1]` 中的真实内层；未知层、外层、重复等价层号和错误值拒绝。不自动开启被 `trace_layers` 排除的层；`sequential` 携带非空映射会报错 |
| `nonpreferred_penalty` | 有限数，默认 `1.5`，不得小于 `1` | 协商寻路在已设置方向的内层，对非优先方向乘此罚系数；斜线含非优先分量时同样受罚，未设置方向的层及换层动作不加此项惩罚 |

节点代价采用 `(基础代价 + 历史拥塞 h_n) × 当前惩罚 p_n`。在第 `i` 轮，`p_n = 1 + negotiated_present_factor × negotiated_present_growth^(i-1) × 当前其他网络占用数`；本网络重布前已撤出其候选资源。轮末对共享格累计 `h_n += negotiated_history_increment × (共享网络数 - 1)`。同网树枝的重复占用只计一次。一个动作覆盖多个格胞时，取其中最大的代价；基础代价含线长权重或换层代价，已设置方向的内层还应用方向罚系数。

动态资源使用按层格胞：走线中心几何按半线宽及适用净距份额膨胀，再加格胞半对角的保守覆盖余量；通孔在完整物理叠层上同时计入铜环和孔间净距。由此可能把连续几何中尚有余量的两条候选判作共享，属于有限栅格的保守搜索限制，不能降低设计规则来消除它。零共享只表示栅格候选已消除资源争用，不等于连续几何、完整连通或原生 DRC 已通过。若代价超出有限数值范围，按预算失败报告，不输出无穷统计或继续假装完成。

无显式 `topology_orders` 的多端网先以各真实铜组的网格代表点比较曼哈顿长度最小生成树，再在这些横纵坐标组成的 Hanan 网格上进行有界 1-Steiner 改进，按得到的连接关系布线。每次比较加入一个分支点，仅保留代理树长确实下降的候选；候选或轮数用尽时保留目前较好的拓扑，不宣称已遍历全部可能。代理树长不等于最终绕障后的实体铜长。Steiner 点仅是候选分支位置，不能作为虚构端子或导电铜；实体路径和接入仍须证明。已有同网连续铜可以在路径内部的可采样位置接入，不限定为轨线端点，但端口搜索仍有预算，不是连续空间穷举；悬空旧铜也须最终连接真实端子，不会被删除以制造连通。

显式 `topology_orders` 保持相邻端子的无旁路铜链语义，优先于自动树拓扑；`CHANNEL_SPEC` 的宽度、允许层、过孔及代价继续作用于各网。`trace_layers` 仍只限制新走线，全通孔仍检查整个物理叠层。`ground_strategy="plane_then_drc"` 继续暂缓明确列出的地网，不参与全局地网协商，也不删除其任何端子或原铜。

`pours_before_net` 将本次路由分为有依赖的批次：先协商普通网络，再按原稳定顺序逐个生成所列网络的铜区并连接该网。已完成批次的候选在后续批次中成为固定输入，不跨批次拆线；这保留“根据先前实际异网铜生成铺铜”的原有语义。整个调用及各网跨批次、跨轮累计使用 `max_seconds` / `max_net_seconds`；候选、格数、扩展数与精检反馈也受各自显式限额限制。预算耗尽报告本次未找到与剩余争用/未完成网络，不声明无解或输出可写回的冲突铜。

协商统计保留通用的节点、搜索时间和栅格构建统计；`negotiated_iterations` 为所有批次实际执行轮数之和。`negotiated_batches` 逐批记录 `nets`、`converged`、`exact_rejections`、`rounds` 和 `topologies`。每轮的 `shared_resources` 是被不同网络争用的格胞数，`conflicting_nets` / `failed_nets` 分别为有争用和搜索未完成的网络；`present_factor`、`history_total`、`history_cells` 记录当前惩罚强度与历史累积，精检失败的网络另见 `exact_failed_nets`。不要把共享格胞数写成实际 DRC 违规数。

每网 `topologies` 记录采用的 `method`；自动树另报 `mst_length`、`steiner_length`（mil 的曼哈顿代理长度）、`steiner_points`、`candidates_tested`、`steiner_rounds` 和 `candidate_budget_reached`。这些是候选拓扑搜索证据，不能代替最终铜长度、实体连接、无旁路检查或原生 DRC。迭代过程中出现零共享但精检失败时，仍未收敛；成功须同时满足批内真实连通、明确拓扑及最终入口的完整适用验收。

### 阶段3：布局关系、代价和搜索预算

`method="analytic_sa"` 显式启用全局力导向、合法化与模拟退火；旧 `optimize` 开关只控制 legacy 的主动优化。`enabled=false` 不搜索，仍要求原几何及区域合法。固定、铜保护、`movable_ids`、`initial_poses`、`regions` 和旋转允许列表的原语义保留。legacy 接受共享的地网/网络权重；非空 `clusters`、`edge_components`、`align_groups` 或 `feedback` 要求 analytic_sa，避免静默忽略关系。

原理图导入后的初始化增加以下严格字段，完整使用说明见 [分区初始布局](COLD_START_LAYOUT.md)。`initialization` 默认 `"preserve"`，可选 `"auto"` 或 `"region_pack"`，后两者要求 `method="analytic_sa"`。`auto` 在初始几何/区域非法时生成区内起点；`region_pack` 明确重新排布可动单元；`preserve` 不运行新初始化。已有固定/旧铜保护和明确刚体簇不被覆盖。`functional_groups` 默认 `[]`，每项只接受 `{id,components,region}`；唯一非空组名、不重复真实器件 ID 的非空列表和有限正面积矩形 `[xmin,ymin,xmax,ymax]`。器件不得重复属于多个功能组；区域与已有 `regions`/`ZONE_ANCHOR` 取交集，空交集拒绝。组内相对位置可变，与 `clusters` 的刚体语义不同。未知字段和值仍报错。这些配置直接经过原 `place_components`、pipeline 和 compact 入口，不需要另写工程专用排布脚本。

legacy 也执行 `max_seconds` 的协作式时限检查，包括候选生成、逐件检查、回溯和优化；候选池按需构造并缓存，不预先构造所有器件的大池。不能把候选检查次数等同于所有几何运算次数。单次几何库调用不可抢占；预算耗尽只保留已经完整检查的候选，没有完整候选则失败，不输出部分板。

| 新增 placement 字段 | 默认及范围 | 用途 |
| --- | --- | --- |
| `global_iterations` / `global_step` | `40`，整数 `0..10000` / `0.25`，有限 `(0,1]` | 固定锚点力导向轮数与平移比例 |
| `sa_temperature` / `sa_cooling` | `10`，有限正数 / `0.85`，有限 `(0,1)` | 初温和逐温度层降温率 |
| `sa_temperatures` / `sa_steps_per_temperature` | `12`，整数 `0..10000` / `30`，整数 `0..100000` | 温度层数与每层动作数上限 |
| `max_seconds` | `30`，有限正秒数 | 单次布局的合作式总时限；原 `max_candidates=4000` 同时限制全部合法性候选检查 |
| `cluster_intra_steps` / `cluster_intra_radius` | `20`，整数 `0..100000` / `10`，有限非负 mil | 每簇内部微调步数及相对该次宏观最佳姿态的最大位移 |
| `rudy_grid` | `[12,12]`，两个 `1..256` 整数，最多65536格 | 板外接矩形的水平/竖直分箱数 |
| `rudy_capacity` | `1`，有限正数 | 将估算需求密度归一化的容量参数，不是实际可布线证明 |
| `rudy_peak_limit` / `rudy_overflow_limit` | `1` / `0`，有限非负数 | 峰值与总溢出同时不超过阈值才标记拥塞达标 |
| `ground_nets` / `ground_weight` | `[]`，真实且唯一的网名 / `0.05`，有限非负数 | 声明布局地网及降权比例；不猜网名 |
| `net_weights` | `{}`，真实网名到有限非负数 | 显式覆盖该网络的最终布局权重 |
| `cost_weights` | 见下文，有限非负数 | 各代价的乘数，未提供项沿用默认值；未知项报错 |
| `clusters` / `edge_components` / `align_groups` | `[]`，见下文 | 完全显式的器件、引脚和对齐关系 |
| `feedback` | `{failed_nets:[],hotspots:[]}` | 明确的失败网及局部拥塞区域；最多256个输入热点 |

默认网络权重为 `net_tags.alpha_length`（省略为1）；若网络属于 `placement.ground_nets` 或声明 `net_tags.type="GND"`，再乘 `ground_weight`；`net_weights` 显式覆盖优先。名字恰好叫 GND 不再被布局算法自动降权。流水线未显式填写布局 `ground_nets` 时继承 `routing.ground_nets`，显式空列表不会继承，但网络类型标签仍适用。`ground_last` 的排序同样采用明确列表/类型；原硬验收器的分类和间距计算未改变。

以下结构中的标识符必须替换成当前板真实 ID，不能根据位号猜测。嵌套未知字段、重复器件/簇 ID、不存在的引脚或网络都报错。

```json
{
  "clusters": [{
    "id": "本地电源簇",
    "components": ["真实IC器件ID", "真实电容器件ID"],
    "relations": [{
      "kind": "decoupling",
      "source": {"compId": "真实电容器件ID", "padId": "真实电容电源焊盘ID"},
      "target": {"compId": "真实IC器件ID", "padId": "真实IC电源焊盘ID"},
      "weight": 1
    }]
  }],
  "edge_components": [{"component": "真实接口器件ID", "edge": "right", "target_distance": 20, "weight": 1}],
  "align_groups": [{"id": "接口对齐", "components": ["真实接口器件ID", "另一接口器件ID"], "axis": "x", "orientation": 90, "weight": 1}]
}
```

`clusters[].components` 至少一个成员，器件不得属于两个簇；`relations` 可省略，`kind` 只接受 `decoupling` / `crystal`。两端为不同的实际焊盘且器件都属于该簇，距离为变换后焊盘中心的欧氏距离。关系 `weight` 默认1、有限非负。宏观力导向只平移簇，合法化和退火允许整簇刚体旋转，但每个成员最终角度必须属于 `placement.rotations`；不缩放簇内距离。任一成员不可动时宏观阶段全簇固定，内部微调仍可移动其他成员，固定件始终不动。原始已合法姿态可以保留，旋转列表仅限制新增旋转动作。

`edge_components` 每个器件至多一项，`edge` 为 `left/right/bottom/top/nearest`；前四项对板外接矩形边计算器件包络距离（top为最大y），nearest 对实际板边界计算最短距离。`target_distance` 默认0 mil、`weight` 默认1，均有限非负。凹形板若需指定某段实际边，应继续使用硬区域/固定姿态，不把外接矩形边当成实际接口面。

`align_groups` 的 `id` 唯一，至少两个不同器件，`axis` 为 `x/y/both`；x 表示使各 x 坐标接近（竖向轴线），y 类似。`orientation` 可省略；省略时取该组首个器件的当前朝向作为比较基准，显式值为有限角度、规范化到0..360。`weight` 默认1、有限非负。不同组可重叠，代价累加。

逐项原始代价：HPWL 是网络权重乘引脚包围框宽高之和；RUDY 在不小于线宽的网络包围框中均布 `权重×曼哈顿线长×线宽`，按格内交叠面积累积并除以 `rudy_capacity`。线宽取 `max(10,规则最小线宽,网络width/min_width)`；`peak` 为最大格需求，`overflow` 为各格 `max(需求-1,0)` 之和。该代理没有扣除障碍、板内切孔和具体层的可用通道容量，不替代真实布线。

去耦/晶振项为明确关系的加权距离和；边距项为实际距离与目标距离的绝对差；朝向奖励为 `-weight×(1+cos角差)/2`，对齐奖励为 `-weight/(1+偏离组均值/grid_step)` 之和。`RELATIVE_POSE` 超距及 `REPEL_FIELD` 欠距平方分别累计为罚项；硬约束仍由原检查器强制执行，不因权重为0而取消。

`cost_weights` 默认 `hpwl=1, rudy_peak=100, rudy_overflow=100, decoupling=1, crystal=1, edge=1, orientation=10, alignment=10, relative_pose=10, repel_field=10, feedback=10`。完整评分的 `initial_cost/legalized_cost/final_cost` 各含 `raw`、`weighted`、`weights`、`total`、`rudy`、`net_hpwl`，总值为各加权项之和。距离用mil，平方罚项用mil²，RUDY/奖励无量纲；不同量纲通过显式权重比较，不解释为物理能量。若初始化已经完整验收，但完整评分因预算耗尽未完成，可返回该合法候选并将 `final_cost`、`rudy` 标为 `null`、`cost_evaluation_complete=false`；不得将未知指标写为0、参与总分优劣排名或声称拥塞达标。公开布局入口仍独立报告可计算的HPWL；后续布线与原生验收照常进行。

`feedback.failed_nets` 为唯一真实网络列表；`hotspots` 每项 `{bounds:[xmin,ymin,xmax,ymax],weight:1,nets:[]}`，边界有限且面积为正，空 nets 表示所有网。反馈项累加失败网加权HPWL及热点内选定网络的平均需求密度；相关器件获得更高的局部动作采样权重，其余器件仍可调整。流水线下一轮读取结构化失败诊断及 RUDY 热点，失败网无命名热点时使用实际引脚包围框。先完整保留原显式区域，再纳入最新区域和历史区域，最多256个；相同边界/网络/权重去重，`placement_feedback_stats.retained/omitted` 分别报告各类保留/省略数，完整失败网列表始终保留。`repair_attempt` 不再通过改变 analytic_sa 的 seed 代替这些反馈，legacy 的既有重试方式保持。

`candidate_checks` 包含初始几何、固定子集、合法化与退火检查；`sa_steps`、`sa_temperature_levels`、`sa_actions`、`cluster_intra_actions` 报实际搜索，动作不适用或被硬规则拒绝也不代表成功移动。每个结果最终再做原 `check_board`。预算内找到合法候选可返回当前最佳并标记 `budget_exhausted`；没有合法候选则结构化失败并报告“未找到”，不证明无解。总时限不能抢占单次 GEOS、完整验收或最终报告操作，实际耗时可略超限。固定 seed、输入、版本、环境和计数预算可复现；墙钟截断时 `deterministic_completion=false`，不能承诺相同最终姿态。

### 自动尺寸契约

`pcb_compact.compact_layout(...,auto_size={...})` 与非空手工尺寸候选互斥；命令行 `--auto-size` 与 `--size` 互斥。自动模式使用 analytic_sa，显式 legacy、关闭合法化或 `contract_centers=true` 均报错。无尺寸开关时仍报错，不擅自改板框。所有自动选项未知字段报错：

| auto_size 字段 | 默认与范围 | 命令行 |
| --- | --- | --- |
| `target_occupancy` | `0.65`，有限 `(0,1]` | `--target-occupancy` |
| `aspect_ratio_range` | `[0.7,1.6]`，有限正数且升序 | `--aspect-ratio-range MIN MAX` |
| `aspect_ratio_steps` | `5`，整数 `1..64`，非退化范围至少2 | `--aspect-ratio-steps` |
| `area_iterations` | `8`，整数 `0..32`，每种比例二分轮数上限 | `--area-iterations` |
| `max_candidates` | `48`，整数 `1..512`，总尺寸候选数 | `--size-max-candidates` |
| `max_seconds` | `60`，有限正秒数 | `--size-max-seconds` |
| `max_area` | 原板多边形面积，有限正mil² | `--max-area` |
| `area_tolerance` | `0.02`，有限 `(0,1)` | `--area-tolerance` |

搜索下界为全部器件包络面积总和除以目标占用率，上界为 max_area；下界超过上界时报告未找到，不静默扩板。双面包络相加是保守搜索估计，不能视为物理最小面积证明。先交错测试各比例上下界，再交错二分，完整保存已测试候选；可行判据是几何合法且 RUDY 达标。离散启发式搜索并不保证面积上的单调性。

每个自动尺寸默认快速配置 `global_iterations=10,sa_temperatures=2,sa_steps_per_temperature=5,cluster_intra_steps=0,max_candidates=800`；显式布局值优先，单候选时限还受总时限剩余量和份额限制。排序依次为几何合法、拥塞达标、面积小、HPWL小；若只有几何合法但拥塞未达标的候选可保留作提案，必须报告未达标。结果记录搜索区间、停止原因、候选及 RUDY，始终 `full_board_accepted=false`，不生成实板板框写回计划。原固定件、来源、旧铜拒绝和禁止复用样板的规则保持。

`placement.initial_poses`默认为`{}`，可将真实器件ID映射为完整`{x,y,rotation,layer}`起点。所有器件的`layer`必须保持原值，翻面及焊盘层变换尚未验证。它只在布局启用时接受，不能覆盖不可移动、固定或接触外部既有铜的器件，仍经过同一独立几何合法化。附属孔只随已启用`footprint_via_movement="rigid"`的父器件同面移动，所有候选均同步计算并检查孔。两种方法的 `initial_hpwl/seeded_hpwl/final_hpwl` 分别报告实时来源、种子与最终线长指标，统一沿用原焊盘内界代表点口径。analytic_sa 的 `initial_cost` 表示应用种子后的搜索起点，并标记 `initial_cost_scope="after_initial_poses"`；分项代价用焊盘内界形心，不规则焊盘上可能与旧 HPWL 指标不同。移动统计与最终写回差异始终相对原始实时来源。

`routing.pours_before_net`默认为`{}`，可显式映射`网络名 → 非空pours请求列表`；每条请求的net须与键相同，字段和检查与`pours`一致。所列网络稳定排在所有普通网络之后，即使显式net_order或重试调整顺序也保持此分组。在每个所列网络布线前，先根据已生成的真实异网铜和孔计算已锚定的实体填铜，再连接该网所有剩余铜组。内层必须已有接到声明端子的物理铜锚；同名悬空孔不能作为锚。生成及独立几何验收耗时计入该网和本次路由预算，每轮重试都从原快照重新生成，不累计失败轮次的铜。

这些铜区作为新增Fill进入最终写回计划，来源仍为原始实时导出快照；路由结束及最终计划均要求全部端子连通并满足全部硬规则。铺铜可能被异网线路/孔分割，不能只凭网络名或平面面积宣布接地完成。多个不同网络按此方式铺铜时，先生成的铜仍会阻挡后续异网过孔，程序不会自动重挖孔或放松间距。`allowed_layers`约束实际铜及贯穿孔的全部物理层，不能用排除某内层的方式只禁止该层走线却继续允许穿过它的过孔。

已存在图元尺寸必须真实，绝不能用默认过孔参数补已有via缺失的孔径。默认过孔尺寸只用于**新生成**图元。global width小于某网最小值时明确失败，不静默改变用户指定值；不指定width则按网计算下限。

## 六类明确约束

rules在任务顶层constraints列表，operator与字段区分大小写；组件引用为真实ID，不能猜位号与ID等价。未知字段/引用/算子失败。编译结果保存到board.metadata.pipeline_constraints，最终用全流程入口重新检查。

| operator | 字段 | 实际检查 |
| --- | --- | --- |
| ZONE_ANCHOR | component,region:[xmin,ymin,xmax,ymax] | 真实本体/courtyard/焊盘整体包含于区域，同时注入placement.regions |
| RELATIVE_POSE | source:{compId,padId},target:{...},max_distance,same_layer | 真实变换后的焊盘中心距离；same_layer检查实际铜层是否有交集 |
| REPEL_FIELD | source:componentID,target:componentID,min_distance | 真实本体间隔，跨面也检查；是明确几何隔离，不是电磁场求解 |
| CHANNEL_SPEC | net及至少一项min_width/allowed_layers/allow_via/alpha_length/via_cost/diff_pair/length_group，或完整current_a+temp_rise_c+copper_oz三参数 | 注入网络规则并独立验收；新增字段定义见下文及[阶段4契约](QUALITY_CONTRACT.md) |
| TOPOLOGY_ORDER | net,ordered_pins:[{compId,padId},...] | 恰好覆盖该网全部端子，连接相邻端子；独立切除每个中间端子铜后证明其不能被旁路，并核对先后两侧 |
| COMPANION_GEN | nets,ground_net,spacing,offset,diameter,hole,layers,minimum_vias | 配置真实地孔生成，最终验计数、尺寸/跨度/跟随位置和实际接地；不是EMC验收 |

COMPANION_GEN的全部字段需明确。多条伴随规则仅在参数一致时合并nets；与手写options冲突会失败。CHANNEL_SPEC、区域与拓扑同样拒绝矛盾配置，不按“最后一条覆盖”悄悄删规则。

TOPOLOGY_ORDER是本几何模型的无旁路铜链，不能代替电路级串联元件语义。TVS等并联保护器不应误编译成强制串联路径；不要仅按自然语言排列端子就称电路拓扑正确。

### 阶段4：严格新增字段

`CHANNEL_SPEC.diff_pair` 恰为 `{p,n,gap,width}`，`length_group` 恰为 `{name,tolerance}`；前者声明两真实网的铜边间距和同宽耦合，后者在各成员分别声明组内最大长度差。`current_a`、`temp_rise_c`、`copper_oz` 必须同时给出，以可追溯的IPC-2152数据拟合计算下限后与已有下限取max；未给出保持旧规则。不能用缓存报告替代实际几何重算。完整类型、模型范围、例子和不支持情况见[阶段4契约](QUALITY_CONTRACT.md)。

`routing.diff_max_candidates=128`、`length_max_candidates=128`（均为1..4096整数）、`length_max_amplitude=200`（有限正mil）用于有界中心线候选及蛇形补长，共享原总/每网时限。未知字段继续报错。默认差分/等长仍为双端同层无支路；显式diff_layer_change可生成有限成对全栈孔换层并独立检查。跨层完整长度、length_group及铜区路径仍不可冒充已验证，详见密集区域契约。

### 密集区域新增严格选项

`routing.geometry_search`、`routing.fanout`、`routing.shove`、`routing.diff_layer_change` 均为严格嵌套对象，enabled默认false。完整字段、默认、有限预算、相互限制及入口支持见 [DENSE_ROUTING.md](DENSE_ROUTING.md)，该说明是本输入契约的组成部分。未知子字段继续报错。默认method/occupancy与旧输入保持兼容；没有新增Java或其他依赖。

推挤仅在sequential启用，明确保护原铜，只允许白名单LINE替换及有原输入差异证据的本轮新线调整。写回计划仍采用现有remove/add模式，旧线变化需额外证明同宽连通与来源/网表/几何不变量。选网续算只支持geometry_search，新整封装/推挤/差分阶段须使用完整solve。

`cleanup`、`return_path` 的字段、默认、报告分类及渲染独立CLI配置在[阶段4契约](QUALITY_CONTRACT.md)集中定义。它们是可选阶段，不变更legacy/sequential/occupancy_grid=false的默认。选网续算遇到差分、等长或这两个质量阶段配置明确拒绝，避免只完成连通却漏执行已声明质量步骤。PNG生成不表示AI已查看或整板验收。

## 独立验收与失败

先验证输入/端子，再按真实轮廓构造；净距与板边使用保守外界，接触使用可证明的内界。模型不会用中心线、包围盒或同网名称代替实体铜。

构造中间阶段允许尚未布通。默认route模式路由结束要求完整基础连通和拓扑，只延期伴随孔计数；最终阶段不得延期任何已声明规则。plane_then_drc是明确的信号阶段：保留完整网表和几何，只暂缓指定地网的搜索与连通完成，之后必须原生铺铜、DRC定点补地和复查。钻孔会切掉其跨度上的铜，空铜不是合法导体。

同名悬空铜不会作为铺铜锚点，只有已物理连接声明端子的铜组才是锚。新铺铜隔离岛可被丢弃；用户原有铜不会被删除来制造通过。写回分片与原铺铜必须几何等价，孔洞不会被填平。

完整生成失败返回passed=false、board=null、writeback_plan=null。完整离线成功status=offline_passed，仅涵盖支持的规则和完整输入。plane_then_drc信号阶段成功则返回signal_stage_passed=true、status=awaiting_native_ground、passed=false、full_board_accepted=false，并产出preview_only的board/plan；CLI此时退出码0表示所请求阶段成功，不表示整板完成。原生DRC及真实客户端验证另看对应收据。

`review-native-copper`读取原snapshot、原计划及完整失败audit（status/plan/result），不接受仅adapter结果或pending job。audit.plan须与输入计划完全相同；原计划ID、来源、严格rollback恢复且无需重导出均须匹配。全部新泪滴和原有铜一并验收，同时按原snapshot的本体、焊盘、端子、tags、保存约束、孔和禁区复验；原孔/禁区不能变。只生成全新plan_id和离线结果；泪滴保留在target而不额外创建Fill，最后仍须实际严格回读、原生DRC及保存后导出回验。审计失败、未知来源、未恢复、需重导出、不同规则或任一硬检查失败都不能生成可用计划。

路由失败的阶段diagnostics区分NO_LEGAL_ESCAPE、SEARCH_BUDGET、TIME_BUDGET、GRID_NO_PATH、PHYSICAL_CONTACT_FAILURE、GEOMETRY_HARD_FAILURE，并保留端子、层、尺寸、候选和阻碍采样。NO_LEGAL_ESCAPE仅指有限候选集没有通过，不证明工艺不可布；时间/扩展预算耗尽也不证明无解。总时限及每网时限在一轮route_board内跨重试累计；时间检查是合作式，不能抢占单次GEOS或独立验收调用，也不含其他流水线阶段。显式ground_last将ground_nets指定的网稳定排到最后，仍要求全部连接，不自动把GND当作已铺通。

## 选网续算与预览

`pcb_continue.py --board --config --output-dir`读取完整快照或检查点，`--nets`和`--exclude-net`二选一，按真实网名限定续算范围。原始完整网表、端子、铜、孔和禁区保持；输出不是删减过的局部板。`--preview`显式允许已有几何错误与部分进度，保留失败诊断；无法严格解释的原生曲线只在寻路视图中被保守不可导电障碍替代，输出仍保留原文铜记录。

结果使用`preview_only:true`、`full_board_accepted:false`、`selected_connectivity_passed`、`selected_nets/excluded_nets/failed_nets/not_attempted`区分范围。`board.json`和`writeback-plan.json`是预览检查点，不是完整验收产物。普通严格apply拒绝preview_only；明确授权的`pcb_preview.py apply --ack-preview`使用真实来源与原文备份，不进行完整目标或原生DRC验收。等待后只读status，不因超时重复派发。

顺序续算保留逐个精检连接的检查点；协商续算不会保存临时共享铜，所有所选批次完整返回后才保存新的候选。无法解释的原生铜只在保守搜索视图中被替代，输出仍保留原始铜区及其完整区域列表，再追加本次新生成的铜区；不能把经过删减的搜索视图覆盖回原工程。

`routing.width`显式指定时覆盖所有网络的自动选宽；小于网络`min_width`会失败。各网需不同宽度时通常省略全局width，由网络约束决定。

`routing.trace_layers`是当前物理铜层中的非空、唯一ID列表，只限制新走线层，不限制通孔穿过参考地层。普通通孔仍采用完整rules.copper_layers，并执行全跨度碰撞/钻孔检查。网络allowed_layers继续限制所有铜（含过孔），不为预留地层放宽已有规则。结果分别报告trace_layers及through_via_layers。

`pcb_continue.py --calculation-only`支持还未写回的独立布局计算：不生成writeback-plan.json，metadata和报告均为calculation_only/requires_live_rebase。必须先完成实板relayout和fresh export，核对真实几何与计算输入后再基于新来源生成计划；旧proposal指纹不能手改成本次source。


## 独立紧凑板框与样板标准

pcb_compact.py的默认输入是当前工程完整canonical board及1..32个明确[width,height]候选，单位mil。独立比较尺寸、面积、器件包络占用率和weighted_hpwl_mil，先选择通过几何检查的最小面积，再比较HPWL。它只改可动器件中心/姿态，不缩放局部封装、焊盘和钻孔；固定姿态、regions、孔和禁区仍保留。样板默认仅提取面积、占用率及明确工艺标准，不能因为同拓扑就把其器件坐标或姿态当成计算输入。

该函数针对经过授权清理并重导的当前工程未布线输入，不会私自删除已有铜。默认禁止复制样板工程、器件坐标、走线或铜区；需要用户明确授权对应复用范围才能使用任何参考移植入口。输出metadata.compact_proposal记录当前计算来源、原板框及requires_native_outline_update_and_reexport；metadata.preview_only=true，routing_verified=false，global_optimum_proven=false。几何候选不是可直接写回的新来源，实板改框后必须重导。

compact_layout的reference默认仅产生reference_metrics；allow_reference_poses默认false，对应CLI的--reference只读统计。只有用户明确授权坐标复用后才可设置allow_reference_poses=true/--allow-reference-poses，同时必须提供reference。结果reference_positions_used如实标明是否使用参考姿态；传入开关不代替用户授权，也不授权复制工程、走线或铜区。

pcb_relayout.py接受最新native inspection、独立placement-proposal和显式cleanup；proposal必须显式reference_positions_used=false，缺失或true均拒绝。cleanup恰含lines/pours/vias/outline_line_ids；最后一项必须覆盖当前1..32条实际原生板框LINE，其他项只能是明确允许清理的独立铜ID。计划outline_mode=replace，先删除全部旧板框再创建目标四边，避免临时交点引起自动合并/切分。目标仅支持无切孔矩形，全部元件必须完整覆盖且不翻面。原生source记录与getter共同证明清理对象，附属Via禁止独立删除；派生TEARDROP/POURED残留会失败并要求cleanup，不直接删除派生对象。

relayout保存原文备份并固定job_key，一次派发后只查询status；部分失败须fresh inspection和全新计划，不重放。回读姿态角度按模360，坐标容差0.0001mil仅容纳原生序列化精度；层/网络/孔径严格比较。附属孔的ID/网络/孔径保留不等于位置已验收，成功仍requires_reexport，需fresh canonical export核对附属孔世界坐标后接续布线。

## 10 mil默认与明确网络宽度

新路由未显式给width时采用max(10, minimum_trace_width, net.width, net.min_width)。显式更宽约束优先。具体项目网名及参数放在[独立示例配置](examples/foc-widths.example.json)，不得按电压名称推导行业统一线宽；未来明确例外可通过配置表达，并验证真实间距。

原生宽度策略为{default_width:10,net_widths:{实际网名:宽度}}，尺寸mil。pcb_native_copper.py只增加不足的LINE/ARC宽度，保留原更宽线路、路径与弧角。宽度调整后需要原生铜刷新和DRC，不把宽度API返回成功等同间距通过。

## 原生地网阶段契约

routing.ground_strategy可为route（兼容完整离线流程）或plane_then_drc。后者要求ground_nets为非空、唯一、当前完整网表中的显式名字，不猜相似网名。它不与同一地网pours_before_net/topology_orders混用，pipeline也不同时接受离线pours/shielding请求。完整nets、端子及源铜保留；信号阶段检查全部硬几何和非地实际铜连通。

signal_stage_passed成功后，deferred_ground_nets列出地网，pending_stages依次是native_pours/native_drc/ground_repair/native_drc_recheck；metadata.native_ground_workflow保存策略与阶段。pcb_continue.py使用该配置时默认暂缓地网。点名缺口使用pcb_ground_repair.py；明确--nets选择地网会搜索整个所选网络，不能用它替代此局部修复阶段。full_board_accepted仍为false。

原生铜spec包含layers（非空[{layer,net,name?,pour_id?}]）、ground_nets和可选edge_clearance。新区域从真实板框内缩，结果分裂或有孔时拒绝简单整层创建；刷新使用真实pour_id并保留原路径、分区和孔。层号和电源网都必须来自实际设计。prepare计划绑定最新实际source；写信号、改宽、复制或改框后必须重新取来源。

可选`copper_keepouts`为`[{points:[[x,y],...],layers:[实际铜层号,...],provenance:"明确区域及退让依据"}]`。每项必须是有限mil世界坐标组成的有效简单polygon，layers非空、无重复且属于真实叠层，provenance非空。prepare按层从实际板框inset减去这些区域，不改板框、器件或source；合法结果必须是单个无孔Polygon，贴边凹口可用，空集/多岛/孔洞拒绝。计划保留禁铜声明，apply前再次拒绝任何覆盖禁铜内部的创建边界。已有pour_id的刷新不能应用新增禁铜区，涉及相同层时明确拒绝；仅影响其他层的声明不改变该刷新边界。此字段不自动移动或删除已有信号铜。

原生apply执行重建实铜、区域读回、可选保存并默认完整DRC。operation success、saved、非空铜区、ground_connections_reported_clear及native_drc_passed各自独立；空区域、未分类错误、未解析目标不能被当作地网完成。DRC目标使用真实对象ID及原生getter世界坐标；native_display_position只保留作报告。短连接/过孔补连后重新铺铜、复查。复杂原生曲线保留原始路径，不用chord伪装为受支持的canonical铜。

### 预览写后几何状态

`pcb_preview.py`的`created`只统计API返回成功的创建调用；`applied`表示请求写入流程完成，`saved`独立表示保存结果，`success`不代表几何或DRC通过。最终必须单独读取`geometry_readback_passed`，pending时为null。写前完整铜LINE减指定删除加全部planned traces，与写后铜LINE用adapter的双向覆盖等价核对；原生拆分/合并可通过，移动、缺失、额外铜、网名/层/线宽变化不可通过。全部通孔用网络、坐标、孔径、外径和完整层栈进行一对一匹配，保留的孔也在范围内；移动器件时必须提供完整`target_board.routes.vias`目标，以涵盖附属孔随动。

`geometry_readback`保存前后来源指纹、实际/预期计数、缺失和额外覆盖及过孔差异，`scope:all_copper_lines_and_through_vias`明确范围；不包含完整铜区/器件验收。几何不符或读回异常使`geometry_readback_passed:false`、`needs_geometry_repair:true`且CLI非零，但不会清除已经成功的applied/saved，也不回滚或重放。须重新读取实板来源并准备只修差异的新计划；full_board_accepted仍false。

### 显式原生线段修复

`pcb_preview.py prepare-trace-patch`接受完整当前board和同源proposal。proposal含精确`source`、`source_segments`、`replacement_segments`及非空`recommended_primitive_operations`；操作的expected/replacement集合必须与两份清单完全一致，可改变执行顺序。source_segments须逐ID匹配当前板实际未锁定铜LINE，不能只替换旧fingerprint。

输出计划的`trace_operations`仅接受三种结构：`{operation:"modify",id,expected,replacement}`、`{operation:"create",replacement}`、`{operation:"delete",id,expected}`。expected恰含id/locked:false/net/layer/sx/sy/ex/ey/width；replacement恰含net/layer/sx/sy/ex/ey/width，数值均有限、width正数。每个原ID只能使用一次；modify保持net/layer/width不变；涉及的每个net/layer/width组合必须在替换铜中保留，create不得引入别的组合。此模式与components/add/remove变更互斥，并要求`--allow-trace-replacement`。

apply保留原文备份、一次timer及逐操作日志；派发前和第一次写入前完整核对实际expected、锁定状态和铜层。原生保存舍入导致旧expected不匹配时在任何写入前拒绝，不放宽到四位舍入等价。运行后用完整before减全部expected加全部replacement进行双向铜覆盖及全体过孔读回；只代表补丁实际几何与请求一致，不代替碰撞验算、重铺铜或DRC。操作失败或偏移时保留现场，读取新来源后重新计算剩余delta。

### DRC局部接地输入

pcb_ground_repair.py接受完整`--board`、同一source的`--drc-targets`及`--copper-evidence`。DRC必须具有native_check_complete/endpoint_source_stable=true，且未知分类和未解析连接为空。只处理能映射到真实完整Pad/Via ID的ground_targets；既有Via（含附属孔）保持原位置、尺寸、归属，只以实际环铜实体为起点补连。其他目标列为unsupported，不被暗中忽略。原生诊断条目、去重端点及独立缺口数分别记录；不能把两端条目当成两个独立缺口，也不能在缺少对应关系时自动除以二。已证明接入主平面的另一端可作为落点而不重复加孔；这种局部证据不能把尚存的原生报错标成已完成。

铜坐标证据为对象：`source`与当前板UUID/fingerprint完全一致，`units:"mil"`、`coordinate_frame:"native_pcb_data_origin"`、`poured_ids`精确覆盖快照内全部source_kind:Poured ID、非空`method`和非空`evidence`数组。evidence应保存当前客户端实际已填铜与官方原生几何接口的逐项坐标/角度互证，或其他可复核原生单位转换证据。不能仅写“已验证”而无实际证据，不能依据板框包围盒猜乘10。getSource与getSourceStrictComplex的文档差别仅是外层数组，不提供单位转换保证。

当前实铜入口还必须包含`coordinate_profile`，与`board.metadata.native_pour_geometry`完全一致。profile包含真实现场source、coordinate_scale、mode:local_preview_only及calibration。默认校准method:native_poured_via_anchors保存source_native完整源、pour_id/poured_id/region_id、raw_paths、全部via_api读数及1e-7 mil匹配容差；至少8个非共线异网VIA须与两半圆孔中心唯一对应。全部原生VIA坐标/尺寸/网名先与API逐个互证，再对所有可识别圆孔检验唯一正比例及零平移；同中心歧义、多比例、共线或不足锚点拒绝。生成方式是pcb_native_poured_capture via-anchors→export。

同ID VIA坐标除原浮点一致外，仅接受源码精确等于getter四位小数mil编码且差值不超过0.00005mil的情况。使用互证后的源码坐标拟合，原始getter保持不变，并输出逐字段 `getter_position_quantization_proofs`；它是特定序列化证明，不是放宽几何容差。孔径、盘径、网络、类型和来源仍需匹配，Python独立复核与JS适配器采用相同语义。

可选method:native_poured_convert_to_fill保存source_before/source_converted完整原文、poured_id/region_id/fill_id、raw_paths和tolerance_mil，仅逐项形状/角度完全对应且XY一致时有效。已观察转换会偏移边界和增加圆角，不得放宽容差把改变后的铜当作原铜。所有方法均核对快照内每个当前Poured的原生记录/API路径/转换mil路径；仅非空evidence不能通过。API的Poured ID可能等于父Pour ID；只有原生记录身份及全部region逐项匹配后才使用独立的原生POURED ID，并保留api_primitive_id。普通严格adapter事务仍拒绝native_compound_path。

参考面选择函数`_reference_region(board, net, *, max_nodes=200000, max_contacts=1000000, max_seconds=60.0)`验证现有铜的物理锚链，不进行全网布线。节点为扣除真实钻孔后的逐层独立保守内界多边形；同层相交面积须大于`contact_tolerance**2`，跨层须属于同一实际同网镀孔、层均在该孔层栈内，且两侧内界与孔外界边界的接触长度均大于`contact_tolerance`。同名、外界相交、非镀孔、仅点/边接触以及同对象不同岛均不得合并。

函数返回已证明接到物理端子的面积最大候选区域；预算不足则抛出PipelineError并如实报告尚未证明。预算不能抢占单次底层几何运算。结果`reference_regions[net]`保存`name/layer/terminal/area_mil2/bounds/proof`，proof包含锚链节点、逐边接触面积或孔壁长度、访问数、接触检查数、耗时和预算。这些预算是该验证函数的显式参数，不是新solver配置或CLI字段；未知配置字段继续拒绝。任何局部候选仍须原生重铺、DRC与完整验收。

入口默认radius=120mil、grid=10mil、max_candidates=1024、短线至少10mil、过孔24/12mil且跨全部实际铜层。以已接实际端子的一个连续Poured保守内界作为参考，检查真实固定铜、钻孔及禁区后，证明目标实体经新短线/孔正面积接触参考铜；新孔不切既有固定铜。采用原生端点坐标，输出四位小数的新线仍按实际几何校验，已有完整路径不重复添加。此过程不逐候选重算全地网；完整网表与未列入DRC端子保持，铺铜来源及曲线另外验证。未找到有限局部路径即unresolved，不放宽规则或转为全局GND搜索。输出board/writeback-plan/result仅是局部计算结果，requires_native_pour_rebuild及requires_native_drc_recheck保持true。

`--local-grid-nodes`对应`local_grid_nodes`，默认0关闭，合法值为整数0至100000。开启后仅在有限直连候选失败时，对同一原生端点执行有界局部网格搜索；半径沿用radius且不得超过1000mil，步长沿用grid，保留全栈孔与固定铜检查。成功项的`local_grid_search`或未解决项的`local_grid_searches`记录`expanded_nodes`、`bounded_radius_mil`、`grid_step_mil`、`node_budget`、`budget_exhausted`和`timed_out`；这是搜索范围与终止证据，不是原生连通验收。参数只增加有限局部候选，不删除端子、不忽略固定铜、不全局搜索地网。

可选`--rebuild-spec`为`{source, rebuild_pour_ids:[原生Pour父ID,...]}`，source须与同次板/DRC完全一致；ID必须精确指向source_kind:Poured的parent_id，不能填区域ID或Fill ID。仅列出的派生Poured从固定碰撞视图暂缓，原始铜仍保留作重铺前接触证据和来源保护。未列Poured、固定Fill/Teardrop/trace/pad/via/drill不豁免。输出candidate_awaiting_rebuild、deferred_clearance_pour_ids及ground_connected_after_rebuild:null；这些候选不能作为整板离线DRC通过结果，须重铺全部列出铜区并完成原生DRC。

`pcb_local_repair.py --mode joint-ground`另需显式`--net`、`--target-pad-id`、`--blocking-trace-id`，以及相同的完整board、同源DRC/铜坐标证据和可选rebuild-spec。目标必须是原生DRC已解析的地焊盘，阻挡物必须是实际未锁定、宽度至少10mil的非地网LINE。`--options`接受`grid_step`（默认2.5mil）、`radius`（地路径默认220mil）、`signal_radius`（信号绕行默认160mil）、`max_nodes`（每次搜索默认40000）、`max_ground_candidates`（默认200）、`max_seconds`（搜索阶段默认120秒）、`width`（默认10mil）、`via_diameter`（默认24mil）、`via_hole`（默认12mil），以及下述`deferred_teardrop_ids`。半径不得超过1000mil，节点1..100000，地落点候选1..1000，宽度及孔环须满足实际规则。时间/节点预算限制搜索，不能抢占单次几何运算。

`deferred_teardrop_ids`可显式给出该父LINE经来源验证的派生泪滴ID子集，重复、未知或无关ID拒绝；省略时暂缓全集。未暂缓的派生铜继续作为保守碰撞障碍，不能因此把其存在当成写入后的连通保证；原信号端子组证明另外剔除该父线的派生铜全集。报告用`expected_removed_derived_teardrops`保留可能随父线消失的全集，`deferred_derived_teardrop_ids`记录规划暂缓的子集，`affected_connectivity_excludes_derived_ids`记录不参与连通证明的全集，最终仍检查原生派生铜读回并重铺、DRC。

该联合入口只替换点名LINE并新增局部地线/全栈孔；原生refs、网络和层证明属于该LINE的派生泪滴才暂缓，其他固定铜保持。它验证原信号每个端子组未拆开、地焊盘经新线/孔正面积接入已锚定连续Poured；不搜索整个地网。提案含`expected_removed_derived_teardrops`、原/后信号组数、参考铜及端子、接触面积、搜索统计；`requires_native_pour_rebuild`、`requires_native_drc_recheck`、`requires_derived_copper_readback`为true，`full_board_accepted`为false。找到候选才输出board与writeback-plan；所有结果都保留proposal，不直接改EDA。写入后须按实际源核对派生泪滴消失/再生成，并重铺、保存、DRC确认。

`pcb_native_poured_geometry.scale_native_paths(paths, factor)`只执行有证据的正比例XY转换，ARC角度保持不变，不推断factor。`verify_native_path_mapping(native_paths, mil_paths, factor)`逐项核对XY、命令和角度；第二份路径必须已通过原生Fill/source证据证实mil，两个未知单位API互相比对不构成单位证明。局部规划填铜区域可使用`{native_compound_path:[ring1,...]}`代替points/native_path，但不能同时提供竞争的points/holes表示。每环支持L和非零且绝对值严格小于360度的两端点ARC，包括两个半圆组成的孔；文档定义的隐式直线闭合保留。保守界由受控弓高采样和内缩/外扩生成，嵌套必须方向交替且包含关系在误差界内确定；未知语法或不确定拓扑拒绝。原生ULP闭合差只在已有数值误差预算内影响中间采样，原路径保留。当前严格EDA adapter canonical签名尚不接受此compound表示，仅在来源完整的局部补地计算/授权预览流程使用。


### 原生直线铜笔画

来源完整的局部实铜快照可使用`{"native_stroke_path":{"points":[[x1,y1],[x2,y2]],"width":w}}`。坐标及宽度单位均为mil，恰含两个不同端点，宽度为有限正数；不接受未知字段、竞争的points/holes/native_path表示或退化线。此表示不是新增solver选项，也不改变旧算法默认值。

`native_stroke_region(paths, width, factor)`只接受原生`[[x1,y1,"L",x2,y2]]`和已取证的正长度比例，将坐标、笔画宽度一并换算，不自行推断比例。`native_stroke_bounds(region,tolerance)`返回平头内界、方头外界，并计入数值误差；输入非法或精度不足时抛出GeometryError。未知端帽不用于证明额外连接，曲线笔画不能以弦替代。普通严格adapter写入事务仍不接受该局部几何表示。

原生POURED的`fill=false`且正笔宽是实际导电笔画，不可作为辅助边界过滤；当前仅支持上述直线情况。每个区域保存原始ID、path、fill和lineWidth，并与完整原文中的strokeWidth逐项一致。单位须由原生格式的长度约定、实际同源几何校准和适用的原生规则交叉证明；界面单位枚举、两个未知单位API互比或板框尺寸不能替代校准。

`validate_native_pour_snapshot`将每个`native_source_record`的type/id/payload与完整`calibration.source_native`中的唯一POURED记录严格比对，整对象ID全集也必须相等；转换实验方法使用完整source_before。不能同时改动局部来源副本、API宽度和mil几何来绕过检查。新快照的实心区域API fill/lineWidth同样核对，旧版未保存这两个字段的实心区域仍按完整原文验证。重建铺铜改变原始记录后，必须重新采集同源完整校准源，再导出。
