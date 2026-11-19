# 从当前工程取得真实 context

`pcb_context.py` 补齐原始工程到 `pcb_bridge.py export` 之间的证据采集步骤。它只读已连接窗口中的当前 PCB，不打开工程、不修改图元、不保存、不执行 DRC。脚本与 `pcb_bridge.py`、`eda_adapter.mjs` 和其余 Python 模块放在同一目录，使用本包 `requirements.txt` 的环境。

先启动官方 API 网关和本包桥接服务，在嘉立创EDA中打开目标 PCB。用 `python -B pcb_bridge.py windows` 确认窗口；多个窗口时必须明确传 `--window-id`。所有输出写入用户选择的全新运行目录，不能放进发布代码包。

```powershell
python -B pcb_context.py --window-id $edaWindowId --output-dir "$pcbRunDir\capture-01"
```

不传 `--spec` 时先采集真实证据，结果为 `inputs_required`，进程返回非零；这是尚未产生可求解快照。读取 `native-inventory.json`、`native-rules-and-stack.json`、`context-receipt.json`，在运行目录编写本次工程的 capture spec，随后使用另一个全新输出目录：

```powershell
python -B pcb_context.py --window-id $edaWindowId --spec "$pcbRunDir\capture-spec.json" --output-dir "$pcbRunDir\capture-02"
```

已拥有经过核对的 spec 时可直接运行第二步。不得把其他板的 spec、UUID、指纹或封装源复制成本板证据。用 `--base-url` 可指定已有的本机桥接地址；默认仅发现本机端口 49620–49629。

## 自动取得的资料

- 当前 PCB 文档标识、完整原生源和严格 `sourceFingerprint`；采集前后和最终导出均检查来源不变。
- 全部已放置元件的原生 ID、位号、姿态、封装与库引用，以及原生层列表。`component_pins` 与 `global_pads` 同时保留两个 API 接口的原始坐标、角度、层、焊盘号和形状元组，便于与局部封装及器件姿态互证；这些原始 getter 的单位差异不自动外推。
- 每个唯一库/封装引用的原始 `.elibz2` 文件。下载接口为 `sys_FileManager.getFootprintFileByFootprintUuid(uuid, libraryUuid, 'elibz2')`；保留 ZIP 字节、摘要、大小、原始成员名和完整 `.elibu` 源。压缩包只在内存中读取，不按成员路径解压；多个匹配文档、错误 UUID、超限或未知格式均拒绝。
- `COMPONENT_SHAPE=48` 层的唯一局部本体。直线 POLY/FILL 按原生局部轮廓取值；不从世界轴对齐包围盒还原尺寸，不把丝印代替本体。层 48 描述由封装作者提供，仍需对关键器件与原厂机械图核对。
- 原生 `CIRCLE` 或两段同向、每段与 180° 相差不超过 0.001° 的闭合 ARC 本体。由原始端点和角度求各自精确圆心半径，采用各圆外接正多边形的凸包作为保守轮廓。两条弧的圆心存在舍入差时保留两个圆，不强行合并。默认外接多边形径向误差上限为 0.001 mil；凸包额外保守量由收据中的圆心半径直接复核。任何其他曲线形状均不自动近似。
- 原生 RULE/RULE_TEMPLATE/RULE_SELECTOR 与 LAYER/LAYER_PHYS 原文记录，保留为审查证据；不会自动声称这些规则已完整翻译。

原始封装可能包含多段同身份 DOCHEAD 元数据。最终 `exportBoard` 仍要求且验证唯一实际几何块、完整图元清单、封装身份、焊盘坐标/尺寸/角度与运行时 API 互证。采集器不绕过现有适配器的任何几何或源匹配门槛。

部分客户端将附属过孔尺寸 getter 取为整数mil。在既有显式 `pad_dimensions="footprint_source"` context 模式下，只有 API 尺寸精确等于已验证封装源尺寸或其整数四舍五入值时才接纳；最终使用源码精确尺寸，并在附属孔metadata保留 `api_hole_diameter/api_copper_diameter`。封装UUID、所有权、层/类型、坐标与实例覆盖检查仍保留；默认API模式不获得此兼容。`pcb_context`生成的context已选择源码尺寸模式，无需新增capture spec字段，不得手工替换当前封装原文。

原生直线板框若API保留更多小数、源码序列化为四位小数mil，适配器仅在完整同ID板框源码可互证且自身精确闭合时使用源码端点，并输出 `metadata.outline_endpoint_encoding_proofs`。不通过“距离很近”自动闭合开口，也不扩大走线读回或几何间距容差。铺铜VIA锚点采取同样的坐标编码互证，保留原getter、源码及 `getter_position_quantization_proofs`；比例拟合与零平移仍用原精确残差标准，直径及身份不匹配继续拒绝。未知客户端差异仍按失败证据处理，不猜单位。

## capture spec 契约

JSON 顶层必须是对象；未知字段拒绝。所有长度为 **mil**，角度为度。此文件是本次工程输入，发布包不附带真实工程参数或可冒充实板的示例。

