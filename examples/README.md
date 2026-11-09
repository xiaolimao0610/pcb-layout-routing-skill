# 项目参数示例

[automation-options.example.json](automation-options.example.json) 是显式开启安全续算、普通网局部拆线与局部布局反馈的配置片段，不改变工具默认。它没有真实器件、位置或网表；须与当前工程完整配置合并，保留真实源、线宽、固定件、分区及ground_strategy。单锚点功能关系和普通外围引出组须从当前板ID/手册填写，不能靠空列表猜测；示例将escape保持关闭。首批与协商、差分/等长、特殊拓扑/推挤不组合，自动局部布局也不与BGA fanout组合。完整字段见 [新增契约](../AUTOMATION_AND_GERBER.md)。

[initial-layout.example.json](initial-layout.example.json) 是从原理图导入后的分区初始化选项片段。使用前按 [分区初始布局](../COLD_START_LAYOUT.md) 从当前工程提供真实功能组、区域和固定接口；片段不含工程坐标、参考姿态或网表，不会自动猜测电机区或主控区。合并时保留原完整配置。

[dense-routing.example.json](dense-routing.example.json) 是密集区域布线的配置片段，启用有界几何搜索，保留显式扇出、几何推挤和差分换层为关闭状态。它没有真实工程数据；使用前按 [密集区域布线说明](../DENSE_ROUTING.md) 填写当前板的工艺、阵列和可移动对象，再与关闭新算法的基线比较。

[via-in-pad-preference.example.json](via-in-pad-preference.example.json) 是solver配置片段，将 `options.routing.via_in_pad_penalty` 设为120。它给新增盘中孔附加有限代价，优先盘外但允许必要的盘内连接；120只是示例搜索权重，不是净距。合并时保留已有 `options.routing` 及其他约束，不写入板的rules。适用入口和限制见[输入契约](../INPUT_CONTRACT.md#优先避开盘中孔)。

[foc-widths.example.json](foc-widths.example.json) 只保存此前双路电机项目明确要求的 `+10V` 网络最小线宽30 mil。这是特定项目的输入，不是通用技能规则，也不能据网名推断电流、铜厚或温升。

线宽示例是可解析的solver配置片段，只有 `constraints`，没有工程快照、来源身份、坐标、走线或铜区。使用前核对当前完整网表确有该网络且当前需求适用，再把规则合并到项目运行目录的真实配置；已有更强下限保留，矛盾配置必须解决，不能覆盖掉其他约束。网名不符时程序应拒绝，不自动改名匹配。

该示例不指定地网、叠层、额定电流、温升或算法开关。它不能单独作为完整任务输入，也不能作为写回授权或当前项目的状态证据。正常prepare/solve流程见根目录 [README.md](../README.md)，字段定义见 [INPUT_CONTRACT.md](../INPUT_CONTRACT.md)。
