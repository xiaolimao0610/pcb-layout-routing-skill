# 桥接安装与运行

接口链为：Python 客户端 → 本机 Bridge Server → 嘉立创EDA内的 Run API Gateway → EDA API。两端都需要，不能只运行 Python 客户端。

## 电脑服务端

`server/bridge-server.mjs` 来自官方 easyeda-api 技能的 1.1.36 版本。仅依赖 ws 8.21.3，Node.js 18+；package-lock.json 只保留实际运行依赖。服务绑定本机 127.0.0.1，自动检查 49620–49629，发现已有兼容桥接时复用。

从工具根目录执行：

```powershell
npm --prefix bridge/server ci --omit=dev
node bridge/server/bridge-server.mjs
```

长任务使用后台写回协议。服务端单次执行请求有约 30 秒超时，超时不撤销已经开始的 EDA 工作。

## EDA 网关

优先从[官方扩展商店](https://jlc-ext.com/item/oshwhub/run-api-gateway)安装 Run API Gateway，启用“允许外部交互”和“在顶部菜单显示”。菜单包含 Reconnect、Stop Connection、Toggle Auto-Connect Status、About。扩展会扫描本机桥接端口并校验服务握手。

`gateway/` 随包提供[官方源码](https://github.com/easyeda/eext-run-api-gateway)及 Apache 2.0 LICENSE，版本 1.0.6，来源提交 e55348027d1008a3d448b8b228fbca7b82dc8381。保留 src、构建脚本、锁文件、扩展清单、图标及语言文件；没有附带依赖目录或声称已在任意客户端验证兼容。

需要自行构建时，使用 Node.js 20.17+，从工具根目录执行：

```powershell
npm --prefix bridge/gateway ci
npm --prefix bridge/gateway run build
```

产物在 `bridge/gateway/build/dist/`，将生成的 .eext 导入嘉立创EDA扩展管理器。当前打包只提供源码，未执行依赖安装或网关编译。构建命令会重建该网关目录内的 dist；不要把个人数据放进构建目录。官方 extension.json 自带的 UUID 是扩展标识，不是 PCB 工程标识。

## 连接检查

从工具根目录执行 `python -B pcb_bridge.py health` 和 `python -B pcb_bridge.py windows`。使用返回的真实窗口 ID；多个窗口时不能默认选中任意 PCB。若无连接，检查网关是否加载、是否允许外部交互，以及 Reconnect。

已有工程需要登录或打开时，由使用者正常进入目标文档。本工具不会安装/启动嘉立创EDA，不会上传板子至其他服务，也不替代编辑器自己的权限和版本要求。
