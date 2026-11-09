# 自动续算、局部修复与制造铜补证

本次升级分三批，所有新计算路径均需显式启用。默认仍为 `legacy / sequential / occupancy_grid=false`。候选只能经原几何、物理连通和原生 DRC 验收；来源绑定、写前备份、一次派发、双向几何读回、`plane_then_drc` 及 `full_board_accepted` 语义保持。此升级不连接或修改真实工程，不从样板复制坐标、走线或铜区。

## 第一批：续算、外围引出与受限端帽补证

### 布线检查点

计算检查点JSON与状态读取保持32MiB上限；超限明确不保存，不能把无法读取的大文件报告成可续算进度。大型工程可缩减与求解无关的外部工件索引，但不能删网表、端子、来源和安全证据来规避上限。

`routing.checkpoint` 为布尔值，默认 `false`。启用后，普通网逐网精检并保存完整计算工件；失败仍不返回成功板或写回计划。`pcb_pipeline.py solve` 和 `pcb_workflow.py` 将工件保存为独立 `calculation-checkpoint.json`，结果记录绝对路径及字节 SHA256。

路由器内部工件 `kind=calculation_checkpoint` 包含完整源、原板/配置/候选摘要、完整板、已完成/未完成/延期网、累计时间/逐网时间/扩展数/修复次数、下一允许轮次和失败区域。流水线外层 `kind=pipeline_calculation_checkpoint` 再绑定原 task、布局后的路由输入、实际路由选项、累计流程耗时、轮次、反馈与停滞历史。二者始终 `writeback_allowed=false`、`full_board_accepted=false`。内层检查点不能直接给流水线 CLI。

```powershell
python -B pcb_pipeline.py solve --input "$pcbRunDir/task.json" --output-dir "$pcbRunDir/round-001"
# 先核查任务确已终结、原状态、现场来源和result中的文件摘要；使用新输出目录。
python -B pcb_pipeline.py solve --input "$pcbRunDir/task.json" --output-dir "$pcbRunDir/resumed-001" --resume-checkpoint "$pcbRunDir/round-001/calculation-checkpoint.json" --checkpoint-sha256 "上次result中记录的64位SHA256"
```

恢复重新核对完整任务、来源、规则、封装、姿态授权、原铜前缀、全网表、已完成网的实际连通与新铜锚定。改变输入/线宽/叠层/允许层后不能复用旧缓存。累计时间、逐网时间、轮次及修复次数不重置；耗尽报告“未找到”，不偷偷重启。文件自带摘要不是签名，也不证明现场新鲜；状态与已派发任务检查仍由调用方执行。检查点不进入 `best_candidate/passed_evidence`，更不能当作新原板绕过原铜保护。

首批增量路径只支持 `sequential` 普通网络，明确拒绝同时启用协商、差分/等长、显式拓扑、`pours_before_net` 或推挤。旧算法的这些能力保留，通过原路径使用。支持延期地网及普通/BGA 引出；恢复不会重复执行已验收引出。

Python接口：`route_board(board, options, *, checkpoint_callback=None, resume_checkpoint=None)`；`run_pipeline(task, *, checkpoint_callback=None, resume_checkpoint=None)`。回调接收独立副本，文件写入失败不伪装成保存成功。批量脚本应使用原子 `pcb_automation.write_checkpoint` 和摘要检查 `read_checkpoint`。

### 普通外围焊盘自动引出

这里的延期地网仅指 `plane_then_drc` 下显式声明的 `ground_nets`，不会从网名猜测地网。

`routing.escape` 是严格对象，默认如下。它用于显式选择的贴片外围引脚，BGA 仍使用原 `fanout`。

| 字段 | 默认与范围 |
| --- | --- |
| enabled | false，布尔值 |
| max_seconds | 30，有限正秒，另受总/逐网预算约束 |
| max_candidates | 5000，整数1..1000000 |
| max_backtracks | 32，整数0..100000 |
| max_candidates_per_pin | 64，整数1..512 |
| groups | []；启用时1..128个显式组 |

组只接受 `component,pads,sides,escape_layers,escape_margin,via_distances,lateral_offsets,provenance`。必填项为真实器件ID、真实padId列表、方向列表、允许出口层列表、非空依据说明；各列表不能重复。`sides` 为封装局部 `left/right/top/bottom`，上/下对应局部负/正Y，并应用实际旋转和底面镜像。`escape_margin` 默认30mil正值；`via_distances` 默认 `[20,40]`，非负候选；`lateral_offsets` 默认 `[0,-10,10]`，有限候选；后两者最多16项。

