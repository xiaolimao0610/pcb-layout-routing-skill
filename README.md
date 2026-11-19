# PCB 布局布线辅助 Skill

让 AI 协助整理嘉立创 EDA 专业版原理图，并用 Python 在本地计算 PCB 布局布线。AI 读取结果摘要，遇到布线问题可以追查对应的网络和引脚。

![合成布线用例：左侧为三个错位窄通道与待连接端点，右侧为穿过通道的布线路径](docs/overview.png)

| 原理图 | PCB 布局布线 | 嘉立创写回 |
| --- | --- | --- |
| 按功能分块、块内直接连线，逐引脚核对网表。[原理图画法](SCHEMATIC_LAYOUT.md) | 比较紧凑布局，处理多层布线、密脚扇出与差分换层。[布线策略](DENSE_ROUTING.md) | 写前备份，核对原生 DRC 与保存后的几何。[接口说明](eda/README.md) |

## 本地试跑

在仓库根目录执行以下两行，即可用合成电路测试布通、失败诊断和完整报告，无需启动嘉立创。

```sh
python -m pip install -r requirements.txt
python -B -X utf8 -m unittest discover -s tests -p test_cli_summary.py -v
```

使用自己的工程时，让 AI 从 [SKILL.md](SKILL.md) 开始。首次连接嘉立创或准备工程输入时，再查[完整使用手册](MANUAL.md)。

欢迎分享难布的板子或改进算法的想法。[参与讨论](https://github.com/xiaolimao0610/pcb-layout-routing-skill/discussions) · [报告问题](https://github.com/xiaolimao0610/pcb-layout-routing-skill/issues) · [贡献指南](https://github.com/xiaolimao0610/pcb-layout-routing-skill/blob/main/CONTRIBUTING.md)

允许非商业使用、修改与分享。商用请事先取得相应权利人的书面许可。[许可条款](https://github.com/xiaolimao0610/pcb-layout-routing-skill/blob/main/LICENSE.md)
