# 工程状态与换会话接续

新续算工件另见 [检查点契约](AUTOMATION_AND_GERBER.md#布线检查点)。开启 `routing.checkpoint` 后，solve/workflow 在独立文件保存计算进度，`result.calculation_checkpoint` 记录路径与SHA256；它不进入 `best_candidate` 或 `passed_evidence`。原状态中的运行键必须先查明已终结，再以新计算任务键、原输入和上次已记录的文件摘要恢复；不能把续算当作重放原生写回的许可。恢复保留累计预算、原铜和完整未完成网络，旧来源变化后拒绝复用。状态格式仍为原schema，不添加可手填的验收字段。

每个工程运行目录保存独立 `project-state.json`；不要把某块板的运行状态放进通用工具包。新会话先读取本文件并执行校验，再决定查询任务、继续计算或重新取源。状态是证据索引，不能替代实际几何、原生 DRC、保存或双向读回证明。

## 起步与接续

```powershell
python -B pcb_project_state.py init --board "$pcbRunDir/baseline-board.json" --output "$pcbRunDir/project-state.json" --next-step "核对约束后执行有限候选比较"
python -B pcb_project_state.py validate --state "$pcbRunDir/project-state.json"
# 刚从当前EDA重新采集后，再核对它是否仍对应这份历史基线。
python -B pcb_project_state.py validate --state "$pcbRunDir/project-state.json" --current-board "$pcbRunDir/fresh-capture/snapshot.json"
```

基线必须是完整规范板，有真实 `metadata.source.document_uuid` 和 `fingerprint`；仍需从当前嘉立创EDA现场导出核实，不把任意本地JSON当作现场真相。基线允许尚未布通。合成板必须保留 `test_only`，不得实板写回。`init` 不覆盖已有状态。

`validate` 不带 `--current-board` 只检查历史文件和已登记证据是否自洽，输出 `current_source_checked=false`，不能据此判断现场未变化。带该参数时，额外严格核对完整快照中的 UUID、来源指纹及用途；不匹配返回非零，不自动替换基线或迁移通过证据。快照是否新鲜仍由调用方通过实际采集保证；CLI不连接EDA。

每次写回、铺铜、局部修改后，保留旧状态并为新鲜来源建立新状态文件；交接时明确指出最新文件的绝对路径，并执行上述来源比较。不要让入口 `project-state.json` 长期停留在 `revision=0`，却把最终结果只写进自定义 `project-state-final.json`：交付摘要可以另存，但不符合本契约的摘要不能交给状态CLI或代替任务索引。已有任务未知时先查询，不能借建立新状态重新派发。

接续顺序：

1. 校验状态及所有文件的实际 SHA-256，再核对当前现场 UUID/sourceFingerprint。文件或来源变化即停止接续，不手改摘要“修好状态”。真实写回或重铺后以 fresh export 建立新基线、新状态文件，保留旧状态作历史证据。
2. 查看 `running_tasks`。存在 `pending/running/unknown` 时先用原工具按实际任务键查询；状态模块不派发任务，未知结果不重新执行。读取完成、保存失败及几何读回结果后再记录状态。
3. 查看 `best_candidate`、`passed_evidence`、`unresolved_issues` 和 `next_step`。只有对应范围已证明；信号阶段通过不表示地网、原生 DRC或整板完成。
4. 读取每个 `runs[].result` 工件中的 `candidate_rounds` 和实际指标；确认本轮目标、不可改变条件、观察指标和停止条件。停滞后换方法的策略见工作流契约，不能无限换随机数重试。

## 严格字段

`schema_version` 固定为整数1。所有状态对象均拒绝未知字段和重复 JSON 键，不接受 NaN/Infinity。以下顶层字段全部必填，空项用 `null` 或空列表，不省略。

| 字段 | 约定 |
| --- | --- |
| revision | 非负整数；更新必须恰好加1 |
| baseline | `{board:{path,sha256},source:{document_uuid,fingerprint,usage}}`；路径为完整基线JSON，usage为physical/test_only |
| best_candidate | `null` 或 `{run_key,board:{path,sha256},scope}`；必须对应已记录的独立检查证据 |
| passed_evidence | `{run_key,scope,report:{path,sha256},candidate:{path,sha256}}` 列表；由实际运行工件和检查器重新导出，禁止手填通过 |
| running_tasks | `{key,kind,status,output_dir}` 列表；status为pending/running/unknown；key唯一、输出目录为绝对路径 |
| task_history | 同样四字段，status为completed/failed/cancelled；终结键永久保留，不可再次登记为新任务 |
| unresolved_issues | 非空问题字符串列表；保留未连接、未知原生项、质量限制及证据缺口 |
| next_step | 非空明确动作，写明下一步依据或需要查询的任务键 |
| runs | `{key,input_task:{path,sha256},result:{path,sha256},candidate,status}` 列表；candidate为文件索引或null；status为offline_passed/awaiting_native_ground/failed |

所有文件索引使用绝对路径和64位小写字节SHA-256。基线中的 `fingerprint` 是实际 `metadata.source.fingerprint`，即工具包规范化的 `sourceFingerprint`，格式为 `sha256:` 加64位摘要；它与文件字节SHA-256不是同一种摘要，不能互换。

`scope` 仅支持由本模块验证的 `offline_geometry` 或 `signal_geometry`。状态不会把 `full_board_accepted`、原生 DRC、热验证或视觉查看等结论从单个 `passed` 字段推导出来。完整离线候选若声明 `return_path`，还会从实际铜重算双向检查并要求 `passed/complete` 均为true，不能凭伪造的结果状态通过；信号范围继续等待原生重铺。阶段4的后处理、回流和视觉等完整报告仍须按各自契约阅读，它们不会被此索引重新命名成整板通过证据。

每份状态或索引JSON不超过32MiB；最多1000次运行、1000项证据、64个运行中任务、1000项终结任务及1000项未决问题。普通文本最长4096字符，任务/运行键最长256字符。越界失败，不截断历史来伪装可接续。

## 记录一次求解

`record` 读取原 `solve` 输出目录里的 `input-task.json`、`result.json` 和成功时的 `board.json`，不自行启动求解或写回。

```powershell
python -B pcb_project_state.py record --state "$pcbRunDir/project-state.json" --run-dir "$pcbRunDir/round-001" --run-key "layout-route:round001" --select-best
```

运行输入完整板必须与基线相同，不仅比较UUID；不同方法和约束放在该运行 `options/constraints` 中。通过候选按输入重新施加完整规则、标签、端子与约束，再用原检查器复核；板框、规则、网表、封装/端子形状、固定或旧铜保护器件不可偷换。允许姿态、区域和旋转也按原布局配置检查。旧铜前缀及写回计划保护仍由原 `build_writeback_plan` 复核，状态模块不改变它。

信号阶段必须保留 `preview_only`、`native_ground_workflow` 和真实 `deferred_ground_nets`，只检查非延期地网的实际连通及全部硬几何。完整离线检查仍不授予原生验收。预算耗尽等失败运行可登记，但不能作为最佳候选或通过证据；失败目录残留成功板工件时要求先核实。

不加 `--select-best` 时只积累记录，不覆盖当前选择。该开关表示调用方已依据候选合同和相同预算比较选择了“当前已检查候选中的最佳”，不表示模块证明全局最优或自动满足默认算法升级条件。

## 运行中任务索引

```powershell
python -B pcb_project_state.py task --state "$pcbRunDir/project-state.json" --key "实际后台任务键" --kind "native-repour" --status pending --output-dir "$pcbRunDir/native-job"
# 用原桥接工具查询后，再把观察到的状态写入；以下命令本身不调用后台服务。
python -B pcb_project_state.py task --state "$pcbRunDir/project-state.json" --key "实际后台任务键" --kind "native-repour" --status completed --output-dir "$pcbRunDir/native-job"
```

相同未完成键可以更新观察状态，不能改写其任务类型和输出目录。终结键进入 `task_history`，不自动删除；运行中条目不能无记录消失。`completed` 只表示观察到任务结束，不代表保存成功、几何读回或 DRC 通过。实际查询收据仍须保留在输出目录。

`record` 自动终结同键运行中任务时，只接受 `kind="solver"` 或 `kind="offline_workflow"`，并严格核对已登记 `output_dir` 与实际 `--run-dir` 的解析绝对目录相同。另一目录的合法求解结果不能关闭本任务。原生重铺、保存、写回等其他任务必须按实际查询收据使用 `task` 入口终结，不能借solve报告代替；已进入终结历史的键也不能再用新solve记录复用。

## Python接口与并发保存

- `init_state(board_path, *, next_step=...) -> dict`：从实际完整基线建立空索引。
- `load_state(path, *, current_board_path=None) -> dict`：重新读取并验证全部工件哈希、来源及已登记候选；给出当前快照时再严格比较其来源，缺省只验证历史索引。
- `validate_state(state) -> dict`：同样验证内存对象，返回独立副本。
- `verify_candidate(task, candidate, result) -> str`：不依赖状态文件，直接复核候选来源、不可变条件与原检查器，返回offline_geometry/signal_geometry范围；新工作流和状态记录共享此入口。
- `record_run(state, run_dir, run_key, *, select_best=False) -> dict`：登记真实求解工件并增加revision，不写状态文件。
- `set_task(state, key, kind, status, output_dir) -> dict`：登记已派发/查询任务，不启动任务。
- `save_state(path, state, *, expected_hash=None) -> str`：新建或原子更新，返回新文件SHA-256。
- `file_sha256(path) -> str`：读取有限大小实际文件摘要，供比较并交换保存使用。

更新顺序是先读取旧文件摘要，再 `load_state`，完成内存修改后以原摘要保存。`save_state` 在同目录短暂持有独占 `.lock`，写临时文件、flush/fsync，再原子替换。若其他会话已修改状态、锁已存在、revision跳变或文件丢失，则失败，不重试覆盖。锁在异常终止后可能保留，此时先核实对应进程，不能以删除锁为借口重复派发任务。

更新必须保留原基线、既有 `runs/task_history` 前缀与终结任务键。状态不能覆盖被索引基线/工件；创建新的真实基线应使用另一个状态文件。核心接口的格式/来源错误抛 `PipelineError`，文件错误按 `OSError` 传播；CLI成功返回0，配置、来源或IO错误返回1。