根据当前封装包络生成直线、45°折线、短线接全层通孔候选，先处理可用出口少的引脚，失败有限回溯。真实出口必须位于包络外且实际铜接回指定端子；仅画短线或放孤孔不算成功。全部层的孔净空、原铜、禁区、板边、线宽和允许层仍精检。GND延期、差分、等长及显式拓扑网络列为保护/暂缓项，不偷偷当普通网引出。盘中孔继续是显式软偏好，不能变成硬禁令。

### Gerber受限补证

来源范围须分清：本工厂逐对象证明原生POURED与制造图的关系；普通器件、走线、过孔和网表的原生真实性仍由原采集/适配及双向读回证明。封存完整输入只防止上下文建立后被替换，不证明建上下文前传入的任意JSON都是真实原生快照。报告明确 `non_pour_source_verified=false`、`source_scope=native_poured_records`；几何检查通过仅表示该输入候选在这些铜证据下通过，不表示全部原生对象已认证。

新增 `pcb_gerber_evidence.py`。`round_caps` 模式完整扫描同源原生导出的正片暗铜 Gerber，匹配圆形实心光圈的直线 D01，与原生 POUR/区域ID、层、网、路径、线宽逐对象绑定。完整原生源码重新计算 sourceFingerprint，核对成功导出收据、ZIP字节摘要、每个成员摘要及完整层映射。未知语法、清铜、宏、步进重复等明确拒绝，不在找到一条匹配后跳过其余文件。

```powershell
python -B pcb_gerber_evidence.py --board "$pcbRunDir/native-poured/geometry-view-board.json" --receipt "$pcbRunDir/fabrication/manufacturing-receipt.json" --archive "$pcbRunDir/fabrication/实际导出文件.zip" --layer-map "$pcbRunDir/gerber-layer-map.json" --profile round_caps --seconds 120 --out "$pcbRunDir/gerber-proof-report.json"
```

层映射是“实际铜层ID → ZIP内实际成员路径”的JSON对象，必须覆盖完整叠层，不能猜文件后缀；有 FileFunction 属性时还须核对铜层序号。工厂 `build_geometry_context(board,receipt,archive_path,layer_map,*,profile,limits)` 返回本次验证上下文及报告。`check_board`、`verify_full_board_drc_and_connectivity`、`copper_and_drills`、`_groups` 可显式接收 `geometry_context`。上下文绑定完整板，几何或规则变化后失效；持久化报告、字典和 `cap_style` 标记不能替代重新读文件。

默认无上下文仍使用平头内界/方头外界。受限补证只扩充经证明的内界，外界不变；不会降低异网间距或改写实体铜。CLI输出逐对象证据及整板几何/连通结果，始终保留 `full_board_accepted=false`，还需独立原生闭环。没有修改PCB以“补上”本来就存在的圆帽。

## 第二批：相对关系模板与局部闭环

### 功能单元

重复引出/局部修复时，`escape` 按实际铜组重验端子、出口层、侧向及包络外出口；已证明出口记录 `reused`，不重复添加孔线。`generated_outlets` 与复用数分报，不能用metadata中的完成标记代替几何证据。

`placement.functional_cells` 默认 `[]`，最多128项，仅适用于显式 `analytic_sa`。每项必含 `id,anchor,relations,provenance`，可含 `max_candidates`（默认128，1..100000）。`anchor` / `neighbor` 为 `{compId,padId?}`，省略padId明确指器件原点。器件不能重复或跨单元重复归属，不能与其他刚体定义冲突。

关系必含 `neighbor,sides,distances,rotations`；可含 `lateral_offsets`（默认 `[0]`）、`tolerance`（默认0.1mil）。方向在锚点局部坐标系；距离为正mil候选，旋转为相对锚点角度候选，侧向偏移为有限mil候选，各数值列表1..16项。每单元1..32个邻件关系；容差非负且小于全部候选距离，不取消方向限制。未知字段拒绝。

模板只保存来自本板要求/原厂资料的相对关系，不能存历史世界坐标。依据本板真实焊盘生成相对种子，先有限回溯选择几何可行组合，再作为刚体参与已有分区初始化、全局布局和退火。原固定件、旧铜、原区域和旋转限制仍优先。`check_relations` 独立重测实际位置与角度，布局出口、流水线续算和 `verify_candidate` 都复查，不能靠stats宣称关系满足。

### 有限邻域拆线重布

