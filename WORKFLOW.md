# 多轮候选、停滞换法与默认值门槛

可选自动化闭环见 [新增契约](AUTOMATION_AND_GERBER.md)。`routing.checkpoint=true` 时，每个round独立保存计算进度和文件摘要；失败仍不输出通过板/写回计划。`options.automation` 只在该轮内部执行有限局部布局反馈，不能改变工作流候选比较的原基线、完整网表及方法序列。恢复旧计算须先查询原任务状态，以原task和上次已记录摘要在新目录恢复；不会自动重放原生动作。

这是显式的离线比较入口，不改变原 `pcb_pipeline.py solve`、默认算法或写回行为。换会话先验证 [project-state约定](PROJECT_STATE.md)，检查实际基线、通过证据及在途任务，再决定是否启动新轮次。运行数据放项目目录，不能放进工具包或当成别的工程基线。

## 配置与运行

```powershell
python -B pcb_workflow.py --input "$pcbRunDir/task.json" --policy "$pcbRunDir/workflow-policy.json" --output-dir "$pcbRunDir/workflow-001" --project-state "$pcbRunDir/project-state.json"
```

`--input` 是原完整task，`--policy` 为独立JSON文件；不能把新字段塞入原solver options。省略policy只比较当前任务的方法。`--project-state` 可省略，此时不持久更新接续索引，但仍保存完整轮次工件。输出目录必须全新，已存在则拒绝，不覆盖或重放。

```json
{
  "methods": [
    {"placement_method":"legacy","routing_method":"sequential","occupancy_grid":false},
    {"placement_method":"legacy","routing_method":"sequential","occupancy_grid":true},
    {"placement_method":"legacy","routing_method":"negotiated","occupancy_grid":true}
  ],
  "max_rounds": 6,
  "max_seconds": 600,
  "stagnation_rounds": 2,
  "objective": "wire_length_mil",
  "min_relative_improvement": 0.01
}
```

这是一组可选方法序列，不会写成生产默认值。未知字段、缺少方法字段、非法值、重复方法、与原约束不兼容的方法组合均报错；不删除原配置来迁就新方法。

| policy字段 | 默认与限制 |
| --- | --- |
| methods | 当前task的方法；1..6项，各项恰含placement_method/routing_method/occupancy_grid |
| placement_method | legacy或analytic_sa |
| routing_method | sequential或negotiated；negotiated必须显式occupancy_grid=true |
| max_rounds | 6，整数1..20，全部方法共享 |
| max_seconds | 600，有限正数，全部方法共享的轮间总时限 |
| stagnation_rounds | 2，整数1..20，连续无改善轮数N；大于max_rounds时总轮数先停止 |
| objective | wire_length_mil，可选via_count、board_area_mil2、hpwl，均以较小为优 |
| min_relative_improvement | 0.01，有限数且0≤值<1，严格降低目标且相对改善达到该值才算改善 |
| target_nets | 全部真实网络；非空、唯一、已声明网络列表 |
| target_regions | 整板包围盒；1..64个正面积世界mil矩形，位于板框包围盒内 |

target表示关注范围，**不删减完整网表、异网障碍、端子或最终检查**，也不代表区域外可动件自动固定。必须不动的器件继续用原fixed/locked/movable_ids/硬约束表达。HPWL是原代表点加权口径，不是实际布通证明。

每轮从同一完整输入独立求解，仅改变已声明的三个算法开关，以及`placement.seed=原seed+从0开始的全局轮号`。其他线宽、间距、允许层、原铜、固定姿态、地网策略及约束原样保留。每轮仍使用原task的内部max_iterations、节点数、轮数和各阶段时限。

流程max_seconds在轮间检查，不抢占正在执行的一次原求解或GEOS调用；一轮可能越过该时限，之后不再启动下一轮。实际耗时和停止原因如实记录。需要更紧的停止边界时同时缩小原task的逐阶段预算，不能通过放宽物理规则缩短搜索。

## 每轮合同与择优

比较紧凑候选时记录板面积、器件占用率、功能区空隙和 HPWL，同时标明人工给定的初始姿态、手动引出和自动完成的步骤。每轮保留相同输入与预算，并在交付时列出未完成的网络、判断依据及实际检查范围。

`round-001`等目录保存：