| 字段 | 类型与要求 |
| --- | --- |
| `schema_version` | 整数 `1`。 |
| `pad_coordinates` | `world` 或 `component_local`，由实际客户端检查后声明。 |
| `pad_coordinates_provenance` | 非空字符串，记录如何用偏心焊盘、已旋转/底层器件及原始封装坐标核实 API 坐标约定。未经检查不能填猜测。 |
| `copper_layers` | 从顶到底的真实物理铜层 ID 数组；完整匹配当前启用铜层，不按 ID 数值排序。 |
| `copper_layers_provenance` | 非空字符串，注明原生叠层证据。层列表中的显示顺序/颜色/可见性不能单独证明多层物理顺序。 |
| `rules` | 本包规则对象，必须显式给出下文列出的物理规则。其他字段按 `INPUT_CONTRACT.md` 和 `RuleConfig`，未知键拒绝。 |
| `rules_provenance` | 非空字符串，记录原生规则、制造能力及设计要求的来源与映射。采集原文不等于已正确翻译。 |
| `body_curve_tolerance` | 可选正数，默认 `0.001` mil；只控制本体圆形的保守多边形逼近，不是铜间距容差。 |
| `footprint_via_movement` | 可选 `fixed`（默认）或 `rigid`。默认固定带附属孔器件；选 `rigid` 时由现有适配器与布局/写回链路共同验证父器件和附属孔同步变换。 |
| `footprint_overrides` | 可选对象，以 `native-inventory.json` 中的 `footprint_key` 为键，为无法自动证明的本体提供来源明确的局部几何。详情如下。 |
| `board` | 可选有来源的真实板框/切孔 sidecar，字段遵循 `eda/README.md`。曲线、多环或接口无法读取的板框不能使用任意矩形代替；测试形状必须保持 `usage: test_only`。 |
| `tags` | 可选本包结构化元件/网络标签，遵循输入契约；不能从网名推断电流、线宽或噪声级别。 |

`rules` 必填：`default_clearance`、`minimum_trace_width`、`minimum_drill`、`min_annular_ring`、`board_edge_clearance`、`component_courtyard_margin`、`hole_to_hole_clearance`、`npth_clearance`、`sensitive_noisy_isolation`、`default_via_drill`、`default_via_diameter`、`clearance_matrix`。不需要类别间差异时明确将 `clearance_matrix` 设为空数组；其他值按实际设计制定。算法网格、数值公差和成本等未写字段采用程序默认值，不视作制造能力证明。

每个 `footprint_overrides` 值只接受：

- `units` 必须为 `mil`，`coordinate_system` 必须为 `footprint_local`。
- `body_polygon` 是真实未镜像/未旋转的局部坐标对数组；可选 `courtyard_polygon` 使用同一局部坐标系。
- `provenance` 为非空字符串，指向所用原厂机械图、原生图元、尺寸推导及保守误差记录。若从原生曲线逼近，必须证明覆盖边界并记录误差；不能用世界 AABB、文字标记范围或猜测矩形。

程序会检查 override 多边形简单且有正面积，但不会伪称它已自动验证原厂来源或完全包含真实本体；收据明确标记 `automatic_native_body_proof: false`。封装原文仍必须重新下载并经过全部严格导出检查。没有可信 override 时停止，保留 `body_error`，不要移除问题元件。

## 结果与后续流程

只有 `context-receipt.json` 中 `success: true`、`context_ready: true`、`status: exported` 同时成立，才会生成 `context.json` 与 `snapshot.json`。其他状态不产生可误用的 context。失败保留原始源、已取得的封装与失败原因；修正输入后重开全新采集目录。

`context.json` 供后续实时导出/写回复用，`snapshot.json` 进入本包 prepare/solve/validate 流程。任务信封中的约束、布局/布线参数需要按工程目标单独建立；这一步不自动生成电流、热设计、阻抗、EMI 或数据手册约束。支持的canonical几何执行完整离线几何/连通检查，并结合实际写回回读和原生DRC；plane_then_drc的信号阶段只表示等待原生铺铜和补地，不能称整板通过。

当前工程已有布线包含适配器不能证明的复杂原生铜弧/铺铜时，保留采集失败证据，按 [使用手册](MANUAL.md) 的pcb_native_copper.py inspect路径读取完整原生几何。可基于当前工程真实来源做原生线宽调整、重铺铜和DRC地网局部修复，但不能把这种路径标记为canonical导出成功或完整离线验收通过。改框、信号写回和宽度调整后，都重新读取当前来源再准备后续计划。样板默认仅采集面积、占用率与工艺标准，不采集其坐标/路线/铜区作为移植输入；复用这些内容需要用户明确授权。

原生封装获取 API 在官方参考中标记为 beta，客户端差异或未支持封装会明确报错。若当前文档没有真实板框、采用未知几何、封装修改后与库源不同、或 API 坐标无法和原生源互证，不能声称导出成功，也不能通过删除图元或放松检查获得绿色结果。

