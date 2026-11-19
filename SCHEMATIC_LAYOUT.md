# 原理图按功能分块重排

打开当前嘉立创工程，沿输入、处理、输出读一遍电路。把能作为一个回路理解的器件放到一起，例如驱动器、栅极电阻和接口，或变换器、电感、输出电容和反馈分压。用真实引脚编号追踪反馈、采样和地线。多路相同功能沿用同一种阅读方向，保留每路实际器件与位号。

## 排版与分页

1. 导出当前工程备份、各页原始文档、原生网表与 ERC 详情。记录工程、原理图、各页 UUID 和原文摘要。已有 PCB 时一并记录器件 Unique ID、封装与 PCB 的对应关系。
2. 按功能和信号方向给每个块安排器件。保留器件型号、引脚、封装、属性、显式 NC、用户注释和经确认的层次结构。位号和短线需要的空间也计入块宽高。由符号库与当前实例取得朝向、引脚触点和实际轮廓。
3. 在相同可读字号下，先试将关联块和重复通道排在同一页。用图框的可用矩形评估合页，给标题、页脚、图签和接口文字留位。放不下时可换用户接受的图幅，或沿清晰的模块边界分页。

`python schematic_pagination.py blocks.json pages.json` 生成平移候选。输入示例：

```json
{
  "bounds": [0, 0, 1100, 750],
  "blocked": [[520, 0, 1100, 130]],
  "gap": 15,
  "blocks": [
    {"id": "通道甲", "width": 420, "height": 230, "related": ["通道乙"]},
    {"id": "通道乙", "width": 420, "height": 230}
  ]
}
```

`placements` 给出各块页码和原点。用它平移块内所有器件及触点再走线。搜索受 `max_pages`、`max_candidates`、`max_seconds` 限制。检查 `complete` 与 `pending`。

## 连线候选

同页的串联电阻、采样、反馈和去耦用原生正交导线连到真实引脚。分支在实际三通处相接。异网交叉只允许两条直线从各自内部穿过。同一页可直读的路径用实线表示，跨页信号在本页实际连线旁保留一次网名。对显式指定的共用电源、地区域可以使用各自的电源符号或少量清晰的网名。不同地网及其跨接器件各自保留。

`python schematic_wiring.py page.json wires.json` 对一个已排好器件的图页生成候选。最小示例：

```json
{
  "bounds": [0, 0, 150, 100],
  "grid": 5,
  "pins": [
    {"ref": "U1", "pin": "1", "net": "SENSE", "x": 25, "y": 50, "dir": "R", "block": "采样"},
    {"ref": "R1", "pin": "2", "net": "SENSE", "x": 95, "y": 50, "dir": "L", "block": "采样"}
  ],
  "obstacles": [[0, 40, 24, 60], [96, 44, 110, 56]],
  "cross_page_nets": [],
  "shared_nets": []
}
```

- 坐标用当前原理图文档的单位，纵轴向上。所有触点与有效图纸范围使用同一坐标系；选择能精确表示所有触点的 `grid`。水平或竖直离开符号的方向由 `dir` 指定。明确空接的引脚使用 `net: null`。
- `obstacles` 包含符号主体及引脚内侧占用、文字、图框和图签禁入矩形，把真实触点和向外的第一段引出线留在可用区域。另加仅供网名避让的 `label_obstacles`。检查位号、数值、符号内部与边缘的实物显示效果。
- `shared_nets` 为显式的共用网络。每个功能块内仍尽量连成一棵线树。仅这些网络的个别引脚可设置 `port: true`，并可用 `stub` 指定引线长度。`cross_page_nets` 仅控制跨页名称显示。A* 搜索到已布线树，可从分支中点接入。默认最多展开 500000 个状态、搜索 30 秒，单页最多 2000000 个网格单元，可通过 `max_expansions`、`max_seconds`、`max_cells` 设置。
- 每条线有原网名 `net` 和正交折线 `points`。T 形结点已拆成相接的原生导线端点。有显示名的线含 `show_name` 及文字中心 `label`，按水平居中、字号 7 显示。这些是候选的逻辑信息。对保存时强制显示 NET 属性的嘉立创版本，运行 `python schematic_wiring.py page.json wires.json --native-names`，或调用 `native_name_seeds(page, wires)`。每个实际相连的同网线岛选一条 `explicit_net: true` 的分支来确定显示位置。用 `wire_tree_indices(page, wires)` 求各岛成员，同一棵树的所有原生 LINE 必须共享同一个 WIRE 的 `lineGroup`，该 WIRE 挂一个 NET 属性。相同网名但彼此分开的岛分别建组。禁止按每条分支分别创建 WIRE 再依赖端点重合继承网名，这会使嘉立创为部分引脚重新分配自动网名。再按这些实际可见的名字为位号和数值留位，保存后读回显示状态并比较原生网表。
- 检查 `completed`、`unrouted`、`geometry`、`label_conflicts`。未布通时保留报告，反馈给块内摆放；名称摆放冲突时调整可用空间。独立函数 `audit_wires(page, wires)` 从实际折线重新核对引脚、连通分组和异网交叉。跨页标签和文档内部原生网络语义仍要从编辑器核验。