- `input-task.json`：实际完整输入与本轮方法、seed。
- `candidate-contract.json`：目标网/区域；源、网表、规则、板框、原铜和约束指纹及不可变条件；观测指标；预算、停滞阈值与实际停止原因。
- `result.json`：原求解报告及`candidate_rounds`中的本轮合同。一次外层候选可能包含原流水线的多个内部repair attempt，内部诊断仍完整保留。
- 仅当原离线或信号阶段实际通过时保存`board.json`、`writeback-plan.json`。信号候选仍为预览，等待原生地网闭环。

择优先比较已验证的范围：完整离线候选优先于信号范围候选，失败不能成为最佳；同范围才比较预先选定的单个几何指标和改善阈值。目标值相同不算改善，不能用运行时噪声反复更新最佳候选。失败的线长/孔数等为null，不用零冒充更优结果。`returned_routed_nets`只统计已验收返回板的范围，不声称失败运行内部一个网络也没布成。

首次取得合格候选视作改善；此后连续N轮未改善即停止当前方法。若methods还有未停滞的下一项且预算允许，实际执行下一方法；不会只改seed继续同一方法。切换实际开始后才记入`method_changes`，预算不足时只在合同中记录`next_method`，不冒充已执行。列表耗尽、总轮数或总时间耗尽时保留当前最佳候选和下一步。`METHOD_SEQUENCE_EXHAUSTED`、`ROUND_BUDGET`、`TIME_BUDGET`均不意味着无解。

`workflow-result.json`记录逐轮合同、方法切换、最佳候选路径、停止原因和下一步。其`passed`只表示存在该范围的已验证候选，`full_board_accepted`固定false；不能替代原生DRC、真实保存与双向读回，也不能称全局最优。

## 接续状态与失败处理

使用project-state时，计算前验证状态文件及全部引用哈希、输入完整基线和在途任务。未知/运行中任务阻止新流程，先按真实任务工具查询。每轮开始前登记唯一offline_workflow任务键，结束后由真实工件登记结果和终结键；状态更新使用旧摘要与revision防止并发覆盖，不自动重试冲突。

上一轮最佳候选只有在完整任务配置一致时参与比较；比较仅排除算法开关和seed，约束、目标层、地网策略及预算不同均拒绝混用。变更比较条件时显式建立新的状态文件，保留旧历史。相同指标的新候选不会覆盖已有最佳候选。

换会话后，新目录的 `round-001` 表示本次调用的第一轮。合同的 `round` 是跨会话的轮号，实际种子记录在该轮 `input-task.json` 的 `options.placement.seed` 中。恢复会核对同一比较条件下的已完成轮次，续用未试种子。旧版本若把一次重复种子计成无改善，恢复时按各方法已实际尝试的种子重新计数。已用不同种子达到停滞条件的方法仍然停止。

同一比较配置、目标指标及改善阈值下，历史合同中的停滞方法会被跳过，末轮未耗尽的连续无改善计数也会继承。换输出目录或换会话不能重启同一已停滞方法；全部列出方法已耗尽时在计算前拒绝，要求明确更换方法。没有project-state时只能保证单次流程内的停滞控制，跨会话必须先建立并验证状态。

进程中断或状态更新失败时保留工件与在途记录，不能把未知结果当作失败后自动重跑。恢复时核查`input-task/result/board`及源身份，按[状态约定](PROJECT_STATE.md)登记已完成结果或明确终结状态。状态索引不构成重新写入EDA的授权或验收凭证。

## 切换默认值的门槛

旧默认保持`legacy/sequential/occupancy_grid=false`，工作流不会自动修改任何默认源码。只有代表性基准满足以下必要条件，才进行单独可审查的默认变更：

1. 事先固定输入集、完整规则、目标、预算、指标及“明显改善”阈值。此版基准使用10%；同一案例多次运行，报告全部结果和中位数。
2. 原检查器通过才计布通；预算耗尽和未找到全部计入分母，不从样本中删除。信号阶段完成与整板离线完成分开统计。
3. 总布通率不降低，并检查各案例/板型是否退化。仅在双方匹配成功案例上比较线长/孔数/成功耗时；失败的缺失值不当零。
4. 至少一项核心指标达到预设改善阈值，同时公开其他指标的退化、未完成项和风险。硬验收、来源、原铜、地网与写回机制必须保持。
5. 小型合成集达标只说明该测试集的结果，不足以证明真实密集多层板布通率不降低。代表性真实工程、多层/大网表及布局联动证据不足时保留旧默认。

阶段交付需列改动文件、新选项、相同口径的对比表、未完成项与风险。已测数值、代理指标、原生DRC和实板功能证据各自标明范围，不相互替代。
