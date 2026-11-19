---
name: pcb-layout-routing-pipeline
description: 用嘉立创工具包重排原理图、核对网表，或对当前 PCB 做布局、布线、铺铜、DRC 修复与来源匹配写回。适用于运行、续算和维护本包。
---

# PCB 与原理图任务入口

**已有 PCB 任务文件**，在工具根目录运行以下命令，将输出放在本次工程的独立目录。

```console
python -B pcb_pipeline.py solve --input task.json --output-dir run-01
```

先看终端摘要。失败时看阶段、受影响的网络和原因，再从本地 `run-01/result.json` 按需提取证据。续算查看摘要指向的逐网记录；原理图网表比较用 `--report netlist-diff.json` 保存完整差异；桥接采集用 `--output` 保存板数据。缺少输入时按[工程采集](CONTEXT_CAPTURE.md)导出，字段问题查[输入契约](INPUT_CONTRACT.md)对应章节。

**接续已有工程**，用 `pcb_project_state.py validate --state project-state.json --current-board fresh-board.json` 核对真实基线与已登记任务。结果未知、超时或 `recovery_required` 时先查原任务和当前板面，保留备份及现场。[状态与恢复](PROJECT_STATE.md)

**计算及写回**，保留完整网表、原铜、固定件和工艺间距，候选须经 `check_board`、`check_routed_nets` 核对。样板仅作标准参考，复用坐标、走线或铜区须有明确授权。实际写回前绑定 UUID 与 sourceFingerprint，备份原文，记录任务键并只派发一次。保存后核对文档身份、几何双向读回及原生 DRC。`plane_then_drc` 先布信号，再按实际层叠铺铜，仅修 DRC 点名的地网缺口，重铺复查。信号阶段或离线通过时仍报告 `full_board_accepted=false`。[嘉立创写回](eda/README.md)

| 当前任务 | 打开对应资料 |
| --- | --- |
| 原理图功能分块、块内直连、合页及逐引脚网表核对 | [原理图工作流](SCHEMATIC_LAYOUT.md) |
| 新布局、换板型或配置、刚导入器件散落板外 | [原厂手册与配权](LAYOUT_REFERENCE_AND_WEIGHTS.md) · [分区初始布局](COLD_START_LAYOUT.md) |
| BGA、密脚、差分换层或失败网络局部重布 | [密集布线](DENSE_ROUTING.md) · [局部修复与制造铜补证](AUTOMATION_AND_GERBER.md) |
| 装配、丝印、回流、载流与逐层检查 | [质量契约](QUALITY_CONTRACT.md) |
| 比较算法、设置预算或停滞换法 | [候选流程](WORKFLOW.md) |
| 首次安装、桥接或查其他命令 | [使用手册](MANUAL.md) · [桥接安装](bridge/README.md) |