## 放置位号和数值

在已有导线及可见网名的位置上运行 `schematic_annotation.annotate_page(page, routed, options=None, measure=None)`。图页额外提供 `parts` 数组，每项含位号 `ref`、数值 `value` 和真实符号主体边界 `body`。图例和图签等禁入区域列在 `obstacles`。可调字号 `ref_size`、`value_size`、`wire_name_size`，边距 `gap`、位移 `max_offset` 和每器件/整页候选数 `max_candidates_per_part`、`max_candidates`、总时限 `max_seconds`。通过 `measure(text, size)` 可使用实际字体返回宽高。

读取 `styles` 中每个器件的位号和数值坐标，按返回的字号和左中对齐方式赋给原生属性。对 `unplaced` 中的条目保留已有文字，移动器件或增加局部空间后重试。重看整页及密集区域时同时核对关键引脚号、位号和网名。

## 写回与验收

跨页还会改变编辑器根据原图页派生的 `Channel ID`。调用 `schematic_native_wires.preserve_channel_ids(records, original_netlist, moved_ids)`，按迁移器件 UID 取得基线网表中的原值。已有原生 ATTR 即使值是 null，也要保留该记录的 ID 与元数据，更新其值。仅在实例缺少整条属性时才新建。双向网表核对中逐项比较 Channel ID；补完后重新生成原生导出验证。

嘉立创跨页迁移器件时，原来从符号库继承的模型属性可能从新导出的网表中消失。在目标页写回前调用 `schematic_native_wires.model_attributes_for_move(records, original_netlist, moved_ids)`，根据当前工程基线网表和待迁移器件的 Unique ID，为缺失的 `3D Model`、`3D Model Title`、`3D Model Transform` 分别补入原值作为隐藏属性。已有一致属性会原样保留，遇到已有值不同或 ID 冲突会报告并停止。保存后用整张原生网表比较复核这些字段。

可调用 `schematic_native_wires.wire_records(page, wires, existing_ids, project_uuid=..., page_uuid=..., source_fingerprint=...)` 编码已核对的 WIRE、LINE 和 ATTR 候选。每棵连续线树输出一组 WIRE 及其 LINE、NET，候选与页身份和原文指纹绑定，坐标会从编辑器正向纵轴转换为文档纵轴。宿主适配器负责保留原有对象、合并票号与原生写回。

这些命令生成离线候选和核对结果。原生写回要使用与现场文档对象类型、版本匹配的适配代码。确认备份可读和目标身份，派发前再次核对原文摘要。在修改范围内移动既有器件、保存身份。合页时保留原 Unique ID、符号、封装、用户注释与未知图元，原页保留至逐对象迁移和新页读回完成。任何未能证明可无损迁移的内容都应报告。只派发一次，若回复超时先读取现场确定已执行范围。

保存后重新从嘉立创导出整张原理图的原生网表，运行：

```console
python schematic_netlist.py before.enet after.enet --report netlist-diff.json
```

要求双向比对器件身份、BOM 与封装属性、每个真实引脚网名及每网完整端点集合。空网名与显式 NC 图元分别核对，已有 PCB 的关联保留。重新读取各页的符号位置、原生导线、网络名显示与注释，运行原生 ERC，对照重排前细项，打开实际图页检查字体、交叉和边界。交付页数、模块端点直连数或组数、剩余标签、网表差异和 ERC 结果。

对由正交原生 WIRE/LINE 形成、可从同一条连续线树唯一确定网名的连接，使用以下命令对比保存后的可见网名、可沿导线到其他器件的引脚数、实际 NC 标记和物理接触关系：

```console
python schematic_readback.py AFTER_DIR readback.json --baseline BEFORE_DIR
```

每个目录包含完整的 `netlist.enet`、原生图页及同名的 `<页名>-geometry.json`；后者来自原生 API 的器件编号、引脚触点和 NC 状态。如发现未知图元、跨图层次符号或其他未实现的连接形式，应使用对应原生证据复核。

导出时将同一批原生图页、网表和实测几何放入同一目录。逐页核对原生器件身份；若页内器件缺失、多出或身份冲突，重新采集整批文件，再作引脚和网表比较。

实际字体已参与排版时，把相同的 `measure(text, size)` 宽高测量函数交给原生网线编码器。对已有载体与位置的网名，编码器核对避让并保留坐标，冲突时返回给排版步骤处理。对于尚未排字的线树，继续使用原有载体选择和排字算法。
