# 密集区域布线升级使用说明

本轮修复新增的严格字段及旧模式边界见 [REVIEW_FIXES.md](REVIEW_FIXES.md)。字段包括 `routing.geometry_search.sampling`、`routing.geometry_search.bend_penalty`、`routing.negotiated_grid_cache`、`routing.negotiated_bend_penalty`、`placement.rudy_method`、`placement.rudy_widths` 和 `component_tags` 下的 `halo_nets/halo_net_types`；未知字段仍拒绝。

本版增加可选的混合几何搜索、整封装扇出、有限级联推挤及成对通孔换层。它们只生成候选，原 `check_board`、`check_routed_nets`、原生DRC、来源绑定和写回证据继续判定最终结果。默认仍为 `placement.method=legacy`、`routing.method=sequential`、`occupancy_grid=false`，四个新增能力均默认关闭。合成用例通过不证明所有工业板可布或优于其他路由器。

## 选择流程

1. 固定实际板框、完整网表、制造线宽/间距/孔尺寸和可用层；读取适用原厂数据手册、封装球图及参考布局。不能因密度高自行缩线、放宽间距、增加层数或把参考地层当作空闲信号层。
2. 用 `pcb_dense_preflight.inspect_channels(board, net, routing_options, max_pairs=1000, max_seconds=5)` 检查目标网在同封装焊盘最短间隙处的线宽/两侧净距。结果只描述该局部横截面；不能据此断言其他位置不可绕行。`budget_exhausted`、`completed_scan` 和 `whole_board_feasibility` 必须如实保留。
3. 完成布局后，对显式球阵列执行扇出，再布差分和普通信号。先尝试几何搜索；需要腾挪时开启有白名单的推挤。地仍按 `plane_then_drc` 原生铺铜后局部修复。
4. 每阶段记录目标、不可改变条件、指标及预算；无改善按既有工作流停止/换方法。失败是“当前预算或支持范围内未找到”，不是无解证明。
5. 在候选通过离线检查后，沿用原生一次派发、写前备份、双向几何读回、保存、重铺和DRC流程。新模块不直接连接EDA。

格式示例见 [dense-routing.example.json](examples/dense-routing.example.json)。下列对象均放在 `options.routing`，未知字段、错误类型和非有限数继续报错；长度单位mil。

## 1. geometry_search：连续几何与有界细化

| 字段 | 默认与含义 |
| --- | --- |
| enabled | false |
| max_seconds | 10，正秒；单次几何图搜索时限，同时服从原总/单网时限 |
| max_nodes | 512，整数2..8192，有限节点上限 |
| max_edges | 12000，整数1..2000000，有限边精检上限 |
| max_anchors | 16，整数1..256，每个铜组的真实接入点上限 |
| margin | 100，局部搜索范围外扩距离 |
| boundary_samples | 64，整数4..512，单条边界的采样数量上限 |
| refine_factors | [2,4]；粗网格失败后的步长除数，最多3个唯一有限数，须大于1且不超过16；[]关闭 |
| max_refine_expansions | 10000，每次细化节点上限，整数1..100000，实际还取原max_expansions较小值 |

算法由真实铜锚点、膨胀后的实际障碍边界建立连续坐标可见图，用有界Dijkstra选择路径。图边仍采用水平、竖直、45度折线和全叠层通孔，逐边执行精确碰撞检查；不因粗格点缺失直接淘汰几何路径。节点/边截断不意味着物理无路。

顺序模式先试几何候选，局部图预算耗尽后保留原搜索后备；粗搜索失败后可在端子组邻域内按指定因子细化。细化使用精确逐边检查，不分配整板高分辨率占用图，且共享原总/单网预算。整批精检拒绝后跳过重复几何候选，不无限重试同一路线。

协商模式只把不占用当前或历史竞争资源的几何路径作为捷径，出现竞争仍交给原PathFinder；当前协商模式没有顺序模式的局部细网格回退。零栅格冲突仍需实体铜精检。统计见 `geometry_searches`、`geometry_seconds`、`geometry_refinements` 及后备计数。

## 2. fanout：显式球阵列整体扇出

| 字段 | 默认与含义 |
| --- | --- |
| enabled / technology | false / "through"，只支持全叠层通孔 |
| max_seconds | 30，正秒，同时累计原单网预算 |
| max_candidates / max_backtracks | 2000 / 32，候选上限1..1000000，回溯上限0..100000 |
| packages | []；启用时1..32个封装 |

每个 `packages` 项必须声明 `component`、`balls`、`escape_layers`；可选 `order`（outer_first默认或inner_first）、`escape_margin`（默认40正mil）、`via_offsets`（默认[0.5,1.0]）。`via_offsets` 是相对球间距的落孔偏移倍数，1..16个唯一的0..4有限数，0允许测试盘内落点。

`balls` 为至少2×2的矩形数组，单边最多128，空位用null；非空位置须为当前器件4..4096个真实唯一padId，并完整覆盖全部焊盘。真实局部坐标必须符合均匀矩形球阵列，不猜封装、网名或缺失球位。底层镜像和旋转用实际变换。

```json
{
  "enabled": true,
  "packages": [{
    "component": "当前器件的真实ID",
    "balls": [["A1", "A2"], ["B1", "B2"]],
    "escape_layers": [15, 2],
    "order": "outer_first",
    "escape_margin": 40,
    "via_offsets": [0.5, 1.0]
  }]
}
```