`routing.local_repair` 默认 `{enabled:false,max_attempts:3,max_ripup_nets:3,max_seconds:15,radius:120}`。enabled为布尔；尝试数1..100、邻网数1..32，时间与半径为有限正数。失败网实际端子包围盒和附近铜对象用于选择少量邻网。只拆本轮生成的普通网铜，原铜、延期地网和特种网络保护；每个试案重新布线并精检全部已完成网，失败丢弃整个试案。报告阻挡对象、范围、拆线清单及次数，不声称碰撞诊断是不可行证明。

### 布线失败回传局部布局

组合边界：BGA `fanout` 支持检查点内恢复且不重复执行，但旧BGA入口还没有跨局部布局新轮的几何复用，因此本版明确拒绝 `automation.enabled=true` 与 `routing.fanout.enabled=true` 同时启用。普通 `escape` 已按实际铜实现重跑复用，可以组合。旧BGA算法不被修改或禁用。

新增 `options.automation` 严格对象：

| 字段 | 默认与范围 |
| --- | --- |
| enabled | false；启用需 `analytic_sa`、布局开启、`routing.checkpoint=true` |
| max_seconds | 600，有限正秒，累计流程总时限 |
| max_layout_repairs | 2，整数1..19；另受原 `max_iterations` 约束 |
| repair_radius | 200，有限正mil；用于热点周边选件，不代表最大移动距离 |
| max_moved_components | 8，整数1..1000；包含扩展后的完整刚体/功能单元 |
| stagnation_rounds | 2，整数1..20；已完成网数连续无改善则停止当前方法 |

先用路由器精检检查点提取失败网、实际失败包围盒与RUDY热点，再从原movable白名单里选择少量器件；固定件、原铜连接件不移动。相关网络本轮新增铜被撤回，无关已布铜完整保留。刚体/功能单元必须整体可动且不超数量预算；否则报告本轮未找到，不偷偷全板重排。原分区是位移边界，首轮绝对种子不在修复时重复施加。

每次局部布局、续布、后处理和末次验收均受累计时限约束，恢复不清零历史。耗时的单次GEOS操作采用合作式时限，不能强制抢占，但结束后超预算不报告成功。默认关闭时仍走原流水线反馈行为。跨方法比较仍由原 `pcb_workflow.py` 的显式方法序列管理；不能把局部预算用尽解释成允许无限换seed。

## 第三批：有序暗清铜图及能力边界

显式 `profile="final_image"` 在完整文件顺序上执行暗铜并集、清铜差集，支持标准无孔 C/R/O 光圈、圆光圈圆弧、明确闭合简单区域及奇偶孔洞、光圈 LM/LR/LS 变换。变换作用于光圈，不误当作焊盘坐标变换。每步保留保守内外界；最终已证明铜图约束整个连接内界，不能保留后来被清掉的端帽或核心。原间距外界不缩小。原生核心与制造图冲突时拒绝或保留未知，不能掩盖来源矛盾。

宏光圈、带孔光圈、SR、AB、非圆光圈圆弧、非简单/退化区域和其他未实现语法继续拒绝。实际样本的二期完整铜图遇到了退化区域，因此未通过；一期受限全暗铜证明及其整板检查通过。不能把合成测试通过说成支持任意Gerber。

`limits` 仅接受 `seconds=120,max_archive_bytes=50000000,max_member_bytes=20000000,max_members=256,max_commands=500000,max_primitives=100000,max_vertices=1000000,max_match_tests=10000000`。各值有限正数；解析、解压量、图元、匹配和时间分别计量，超预算拒绝不完整补证。默认无上下文的旧检查器行为保持。

## 验证与选择默认

测试包括伪完成、隐藏未完成网、跨来源/规则/坐标复用、原铜变更、桥接/断连、累计预算、末次超时、功能关系被篡改、出口未锚定、底层镜像、清铜删除端帽与核心、顺序恢复、孔洞、层映射以及非法输出覆盖。运行 `python -B -X utf8 -m unittest discover -s tests -v`；基准入口见 `tests/benchmark_escape_cells.py`、`tests/benchmark_checkpoint_repair.py`。

功能单元微基准改善了明确关系的满足情况，但其HPWL因满足指定距离而增加；简单四网板上预引出也增加了长度和耗时。局部拆线小例减少了冗余绕行，不能外推工业板布通率。完整分批改动清单、数据和未完成项见 [本次升级记录](UPGRADE_20261108.md)。没有足够跨板公平对比证据，故不切换任何算法默认。
