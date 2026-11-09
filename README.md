# PCB布局布线辅助Skill的设计与实现

面向 AI 协作的 PCB 布局、布线与几何检查工具包，包含 Python 算法、使用 Skill、嘉立创EDA（EasyEDA）接口、桥接服务和合成测试。欢迎通过 Issues 讨论算法、提交可复现问题，通过 Pull Request 一起改进。

**许可：允许非商业使用、修改与分享；商业使用须事先获得权利人的书面许可。** 详见 [主项目许可](LICENSE.md) 和 [参与贡献](CONTRIBUTING.md)。第三方文件沿用其原有许可，见 [第三方清单](licenses/README.md)。由于限制商用，本项目采用源码可用的非商业协作许可，不宣称符合 [OSI开源定义](https://opensource.org/osd)。

## 从这里开始

1. 让协作 AI 先阅读根目录 [SKILL.md](SKILL.md)。
2. 安装 Python 依赖：`python -m pip install -r requirements.txt`。
3. 运行合成回归：`python -B -X utf8 -m unittest discover -s tests -v`。其中适配器测试需要 Node.js。
4. 需要操作嘉立创EDA时再按下文的安装与连接步骤设置桥接；先使用工程副本，核对来源、备份及写回结果。

本公开仓库保留通用算法和嘉立创EDA接口，不发布 AD/Altium Designer 或其他EDA软件的专用接口脚本，也不附带真实PCB工程、认证信息或运行日志。算法的默认值和安全验收机制保持原样。

当前可在干净副本复跑的是包内284项Python测试。升级记录中额外101项历史算法回归、73项历史适配器测试属于作者保存的历史验证记录，其外部测试文件没有随本仓库分发；不能把这些数量说成克隆后都能直接复跑。有限搜索不保证全局最优，也不替代原生DRC与实板电气、热和制造验收。

以下为完整工具使用说明。

---

# PCB 自动布局布线工具包

2026-11-08分批升级：新增安全续算、普通外围引出、相对功能单元、有限邻网拆线/局部布局闭环及同源Gerber端帽补证。使用前阅读 [自动化与制造铜补证](AUTOMATION_AND_GERBER.md)，各项输入契约、限制和恢复命令集中在该文件；[交付记录](UPGRADE_20261108.md)列出每批改动、对比及未完成项。新路径全部显式启用，旧默认和原生安全闭环保留。

2026-11-08新工程实践修复：跨器件同网焊盘接触/重叠现在会被共享检查器拒绝；禁用布局跳过无用铜锁扫描；适配器支持经原始源码互证的有限编码差异；状态校验可直接比较当前快照来源。具体边界与实测对比见 [修复记录](REVIEW_FIXES.md#2026-11-08新工程实践修复)。未改变旧算法默认或原生写回安全机制。

原理图导入后的板外散落器件可使用 [分区初始布局](COLD_START_LAYOUT.md)，通过 `initialization="auto"` 和显式功能区域生成合法起点，再进入原布局优化；新方法须显式启用。逻辑功能分组与刚体簇分开声明，旧算法和全部验收机制保留。

差分线宽、可见图采样和性能审查的修复、对比结果与选项见 [REVIEW_FIXES.md](REVIEW_FIXES.md)。原验收机制和旧算法保留。

密集区域的可选升级见 [DENSE_ROUTING.md](DENSE_ROUTING.md)：混合几何搜索、整封装BGA扇出、有限级联推挤和成对通孔换层。原默认不变，配置格式见 [示例](examples/dense-routing.example.json)，支持边界和基准方法以该说明为准。

这个目录包含通用计算代码、嘉立创EDA接口、桥接服务与网关源码，以及对应的流程skill。让其他AI先读 [SKILL.md](SKILL.md)，接续工程时首先验证项目运行目录的 `project-state.json`。工程快照、真实板配置、运行状态、结果和测试日志放在目录外；包内 [examples](examples/README.md) 仅保留项目参数格式示例，不含PCB源文件或几何。只复制SKILL.md会丢失相对路径依赖。

本工具针对用户当前工程独立计算紧凑布局与布线。参考样板默认只提供板面积、占用率和工艺标准的比较依据，不是可复制的设计模板；即使拓扑相同，也不能据此复制工程、器件坐标、走线或铜区。只有用户明确授权复用这些内容时才可进入相应复用路径。有限候选与启发式计算不保证全局最优。

面对不同板型、层叠或线宽配置，先按 [原厂参考与权重配比](LAYOUT_REFERENCE_AND_WEIGHTS.md) 查阅适用的官方数据手册与参考布局，将关键结构转为本工程约束，再用固定尺度校准现有 `cost_weights`。该说明包含板型起始比例、零基准和负奖励处理、可执行字段示例及有限对比方法；没有新增自动归一化选项，也不改变算法默认值。

## 装配空间与丝印整理

布局硬检查通过后，还应按实际器件外壳和封装丝印复查焊接空间。显式指定高件及操作净距，用 `pcb_assembly.py` 检查；同一封装的相连轮廓先按所属器件合并，保留全部原图元及极性标记。密集行列应联合调整间距，不能只挪一个小件而把碰撞转移给相邻器件。

`eda/capture_silkscreen.py` 只读捕获当前原生属性和器件姿态；`eda/prepare_silk_geometry.py` 根据本工程已捕获的真实封装生成顶层轮廓障碍。未知曲线、底层镜像或独立图元保持未支持，不自动忽略。`pcb_silkscreen.place_labels` 在明确字框、板边、焊盘与轮廓障碍下生成有界位号候选；软件名义字号不等于实际墨迹高度，必须保存字体依据并查看原生画布。

写回采用 `pcb_native_silkscreen.py` 的独立、一次性任务，见 [接口说明](eda/README.md#独立原生位号显示整理)。默认保留位号显示；用户明确允许省略放不下的丝印时，可逐对象使用 `action:"omit"` 和非空 `reason` 关闭位号显示，保留逻辑位号和属性对象。封装外框、极性、1脚和接口标记不在此入口的省略范围。未找到位置及已省略对象分别列明；部分调整须把仍可见的未移动文字作为固定障碍重新核对，不能宣称全部丝印或整板DRC通过。该质量流程不改变布局/布线默认算法或整板验收语义。

## 安装与连接

盘中孔支持“尽量避免”的软偏好：将solver配置中的 `options.routing.via_in_pad_penalty` 设为正数，见 [配置示例](examples/via-in-pad-preference.example.json) 与 [详细边界](INPUT_CONTRACT.md#优先避开盘中孔)。路由会权衡绕行与新增盘中孔代价，必要时仍允许盘内孔；原硬检查不变，已有孔不自动修改。省略或设为0保持旧行为。

在工具根目录执行。Python需要3.10+，桥接服务需要Node.js 18+；网关源码构建需Node.js 20.17+。建议使用独立Python环境。

Python 依赖只有 Shapely 与 NumPy，版本范围见 `requirements.txt`。占用栅格使用 NumPy 布尔数组及 Shapely 批量点包含查询；拥塞协商、布局及阶段4质量检查复用这些依赖，PNG编码使用标准库，没有引入其他算法或图形库。

```powershell
python -m pip install -r requirements.txt
npm --prefix bridge/server ci --omit=dev
python -B pcb_bridge.py health
python -B pcb_bridge.py windows
```

无服务时在独立终端运行 `node bridge/server/bridge-server.mjs`。嘉立创EDA中安装启用Run API Gateway，允许外部交互；见 [bridge/README.md](bridge/README.md)。已有服务或等待超时时不重复启动。包内不分发Python、Node.js、嘉立创EDA或node_modules。

后续命令中的变量由当前工程提供：

```powershell
$pcbRunDir = '../pcb-run'
$edaWindowId = 'windows命令返回的实际窗口ID'
$pcbUuid = '当前工作副本的实际PCB UUID'
```

## 原生热焊盘辐条读取

已校准的原生铺铜同时保留实心区域与带宽度的直线热辐条。`fill=false`不等于没有铜；正`lineWidth`/`strokeWidth`表示实际铜笔画，不能当显示边界丢弃。仅支持单条两点显式`L`直线，并按已取证的POURED长度比例换算坐标和宽度；没有校准、未知曲线或混合填充笔画继续拒绝。

原生API未单独给出端帽样式，因此连通使用平头保守内界，间距使用方头保守外界，同时包含圆头可能性；不把未知端帽当作已证明的连接。API区域ID、填充、宽度及路径必须匹配完整原生来源，所有POURED对象和区域都保留；同时修改局部来源副本、API宽度与输出几何仍会被完整来源校验拒绝。来源绑定、备份、一次派发、双向读回及原生DRC的要求不变。

格式与限制见 [输入契约](INPUT_CONTRACT.md#原生直线铜笔画)。通用离线反例运行：`python -B -X utf8 tests/test_pcb_native_poured_strokes.py`。测试仅使用合成数据，不连接桥接，也不包含真实项目的ID、坐标、铜区或取证材料。

## 阶段1：可选的布线占用栅格

旧方法仍为默认：`placement.method="legacy"`、`routing.method="sequential"`、`routing.occupancy_grid=false`。可选的 `negotiated` 与 `analytic_sa` 见阶段2、3。新算法由用户明确选择，不因一次小样本改善自动改变默认值。

在现有工程配置中合并下列字段即可开启；完整 `board`、约束、地网列表和其他原配置仍须保留，不能用此片段替换整份工程输入。

```json
{
  "options": {
    "placement": {"method": "legacy"},
    "routing": {
      "method": "sequential",
      "occupancy_grid": true,
      "occupancy_max_cells": 2000000,
      "occupancy_max_seconds": 10.0,
      "occupancy_retries": 3,
      "max_expansions": 50000,
      "max_passes": 3,
      "max_seconds": 120,
      "max_net_seconds": 30
    }
  }
}
```

`grid_step` 仍使用原路由网格。栅格按目标网络、实际线宽和净距处理焊盘、铜、过孔、钻孔、禁区和板边；过孔候选另检查完整物理叠层。A* 网格扩展采用常数时间的布尔查询，起终点逃线、非网格候选和最终提交仍使用真实几何。同一网络的连接提交产生铜后，对已缓存网格只更新受影响包围盒；切换目标网络或线宽需独立构建，输入几何、规则或网格规格变更时不能沿用旧占用结果，不宣称整板仅构建一次。

栅格只生成候选：节点之间的细障碍、转角和组合钻孔仍可能使整条路径失败。原 `_commit` 会用 Shapely 精确复核，冲突格被阻塞后最多进行 `occupancy_retries` 次额外重搜；构建、更新、搜索和复核同时受原总时限与单网时限约束。栅格过大、构建超时或重搜预算耗尽均保留真实失败诊断，不能把未完成的栅格当作空白板或报告“无解”。最终仍执行 `check_board` / `check_routed_nets`，原生 DRC 与写回安全机制不变。

比较 `result.json` 中同一输入、规则和预算下的 `expansions`、`astar_seconds`、`elapsed_seconds`、`occupancy_build_seconds`、`occupancy_update_seconds`、`occupancy_cells`、`occupancy_updated_cells` 和实际重搜计数，同时比较完成网络、线长、过孔及独立检查。构建耗时可能超过小板的查询收益，局部查询提速也不代表整轮求解提速。统计单位和限额见 [INPUT_CONTRACT.md](INPUT_CONTRACT.md)。

算法原理与函数索引见 [SKILL.md](SKILL.md)。旧布局路径保留有限贪心摆放，新增方法见阶段3；信号网格采用单向多层 A*，DRC 局部补地仍是有界 Dijkstra 类搜索。开启阶段1栅格只改变候选碰撞查询方式。

## 阶段2：PathFinder 拥塞协商布线

需要多网共同调整路径时，在原工程配置中合并以下字段。`negotiated` 省略 `occupancy_grid` 时自动启用栅格，显式设为 `false` 报错；`sequential` 的默认值保持不变。

```json
{
  "options": {
    "routing": {
      "method": "negotiated",
      "negotiated_max_iterations": 20,
      "negotiated_history_increment": 1.0,
      "negotiated_present_factor": 1.0,
      "negotiated_present_growth": 1.3,
      "steiner_max_candidates": 128,
      "steiner_max_rounds": 2,
      "max_expansions": 50000,
      "max_seconds": 120,
      "max_net_seconds": 30
    }
  }
}
```

PathFinder 用 `(基础代价 + 历史拥塞) × 当前惩罚` 选择路径，按不同网络的资源共享情况更新代价并有限拆线重布。只拆除本次计算产生的候选铜，原板已有铜始终保留。中间共享候选不能写回；资源争用消除后仍逐网经过原 `_commit` 精确复核及原独立检查。轮数、格数、节点数或时间耗尽时保留“本次未找到”的诊断，不降低净距或隐去未完成网络。

多端网先用曼哈顿距离构造直角 MST，再在有限候选和轮数内做 1-Steiner 改进；既有同网铜允许从真实路径内部接入。树节点只是候选连接关系，完整实体连通仍由检查器证明。已有 `topology_orders` 保持无旁路铜链语义，优先于自动树；`CHANNEL_SPEC`、`trace_layers`、完整层栈过孔以及 `plane_then_drc` 地网流程保持。

内层可设置 `preferred_direction` 和 `nonpreferred_penalty`。例如实际物理层栈为 `[1,15,16,2]` 时，可合并 `"preferred_direction":{"15":"horizontal","16":"vertical"},"nonpreferred_penalty":1.5`；必须替换为当前工程真实内层号。方向是软代价，不禁止绕障，也不扩大允许走线层。外层/未知层配置拒绝，顺序模式下非空方向映射也拒绝，避免静默忽略。它不能代替参考面连续性、回流、阻抗或载流核验。

协商以 `negotiated_max_iterations` 控制轮数，不额外套用旧 `max_passes`。`pours_before_net` 仍在普通网络之后按依赖分批执行，先前批次保持固定，不跨铺铜依赖拆线。`pcb_continue.py --config` 也支持所选网络的协商续算；完整输入和所选范围之外的铜保持，结果仍是预览。字段限额、统计和失败边界见 [INPUT_CONTRACT.md](INPUT_CONTRACT.md)。

## 阶段3：力导向、模拟退火与自动尺寸

在现有完整任务的 `options.placement` 中合并以下字段。显式选择 `analytic_sa` 会执行全局及详细布局，不要求再设旧路径的 `optimize=true`；`enabled=false` 仍只核对原姿态。所有器件和网络来自当前工程，以下仅是参数示例。

```json
{
  "method": "analytic_sa",
  "seed": 17,
  "max_candidates": 4000,
  "max_seconds": 30,
  "global_iterations": 40,
  "global_step": 0.25,
  "sa_temperature": 10,
  "sa_cooling": 0.85,
  "sa_temperatures": 12,
  "sa_steps_per_temperature": 30,
  "rotations": [0, 90, 180, 270],
  "cluster_intra_steps": 20,
  "cluster_intra_radius": 10,
  "rudy_grid": [12, 12],
  "rudy_capacity": 1,
  "rudy_peak_limit": 1,
  "rudy_overflow_limit": 0,
  "ground_weight": 0.05
}
```

先做以固定件为锚点的力导向，再按器件/刚体簇包络进行行式合法化，并调用原几何检查；随后模拟退火尝试平移、两独立器件交换、允许角度旋转和簇整体平移，最后做有半径限制的簇内微调。目标逐项包含加权 HPWL、RUDY 峰值/溢出、明确的去耦及晶振引脚距离、接口边距、对齐与朝向奖励，以及原 `RELATIVE_POSE` / `REPEL_FIELD` 罚项。原硬约束继续拒绝非法候选，软代价不能覆盖它们。

关系来自 `clusters`、`edge_components`、`align_groups` 的真实 ID 和引脚引用，结构见 [INPUT_CONTRACT.md](INPUT_CONTRACT.md)。地网来自显式 `ground_nets` 或网络标签，名称本身不决定布局权重。失败布线的结构化网络与 RUDY 热点进入下一轮局部退火反馈；完整网表始终保留。读取 `initial_cost/final_cost`、`candidate_checks`、`sa_actions`、`budget_exhausted` 和实际布线检查，不能仅凭代价下降称整板最优。固定 seed 在相同版本、环境及计数预算下可复现；墙钟超时截断不保证相同姿态，结果会标记 `deterministic_completion=false`。

对已按授权清理并重新导出的未布线板，可在尺寸范围内自动比较：

```powershell
python -B pcb_compact.py --board "$pcbRunDir/unrouted-snapshot.json" --auto-size --target-occupancy 0.65 --aspect-ratio-range 0.7 1.6 --aspect-ratio-steps 5 --area-iterations 8 --size-max-candidates 48 --size-max-seconds 60 --placement-config "$pcbRunDir/placement.json" --output-dir "$pcbRunDir/auto-compact"
```

`placement.json` 是上述布局对象，可加入当前真实关系。自动尺寸显式使用 `analytic_sa`，默认上界为原板面积，包络面积总和除以目标占用率作为搜索下界；若需要更大的上界，必须显式传 `--max-area`。对长宽比扫描并有限二分面积，每个尺寸执行快速布局和 RUDY，保留候选结果并按“几何合法 → 拥塞达标 → 面积 → HPWL”排序。包络面积下界是该搜索的保守估计，双面器件面积相加可能过大；启发式合法化也不保证面积上的单调可行性，不能把二分结果称作最小可制造板。自动与手工 `--size` 互斥；仍只输出离线布局提案，先核对再按现有来源绑定流程应用。

## 阶段4：后处理、回流与信号质量

新增五个模块及完整[质量配置契约](QUALITY_CONTRACT.md)。默认行为不变，显式配置才启用：

- `options.cleanup`：有界橡皮筋拉直、45°候选规整及冗余过孔消除。只改本轮新增且未保护铜；死枝和单层接入/悬空孔只列候选与证据，不自动删除。
- `options.return_path`：真实铺铜后分别检查线宽投影、保护带、孔净空变化，并反查旧信号参考面是否被新增孔/铜分割。plane_then_drc保留待重铺状态，重铺后用独立入口复核。
- `CHANNEL_SPEC.diff_pair/length_group`：双端同层中心线耦合偏移、有界蛇形补长；从实际路线独立报告长度、偏差与耦合，拒绝宽铜捷径、支路和未支持路径，保留原几何及连通验收。
- `CHANNEL_SPEC.current_a/temp_rise_c/copper_oz`：基于公开IPC-2152数据拟合计算静止空气基线，仅提高现有线宽下限，保留模型系数、层别和限制。它不是实板热认证；缺少电流时保持旧规则。
- `pcb_render.py`：每层PNG及同尺度前后/差异图，含实际铜、焊盘、孔环与钻孔。生成后必须实际打开图片再给视觉结论。

在包含当前完整板和规则的task中添加配置后仍使用原solve命令；详细JSON、API与预算见[QUALITY_CONTRACT.md](QUALITY_CONTRACT.md)。`pcb_continue.py`不会静默忽略新增质量配置，差分/等长与cleanup/return_path需完整solve或对应独立入口。

```powershell
python -B pcb_render.py --board "$pcbRunDir/after-repour.json" --before "$pcbRunDir/before-change.json" --output-dir "$pcbRunDir/layer-review"
python -B pcb_return_path.py --board "$pcbRunDir/after-repour.json" --before "$pcbRunDir/before-change.json" --options "$pcbRunDir/return-options.json" --output "$pcbRunDir/return-report.json"
```

输出为离线质量证据，不自动操作嘉立创EDA，不改变来源绑定、写前备份、一次派发、几何双向读回或full_board_accepted语义。预算耗尽表示未找到或未完成，不表示无解；图像、载流拟合和回流几何检查均不能替代原生DRC及实板验证。

## 阶段5：接续状态与有界候选流程

通用SKILL已精简为决策与安全入口；详细步骤仍保留在本README和输入/质量契约。特定项目网名和线宽移到 [参数示例](examples/README.md)，不再作为通用技能要求。

在独立项目运行目录使用 `project-state.json` 保存基线身份与文件指纹、当前最佳候选、通过证据、运行中任务键、未决问题和下一步。换会话先按 [PROJECT_STATE.md](PROJECT_STATE.md) 验证实际文件及来源；哈希变化、文件缺失或来源不符不能用手改指纹处理。状态本身不授予写回权限，也不代替原生DRC或full_board_accepted。运行中/未知任务按原工具查询，不重新派发。

```powershell
python -B pcb_project_state.py init --board "$pcbRunDir/baseline/board.json" --output "$pcbRunDir/project-state.json"
python -B pcb_project_state.py validate --state "$pcbRunDir/project-state.json"
```

已有单轮solve产物可用 `record --state 项目状态文件 --run-dir 实际单轮输出目录 --run-key 唯一运行键` 登记，明确选为当前最佳时添加 `--select-best`。状态更新会复核工件并使用原子写入及旧文件哈希冲突检测；不能以状态中自填的passed替代实际检查。完整字段和状态转换见[状态契约](PROJECT_STATE.md)。

新增 `pcb_workflow.py` 在原solve之外执行显式方法序列，旧入口及默认算法保持。每轮保存目标网络/区域、不可改变条件、实际观测指标和停止条件；目标范围不删减完整网表，也不缩小硬检查。配置为独立policy文件，不能放进未知的solver options字段。

```powershell
python -B pcb_workflow.py --input "$pcbRunDir/task.json" --policy "$pcbRunDir/workflow-policy.json" --output-dir "$pcbRunDir/workflow-run" --project-state "$pcbRunDir/project-state.json"
```

| policy字段 | 默认与作用 |
| --- | --- |
| methods | 显式有序方法列表；每项指定placement_method、routing_method、occupancy_grid |
| max_rounds / max_seconds | 默认6轮 / 600秒，限制整个比较流程 |
| stagnation_rounds | 默认2；连续无改善达到此数即停止当前方法并切换下一项 |
| objective | 默认wire_length_mil；可选via_count、board_area_mil2、hpwl |
| min_relative_improvement | 默认0.01；预先定义明显改善阈值，不能看到结果后改门槛 |
| target_nets / target_regions | 默认完整网络 / 整板；记录改善目标，不改变原验收范围 |

每轮从同一原始输入独立求解，原规则和预算继续有效。方法用尽或总预算耗尽时明确停止，报告是否需要另选方法；不能只换seed无限重跑。信号阶段成功仍是awaiting_native_ground，离线结果与原生验收分别记录。精确类型、比较次序、时间边界及失败语义见 [WORKFLOW.md](WORKFLOW.md)。

默认值只在公平基准证明布通率不降低、至少一项预先声明的核心指标明显改善后才考虑切换。比较表必须包括失败样本、相同输入/规则/预算与环境/源码版本；单次耗时偶然下降或代理代价改善不足以切换。目前保留legacy/sequential/occupancy_grid=false，候选流程不会修改包内默认。

每阶段交付统一包含：改动文件清单、新增选项说明、基准/候选对比表、未完成项与风险。反例应覆盖已知断连、异网桥接、窄接触和预算耗尽，失败不得生成冒充通过的候选或证据。有限搜索结果不称为全局最优。

## 真实输入与紧凑布局

首次从当前板采集真实证据，具体字段见 [CONTEXT_CAPTURE.md](CONTEXT_CAPTURE.md)。

```powershell
python -B pcb_context.py --window-id $edaWindowId --output-dir "$pcbRunDir/capture-01"
python -B pcb_context.py --window-id $edaWindowId --spec "$pcbRunDir/capture-spec.json" --output-dir "$pcbRunDir/capture-02"
$captureDir = "$pcbRunDir/capture-02"
```

无spec时inputs_required、退出码2表示仍需实际规则，不是求解成功；只有exported/success:true/context_ready:true才继续。长度为mil，角度为度。读取样板仅提取板面积、器件占用率与明确工艺标准；当前工程的网表、器件、机械边界和计算结果来自当前工程。不得把“参考样板”“像样板一样紧凑”或同拓扑匹配解释成复制工程/坐标/路线/铜区的授权，更不能拿样板的UUID/指纹冒充当前来源。

先评估当前工程的板面积、占用比例与HPWL，再独立比较明确的较小候选。不能用大板上的大空隙掩盖布局/路由困难。对经过授权清理并重新导出的未布线输入，可执行：

```powershell
python -B pcb_compact.py --board "$pcbRunDir/unrouted-snapshot.json" --size $candidateWidthMil $candidateHeightMil --output-dir "$pcbRunDir/compact"
```

宽高变量来自当前机械条件，单位mil；`--size`可重复比较最多32个候选。程序保留焊盘/孔尺寸，根据当前工程独立调整可动器件，并进行有限合法化；排序依次为几何合法、RUDY拥塞达标、板面积、HPWL。手工模式默认仍用旧布局算法，也可显式选择 `analytic_sa`。不得将样板的坐标作为默认计算种子。`--preview`可保留有问题的候选，不能冒充合法结果。它输出board.json、result.json、placement-proposal.json，**不输出可直接改板框的写回计划**。按授权应用真实板框与姿态后重新导出，再开始路由。

可选`--reference`只增加reference_metrics对照，默认reference_positions_used=false，不移植器件坐标。`--allow-reference-poses`是另行明确授权后的坐标复用入口，不属于本次“参照标准、独立计算”的流程；传一个命令行开关本身也不能代替用户授权。该开关不会授权复制工程、路线或铜区。

独立提案需要改变实板布局和板框时，用pcb_relayout.py准备可审查计划。cleanup.json只列经确认允许清理的原生`lines`、`pours`、独立`vias`和全部当前`outline_line_ids`，禁止删除附属孔；proposal使用compact输出的initial_poses，也接受同义poses，并必须显式标记reference_positions_used=false。此入口只接受独立计算姿态。

```powershell
python -B pcb_relayout.py prepare --inspection "$pcbRunDir/native-before-relayout/inspection.json" --proposal "$pcbRunDir/compact/placement-proposal.json" --cleanup "$pcbRunDir/cleanup.json" --output "$pcbRunDir/relayout-plan.json"
python -B pcb_relayout.py --window-id $edaWindowId apply --plan "$pcbRunDir/relayout-plan.json" --audit-dir "$pcbRunDir/relayout-apply" --save
python -B pcb_relayout.py --window-id $edaWindowId status --job-key '计划中的实际任务键' --output "$pcbRunDir/relayout-status.json"
```

它核对真实UUID/source并备份，一次后台任务分批删除明确的旧生成铜，**先删除全部旧板框LINE，再创建四条最终矩形边**，避免逐边modify时编辑器自动合并/切分旧ID。元件仅同面移动，保留附属Via ID/网络/孔径。回读同时检查派生TEARDROP和POURED自动消失；不直接删除这些派生对象。成功也必须重新导出，比较真实板框、器件以及附属孔世界坐标后才接续布线；full_board_accepted仍false。部分失败不自动重放，读取当前状态和新来源后准备只涵盖剩余工作的全新计划。

## 明确线宽策略

普通信号默认至少10 mil；已有更宽线路保留。项目特定电源网及线宽通过当前配置表达，不从电压名称推断额定电流。未来明确的细间距例外或更宽要求可以覆盖默认值。历史项目参数已移到 [examples/foc-widths.example.json](examples/foc-widths.example.json)，仅在当前需求适用且真实网名匹配时合并。

新路由通常省略全局`routing.width`，为实际网络生成CHANNEL_SPEC；对应结构见 [INPUT_CONTRACT.md](INPUT_CONTRACT.md)。默认选宽为max(10,规则/网络下限)，显式全局width若小于某网下限会拒绝。合并示例时保留完整原配置及其他约束，不用片段覆盖整份任务。

调整当前工程已有直线/铜弧时，用原生清单只加宽不足部分。width-policy.json含`default_width`和`net_widths`，后者映射当前真实网名与明确的最小宽度。此原生宽度策略与solver示例是不同格式，应按当前规则分别编写，放在运行目录。

```powershell
python -B pcb_native_copper.py --window-id $edaWindowId inspect --document-uuid $pcbUuid --output-dir "$pcbRunDir/native-before-widths"
python -B pcb_native_copper.py prepare-widths --inspection "$pcbRunDir/native-before-widths/inspection.json" --policy "$pcbRunDir/width-policy.json" --output "$pcbRunDir/width-plan.json"
python -B pcb_native_copper.py --window-id $edaWindowId apply-widths --plan "$pcbRunDir/width-plan.json" --audit-dir "$pcbRunDir/width-apply" --save
python -B pcb_native_copper.py --window-id $edaWindowId status --job-key '派发收据中的实际任务键' --output-dir "$pcbRunDir/width-status"
```

只有final/success:true才算操作完成，saved单独核对。只改lineWidth，保留路径、网络、层、圆弧角度及原来更宽线；锁定线不会被自动解锁。原生共线合并可能改变LINE的ID，代码用完整铜覆盖等价验证修改结果，不以旧ID是否存在作为唯一依据。加宽可能影响间距，随后必须重铺铜和检查DRC。

## 信号先行，原生铺铜后补地

推荐对明确地网使用以下路由配置，保留完整输入网表和所有地焊盘：

```json
{"options":{"routing":{"ground_strategy":"plane_then_drc","ground_nets":["GND"]}}}
```

GND必须替换为当前实际地网名，不能按相似字符串推断。该模式只暂缓地网搜索，不与同一地网的pours_before_net或地网topology_orders混用；pipeline离线pours/shielding也不应同时配置。六算子和全部参数见输入契约。

需要为参考地预留内层时，用`routing.trace_layers`限制**新走线**所在层，例如当前实际六层为`[1,15,16,17,18,2]`时可将新信号线限制为`[1,16,17,2]`。通孔仍跨完整物理叠层并逐层检查碰撞和钻孔；已有铜保持原样。`net_tags.allowed_layers`限制包括通孔在内的全部网络铜，不能用它代替仅限制走线层的选项。

```powershell
python -B pcb_pipeline.py prepare --board "$pcbRunDir/snapshot.json" --config "$pcbRunDir/solver-config.json" --output "$pcbRunDir/task.json"
python -B pcb_pipeline.py solve --input "$pcbRunDir/task.json" --output-dir "$pcbRunDir/signals"
```

信号阶段完成时退出码0，但结果是`signal_stage_passed:true`、`status:awaiting_native_ground`、`passed:false`、`full_board_accepted:false`。检查deferred_ground_nets与pending_stages；board.json及writeback-plan.json是等待原生地处理的预览产物，不是整板验收。已授权后可用专用入口写回，再重新导出真实来源：

```powershell
python -B pcb_preview.py --window-id $edaWindowId apply --plan "$pcbRunDir/signals/writeback-plan.json" --audit-dir "$pcbRunDir/signal-apply" --ack-preview --save
python -B pcb_preview.py --window-id $edaWindowId status --job-key '派发收据中的实际任务键' --output "$pcbRunDir/signal-status.json"
python -B pcb_bridge.py --window-id $edaWindowId export --context "$captureDir/context.json" --output "$pcbRunDir/after-signals.json"
```

当前工程已有信号若仍符合新的独立布局和连接要求，可核对后保留；这不构成导入样板路线的许可。布局变化影响的信号应在当前工程重新计算，完成后进入原生铺铜阶段。

预览任务的`success/applied/saved`只记录API执行及保存结果；另检查`geometry_readback_passed`。程序将写前全部铜LINE减去指定删除、加上计划新线，与写后实板进行双向覆盖核对，允许几何等价的拆分/合并；过孔按网络、位置、孔径、外径和完整层栈逐一匹配。原生自动偏移即使计数一致也会报告差异并置`needs_geometry_repair:true`，CLI返回非零；保留当前实板，不回滚或重放，须基于新来源准备修复delta。若移动器件，计划须包含完整`target_board.routes.vias`以核对附属孔目标位置。此校验范围是铜LINE与通孔，不代替铜区、器件、DRC及整板验收。

### 原生铺铜、DRC与局部补连

新建区域的native-copper-spec.json明确写入`layers:[{layer,net,name}]`、`ground_nets`和可选edge_clearance（mil）；每层接什么网、哪里分区来自实际电路和叠层，程序不猜电源。常见六层起点是外层器件/信号与地铜、紧邻外层的L2/L5连续参考地、L3/L4安排电源分区和内部信号；实际物理层顺序、回流与电源边界需要核对，不能把所有层无差别接到一个网。创建整层区域需要真实outline；板框内缩后分裂或带孔时需明确规划区域，不能填平孔洞。

天线、安装结构等需要留空时，在同一spec中明确给出`copper_keepouts:[{points:[[x,y],...],layers:[实际铜层号,...],provenance:"区域来源与所含退让依据"}]`。坐标为真实PCB世界坐标mil；退让距离须包含在声明区域内，程序不会猜测天线边界。每层分别从真实板框内缩区域减去其适用禁铜区，允许形成贴板边的凹口，保留原板框和来源身份。结果为空、分裂为多个岛或仍有孔洞时拒绝生成，不能用外轮廓填掉禁铜区。刷新已有pour_id只保留其原边界，因此若该层声明了禁铜区会拒绝刷新，需要重新规划该区域；禁铜区也不能替代已有信号线/过孔的独立禁布检查。

刷新既有区域时，先inspect，spec里的每项还要填写其实际`pour_id`。这条路径保留原生复杂轮廓、电源分区、孔洞、优先级和孤岛策略，可处理离线适配器暂不支持的铜弧/原生铺铜。新建区域才采用默认去孤岛设置。单个区域重建为空时记录empty_pours并继续，按层/网络的实铜覆盖与DRC判断结果。不要强行将复杂路径改为直线弦。

```powershell
python -B pcb_native_copper.py --window-id $edaWindowId inspect --document-uuid $pcbUuid --output-dir "$pcbRunDir/native-before-pours"
python -B pcb_native_copper.py prepare --board "$pcbRunDir/native-before-pours/inspection.json" --spec "$pcbRunDir/native-copper-spec.json" --output "$pcbRunDir/native-copper-plan.json"
python -B pcb_native_copper.py --window-id $edaWindowId apply --plan "$pcbRunDir/native-copper-plan.json" --audit-dir "$pcbRunDir/native-copper-apply" --save
python -B pcb_native_copper.py --window-id $edaWindowId status --job-key '派发收据中的实际任务键' --output-dir "$pcbRunDir/native-copper-status"
python -B pcb_native_copper.py --window-id $edaWindowId drc --document-uuid $pcbUuid --ground-net GND --output-dir "$pcbRunDir/ground-drc-01"
```

上面的inspection用于刷新既有pour；首次创建时将prepare的`--board`改为写回后重新导出的真实快照。可重复`--ground-net`指定多个实际地网。原生apply会重建实铜、读回完整区域、按要求保存并默认执行原生DRC；DRC有问题时报告问题，不因它自动撤回整板工作。

从ground_targets读取报告对象，再使用同次证据中的原生Pad/Via/Line/Arc坐标规划短连接或接地孔，不能直接把DRC面板显示位置当作mil坐标。端点条目与独立缺口不是同一个数量：同一缺口可能列出两端，未确认对象关系时只报告原生条目数，不自动除以二。仅补剩余缺口；同一缺口中已证明接入主地平面的另一端不重复打孔。局部修复后重新inspect/prepare刷新受影响铜区，并重跑DRC。`ground_connections_reported_clear`、`native_drc_passed`、操作success和saved是不同结果；未知分类或无法解析的端点不能被忽略。`--skip-drc`仅用于用户当前明确要求的纯预览，不能替代此闭环的DRC依据。

已取得含真实填充铜的canonical快照和坐标证据后，用局部入口计算DRC点名的焊盘/既有过孔缺口：

```powershell
python -B pcb_ground_repair.py --board "$pcbRunDir/after-pours.json" --drc-targets "$pcbRunDir/ground-drc-01/ground-repair-targets.json" --copper-evidence "$pcbRunDir/poured-coordinate-evidence.json" --rebuild-spec "$pcbRunDir/ground-rebuild-spec.json" --output-dir "$pcbRunDir/ground-local"
```

该入口只枚举有限半径内的至少10mil短线及默认24/12mil全栈过孔，以已接实际端子的单个连续Poured内界作为参考，证明目标→短线/孔→参考铜的局部实体连接。优先使用已接参考铜的现有孔，新孔不能切断既有固定铜；未点名的地焊盘不参与全局搜索，浮空同名岛不能充当参考。原生仍报错时，单凭离线预测接触不能跳过；已有完整路径也不重复添加，须检查实际端点与重铺后的DRC。`--radius`、`--grid`、`--max-candidates`仅控制有限局部搜索。产出source-bound预览计划，应用后必须原生重铺和DRC复查；局部证明不替代整板验收。坐标证据契约见[INPUT_CONTRACT.md](INPUT_CONTRACT.md)。

参考面可通过现有同网铜和真实镀孔连接到物理焊盘，不要求主面直接碰到焊盘。选择过程逐层、逐独立铜岛建图，同层只接受保守内界的正面积接触，跨层只接受同一实际镀孔且两侧均连续接触孔壁；同名浮岛、异层投影、非镀孔、点触或仅边缘接触不能作为锚链。按面积优先检查候选，返回一块已证明接到端子的连续实际铜区，不把不同岛的面积合并。

锚链验证具有节点、接触检查次数及时间预算；耗尽只报告尚未证明。结果中的`reference_regions`记录所选面、面积、层、物理端子、逐边接触证据及用量，不能凭面积最大或网名相同认定参考面。它只验证已有铜连接，不新增全网地线搜索。通用反例运行`python -B -X utf8 tests/test_pcb_ground_reference.py`，不含实际项目数据。

接地通孔会穿过电源铺铜，旧Poured需要重铺以生成避让。`--rebuild-spec`显式列出本次会重建的原生Pour父ID；仅这些派生铜暂缓碰撞判断，固定Fill、泪滴、走线、焊盘及过孔继续作为障碍。清单未给或未包含的Poured仍是固定障碍。候选状态为candidate_awaiting_rebuild，旧铜保留在目标数据中，ground_connected_after_rebuild仍未知；必须写入→重铺所有清单铜区→保存→原生DRC复查，不能把暂缓碰撞当通过。

有限直连候选用尽时，可显式增加 `--local-grid-nodes` 启用同一目标附近的网格回退，默认0关闭，允许1至100000个展开节点。它仍受 `--radius`（最大1000mil）和 `--grid` 限制，逐段检查真实固定铜，落点只接受已接参考铜的既有孔或经过全叠层检查的新孔；不移动信号、不搜索整个GND。读取result中 `local_grid_search` 或 `local_grid_searches` 的实际节点数及预算耗尽状态；局部搜索失败不证明无解，候选成功也仍需写回、重铺与原生DRC。

首次读取实际Poured优先用只读校准：`pcb_native_poured_capture.py via-anchors --source 当前source.json --pour-id 实际父PourID --output-dir 新目录`。它以至少8个非共线真实异网过孔为锚点，逐个核对VIA原文与API坐标，再对完整两半圆孔集合求唯一比例及零平移；不按板框猜比例。成功生成native-pour-profile.json后用`export --context context.json --profile native-pour-profile.json --output-dir 新目录`导出实时完整board.json与poured-coordinate-evidence.json。export在现场读取新来源，并逐项核对当前Poured源码/API和已测比例；补地重铺改变POURED记录后，先重新执行via-anchors采集同源完整原文，再用export刷新，不能手换旧板摘要。

可选`capture --source 当前source.json --pour-id 实际父PourID --region-id 实际区域ID --output-dir 新目录`用于原生Poured→Fill实验取证；它先本地备份并派发一次性任务，之后`collect --output-dir 同目录`只读取得状态和三阶段原文。该API已观察到会改变铜区边界，不能保证产生有效比例证据，禁止放宽公差伪装纯缩放。中途中止且已知临时Fill尚在时用`recover --original-dir 原目录 --source 当前source.json --output-dir 新目录`只取证和清理，不重放转换。删除返回值与真实ID消失分别记录，只有实际ID消失才继续重铺保存。全局参数`--bridge-url`、`--window-id`放在子命令前。

## 指定网络续算

```powershell
python -B pcb_continue.py --board "$pcbRunDir/checkpoint.json" --source-board "$pcbRunDir/snapshot.json" --config "$pcbRunDir/solver-config.json" --preview --output-dir "$pcbRunDir/continued"
```

输入仍是完整板。`--nets`和重复`--exclude-net`互斥；plane_then_drc配置下默认暂缓ground_nets。原生铺铜后的少量地网缺口使用上面的pcb_ground_repair.py；显式`--nets 实际地网`会搜索该整个网络，只用于用户另行要求的全地网路由，不能冒充局部修复。PowerShell中含`$`的网名用单引号。顺序续算每个接受连接保存board.json；协商中间共享候选不保存，所选协商批次完整返回后才保存新的候选。未写检查点用source-board保留原始真实基线。`selected_connectivity_passed`只描述所选范围，full_board_accepted仍为false。

若实板正在应用独立布局，可以用`pcb_continue.py --calculation-only`并行计算尚未写入的布局提案。此模式只保存board/result/progress，不生成writeback-plan.json，并标记requires_live_rebase。实际relayout成功后必须重新导出，核对板框、全部姿态、真实封装/孔和叠层与计算输入相符，再针对新来源生成写回计划；不能直接换UUID/指纹冒充已完成核对。

需要有限让线时，`--trace-patch`指定source_segments及replacement_segments。原段必须与输入和基线完整匹配且未锁定；新段须通过障碍检查，不增加铜组也不拆开原已连接端子组。写此类计划追加`pcb_preview.py apply --allow-trace-replacement`。不要把同一补丁重复应用到已经替换的检查点；原生操作未知时只查询状态，不重放。

原生自动合并导致需要保留部分LINE身份时，可从刚导出的完整板与同源、已重验的局部提案准备显式修改计划：

```powershell
python -B pcb_preview.py prepare-trace-patch --board "$pcbRunDir/current-board.json" --proposal "$pcbRunDir/rechecked-proposal.json" --output "$pcbRunDir/trace-patch-plan.json"
python -B pcb_preview.py --window-id $edaWindowId apply --plan "$pcbRunDir/trace-patch-plan.json" --audit-dir "$pcbRunDir/trace-patch-apply" --ack-preview --allow-trace-replacement --save
```

提案须包含`source`、`source_segments`、`replacement_segments`及`recommended_primitive_operations`，后者仅允许显式modify/create/delete。prepare交叉核对两个线段清单与操作描述、真实当前ID/几何/铜层；apply在任何写入前再次核对现场。modify保留原net/layer/width；create只能使用被修复线段已有的同net/layer/width组合，delete对应组合必须仍有替换铜。操作按列出的顺序执行；多条相连LINE逐个modify也可能使相邻ID拆分或合并，不能假定旧ID仍有效。失败或几何读回不符时先fresh export查明差异，不重放；必要时对已重验的明确当前线清单采用先delete再create。删除返回false时仍以该ID确实消失为准。此入口只应用已计算、已重验的显式补丁，不自动修正旧指纹或推断新路径；保存四位坐标变化也必须重新导出和重验。完成后重建受影响的原生铺铜并检查DRC。

已有具体阻挡线时，可用`pcb_local_repair.py --board 当前完整快照 --net 待连接网络 --blocking-trace-index 实际零基索引 --options 路由选项JSON --output-dir 新目录`计算有限局部绕行。选项需保留当前`trace_layers`和孔径/线宽约束。它保留该线的两端、核验原网络的每个端子连通组，再用全栈孔及真实铜接触证明补连；只输出显式`proposal.json`与候选`board.json`，不自动删除或写入原生铜。提案仍需当前来源重验；不能把尚未写入布局的旧来源指纹直接作为实板写回依据。

原生DRC点名的地焊盘确实被一条信号线挡住时，使用同一入口的 `--mode joint-ground` 联合计算该线绕行和局部接地路径：

```powershell
python -B pcb_local_repair.py --mode joint-ground --board "$pcbRunDir/current-board.json" --net $groundNet --target-pad-id $targetPadId --blocking-trace-id $blockingTraceId --drc-targets "$pcbRunDir/ground-repair-targets.json" --copper-evidence "$pcbRunDir/poured-coordinate-evidence.json" --rebuild-spec "$pcbRunDir/ground-rebuild-spec.json" --options "$pcbRunDir/joint-options.json" --output-dir "$pcbRunDir/joint-ground"
```

三个变量须填同次原生证据中的实际地网、目标焊盘和未锁定异网铜LINE ID。候选保留原线两端、层、网络、线宽及原端子连通组；只暂缓该线和原生refs证明直接属于它的泪滴，并明确列出 `expected_removed_derived_teardrops`，不把它们当作Fill删除。找到候选才输出board.json与来源绑定的writeback-plan.json，proposal.json保存证明与预算；应用计划需 `--allow-trace-replacement`，随后检查派生铜真实清理、重建清单铜区并跑DRC。参数及默认预算见[输入契约](INPUT_CONTRACT.md)；未找到有限候选不等于无解。

## 完整离线验收与交付边界

需要完全离线流程时，使用默认ground_strategy:route，明确配置离线pours/shielding，并检查完整结果：

```powershell
python -B pcb_pipeline.py validate --input "$pcbRunDir/solved/board.json" --output "$pcbRunDir/validation.json"
python -B pcb_bridge.py --window-id $edaWindowId apply --background --max-wait-seconds 900 --save --plan "$pcbRunDir/solved/writeback-plan.json" --context "$captureDir/context.json" --audit-dir "$pcbRunDir/strict-apply" --output "$pcbRunDir/strict-result.json"
```

普通严格apply要求完整目标与原生DRC通过，不接受preview_only。原生复杂曲线超出canonical支持范围时，改走上述原生证据流程，不能伪造离线通过。任何路径都保留UUID/来源匹配、原文备份和一次派发记录。有限搜索不保证全局最优；铺铜、线宽和连通检查也不能代替载流、温升、回流、功能与EMC验证。

## 代码索引

| 文件 | 职责 |
|---|---|
| pcb_compact.py / pcb_placement.py | 当前工程独立板框/布局候选、自动尺寸与手工尺寸、严格入口及原有限合法化 |
| pcb_global_place.py | 可选力导向、刚体簇合法化、模拟退火、RUDY与明确关系代价；输出原检查器验收的候选 |
| pcb_relayout.py | 明确旧铜清理、原生板框整体替换、同面姿态写回与重导要求 |
| pcb_pipeline.py / pcb_constraints.py | 完整或信号阶段编排，六算子编译和独立检查 |
| pcb_routing.py / pcb_copper.py | 多层路由，离线实体铜及伴随孔 |
| pcb_occupancy.py | 可选的分层占用栅格、全叠层过孔候选、局部增量更新与冲突格反馈；最终仍须精确复核 |
| pcb_negotiated.py | PathFinder 拥塞协商、直角 MST 与有界 1-Steiner 拓扑、内层方向代价；中间共享资源不能作为验收结果 |
| pcb_cleanup.py | 本轮新增铜的有界拉直/45°规整/冗余孔候选，死枝及单层孔只列证据 |
| pcb_return_path.py | 已锚定实铜上的双向参考面检查，分开报告投影、保护带、孔净空变化 |
| pcb_differential.py | 中心线耦合偏移、蛇形补长及独立实际路径测量，不信任生成缓存 |
| pcb_ampacity.py | IPC-2152数据公开拟合的分层线宽下限及可追溯模型证据 |
| pcb_render.py | 无新增依赖的逐层PNG与同尺度几何差异图、来源和哈希清单 |
| pcb_project_state.py / PROJECT_STATE.md | 项目基线、候选、证据、运行任务键及接续步骤的严格状态索引 |
| pcb_workflow.py / WORKFLOW.md | 显式方法序列、逐轮合同、停滞切换和有限总预算，不改变原solve默认 |
| pcb_native_copper.py | 原生LINE/ARC加宽、Pour创建/刷新、实铜证据与DRC地网目标 |
| pcb_ground_repair.py | 仅处理原生DRC点名焊盘/既有过孔缺口的短线/通孔候选及实际地铜连接证明 |
| pcb_native_poured_geometry.py | 已校准L/ARC复合填铜及两点直线铜笔画的保守内外界，完整来源与清单核对；不推测原生单位 |
| pcb_native_poured_capture.py | 只读Via圆孔单位互证、可恢复原生转换实验、同源复合实铜快照导出 |
| pcb_pipeline_common.py / pcb_geometry.py / pcb_solver_hardened.py | 几何、钻孔与物理连通验收 |
| pcb_bridge.py / eda_adapter.mjs | 真实导出、来源保护、严格写回、DRC及制造导出 |
| pcb_context.py / CONTEXT_CAPTURE.md | 当前工程真实封装、层与规则证据采集 |
| pcb_continue.py / pcb_preview.py | 完整输入上的选网续算及授权预览写回 |
| pcb_local_repair.py | 保留原线端点和原网连通的有限让线、细焊盘局部出孔候选 |
| bridge/server / bridge/gateway | 桥接服务与网关扩展源码 |

`tests/test_pcb_native_poured_strokes.py`覆盖合成直线端帽界、非法语法、窄接触/假连接、改宽、删区域、整对象遗漏与来源副本协同篡改反例。

交付文件相对路径和SHA-256见 [MANIFEST.sha256](MANIFEST.sha256)，不含清单自身；来源许可见 [licenses/README.md](licenses/README.md)。依赖安装及任务运行产生的文件不属于原始清单。