上例ID和层号必须换成当前板真实数据；`escape_layers` 还须在该网允许的信号层内。按球圈安排引出顺序、目标层、落孔与封装包络外出口，已选通道以实际铜占据，后续球避让，失败在副本内有界回溯。孔和线尺寸来自原规则与网络配置，不能自动缩颈或生成HDI孔。

启用前比较关闭fanout的基线。原路由已布通且线长/孔数更好时保留原候选；扇出增加出口铜和过孔，可能更慢、更长，不应对所有BGA无条件启用。

差分、等长、有序拓扑及延期地网按球位列明暂缓，不漏端子。`via_in_pad_penalty` 沿用软偏好：优先避免盘内孔，必要且合规时仍允许。报告中的 `escaped` 只证明焊盘到出口，不证明出口已接远端；`completed` 不代表整板验收。单阶段报告不污染整板preview标记。

## 3. shove：有限几何级联推挤

| 字段 | 默认与含义 |
| --- | --- |
| enabled | false；当前仅支持sequential，negotiated组合明确拒绝 |
| movable_trace_ids | []，可改变的原生LINE显式白名单 |
| protected_trace_ids | []，额外保护对象，保护优先 |
| move_generated_traces | true；总开关启用且有原输入证据时，可调整本轮新增线 |
| max_depth / max_objects | 4 / 8，分别为1..32和1..128的整数，递归深度及联动对象上限 |
| max_candidates / max_seconds | 256 / 10，候选上限为1..100000整数，局部时限为正秒 |
| max_displacement | 100，正mil，最大偏移 |

根据实际铜和净距计算局部偏移，保留原线两端接入，必要时递归推动其他可动线。当前支持单层直线变为有限折线；过孔、分支节点、与孔/铺铜接触的原线、附属/锁定/派生铜和受保护差分/等长/拓扑/延期地网保持保护。没有实现移动过孔或任意分支网络的推挤。

本轮新增线的可动身份只来自入口保存的原输入差异，不从调用者伪造的标签推断；原输入没有ID的铜也受保护。内部临时ID不得进入原生计划。统计中 `native_source` 与 `candidate_generated` 分开，后者不表示需要删除任何原生对象。

推挤在候选副本中原子完成，失败不返回部分修改；检查所有受影响网络的原端子组保持、原网名/层/宽度及完整几何。修改旧铜后重建占用缓存，不把删除当成仅追加更新。废弃重试轮的替换证据与当前成功轮分开。

整板计划生成只对明确白名单允许旧LINE替换；保留其他旧线，独立证明同网同层同宽路径连接原两端，保护原来源、网表、焊盘、规则和板框。不得同时移动器件。沿用原适配器已有remove/add、来源指纹、备份和双向读回机制，不放宽原生检查。原始输入已有预览限制不解除。

## 4. diff_layer_change：成对全栈孔换层

字段默认：`enabled=false`、`max_candidates=128`（1..4096）、`max_transitions=2`（1或2）、`lead_distance=120`正mil、`search_step=20`正mil、`max_seconds=10`正秒。配置与原 `CHANNEL_SPEC.diff_pair:{p,n,gap,width}` 配合，不改变其字段或线宽下限。

同层候选未找到或四端无共同铜层时，显式启用后尝试成对孔、一或两次换层、直线耦合中心通道和短端部引出。两网逐网原_commit后，按实际LINE/VIA重建路径，复核孔一一配对、尺寸、全栈跨度、逐层耦合、原端子连接、分支、自接近和返回同层时的跨段铜捷径。不以生成metadata作验证证据。

输出平面长度、每层路径、配对孔和不耦合长度；当前没有真实层厚测量，孔段/总长度为未知，`length_complete=false`。跨层length_group及蛇形补长仍拒绝；不把二维等长当作完整长度或电气延迟相等。`return_path_verified=false`，还需铺铜后实际参考面和回流复核。不是任意障碍跨层迷宫、HDI或阻抗求解器。

## 入口、基准与默认决策

完整使用入口仍为 `pcb_pipeline.py solve`。`pcb_continue.py` 仅可传递geometry_search；启用整封装扇出、推挤或差分换层明确拒绝，不能静默漏掉阶段。所有新增选项仅生成候选，原生结果未知不重放。

`pcb_dense_benchmark.py` 用独立子进程比较冻结旧版与实验版，不连接EDA。用例文件结构为 `{"cases":[{"name":"用例名","board":"相对用例文件的板JSON路径","options":{},"upgrade":{}}]}`；可选constraints。upgrade只能改变四个新算法对象、method、occupancy_grid，不能改变工艺、总预算或网表。用例应包括简单、密集四/六层、BGA、差分及失败反例；保存真实输入、源码指纹和多次重复结果。

```powershell
python -B -X utf8 pcb_dense_benchmark.py --suite 路径\suite.json --baseline-dir 路径\冻结旧版 --repeats 3 --output 路径\新对比报告.json
python -B -X utf8 -m unittest discover -s tests -v
```

失败调用不返回候选时，部分完成网络数保持null，不填0或假定全连；独立报告离线板/信号范围、地网暂缓和native_drc=not_run。线长和过孔数是完整返回候选的几何统计；没有采集进程峰值内存时保持未知。失败更快不能称作布线更快。

旧版关闭新选项的兼容回归、反例拒绝和真实工程基准都通过后，才讨论默认切换。合成定向用例不能替代代表性的工业密集板评估；FreeRouting只作为独立可选对照，不引入为运行依赖，也不依赖其完成差分跨层。
