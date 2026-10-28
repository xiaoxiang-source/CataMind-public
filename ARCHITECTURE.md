# CataMind · 系统架构说明

> 本文面向**开发者与维护者**,回答"系统如何组织、模块各管什么、请求怎么流转"。
> 功能使用说明、API 清单与运维操作见 [README.md](./README.md) 与 [docs/admin-manual.md](./docs/admin-manual.md)。

---

## 1. 架构总览

CataMind 是一个**零第三方 Python 依赖**的单体 Web 应用:后端基于标准库 HTTP 服务,前端为单文件 SPA,重量级计算外包给外部专业栈(AiiDA / CP2K / Multiwfn / DP-GEN / PyTorch)。

```text
┌────────────────────── 接入层 ──────────────────────┐
│  index.html (单文件 SPA)      miniapp/ (微信小程序) │
│        └── REST over HTTP ──┘        └── 登录确认 / 审批 │
└──────────────────────────┬─────────────────────────┘
                           │
┌────────────────────── 服务层 ──────────────────────┐
│  server.py   ThreadingHTTPServer                    │
│  · REST 路由与参数白名单      · 三角色鉴权体系        │
│  · SQLite 读写与事务          · CM- 队列调度          │
│  · 结构化错误码              · 服务端日志与审计留痕    │
└──────────────────────────┬─────────────────────────┘
                           │
┌────────────────────── 执行层 ──────────────────────┐
│  catmat.py (CLI 统一入口) ──> catmat_tools/ (22 模块) │
│  通过 subprocess 调用异构环境 Python                  │
└──────────────────────────┬─────────────────────────┘
                           │
┌──────────── 外部计算栈(异构 conda 环境)────────────┐
│  AiiDA (verdi) │ CP2K │ Multiwfn │ DP-GEN/DeePMD    │
│  ASE │ PyTorch │ 国产大模型 OpenAI 兼容端点          │
└──────────────────────────────────────────────────┘
                           │
┌────────────────────── 数据层 ──────────────────────┐
│  catmat.db (SQLite,24 张业务表) + data/accounts/ 角色镜像 │
│  catmat.db-wal/-shm │ backups/ │ cp2k_runs/ │ logs/ │
└──────────────────────────────────────────────────┘
```

**设计取向**:服务层刻意保持"薄"(不引入 Web 框架),把不可控的重计算交给外部进程,从而获得极低的部署成本与较强的故障隔离。

---

## 2. 分层职责

### 2.1 接入层

| 组件 | 路径 | 职责 |
|---|---|---|
| Web 前端 | `index.html` | 单文件 SPA,零构建步骤,由后端同源托管;提供 13 个导航模块的交互界面 |
| 微信小程序 | `miniapp/` | 移动端入口:登录确认器(RFC 8628 风格一次性挑战)、待办审批(`pages/approve/`) |

小程序与 Web 端共用同一套后端 REST 接口与会话体系,不是独立业务系统。

### 2.2 服务层

| 组件 | 路径 | 职责 |
|---|---|---|
| HTTP 后端 | `server.py` | 唯一的业务中枢:REST 路由、参数校验白名单、鉴权、SQLite 事务、任务调度、审计日志、错误码统一封装 |

关键子机制:

- **三角色鉴权**:东翁( owner,唯一且不可删除)/ 掌书( admin,能力由东翁逐项授予)/ 清客( user)。SQLite 为事务权威源,`data/accounts/` 下的 JSON 为角色镜像,读取时核验互斥性与完整性。详见 [docs/auth-security-design.md](./docs/auth-security-design.md)。
- **CM- 队列**:无外部调度器时的 FIFO 排队机制(`server.py` §「平台内置 CM- 队列」)。
- **结构生成参数白名单**:杜绝参数静默回退,未知参数直接报错而非忽略。
- **结构化错误码**:统一返回 `{"ok": false, "error": "..."}`,便于前端空态渲染。

### 2.3 执行层

| 组件 | 路径 | 职责 |
|---|---|---|
| CLI 入口 | `catmat.py` | 唯一命令行入口。`serve` 启动服务;其余 15 个子命令把参数原样转发给 `catmat_tools/` 下对应脚本,旧脚本参数约定保持不变 |
| 工具包 | `catmat_tools/` | 工具模块（实际文件清单以仓库为准；下表为历史核心模块） |

**为什么要这一层**:各模块依赖互不兼容的运行环境(AiiDA / dpmd / 系统 Python),统一收口到 `catmat_tools` 后,由 `catmat.py` 以子进程方式调用对应环境的解释器,避免依赖冲突。

| 模块 | 职责 | 运行环境 |
|---|---|---|
| `cp2k_workchain_input.py` | 工作流输入文件构建**唯一基线**(以 `irru_adsorption_workchain.py` 参数为准),拆分为结构优化与单点能两套独立参数集 | 通用 |
| `catmat_workflow_chain.py` | 高通量链式工作流:逐结构 GEO_OPT → ENERGY(+PDOS/Bader)→ 下一结构 | AiiDA |
| `submit_cp2k_verdi.py` / `submit_catmat_workflow.py` | 通过 `WorkflowFactory + submit(builder)` 稳定提交 CP2K 计算与链式工作流 | `verdi run` |
| `aiida_node.py` / `aiida_probe.py` / `aiida_task_files.py` | AiiDA 节点详情查询、进程状态探针、任务文件路径检索 | AiiDA 环境 |
| `store_structures.py` / `load_structure.py` / `load_workflow_results.py` | 结构批量入库、StructureData 读取、工作流结果回收 | `verdi run` |
| `postprocess_pdos.py` | CP2K PDOS 后处理,解析 d 带中心等电子结构描述符 | 通用 |
| `bader_charge.py` | Bader 差分电荷:`delta = total - slab - ads` 逐格点相减 | 通用 |
| `ml_train.py` | 回归模型训练(预测 η_OER),支持 GPU 双卡并行 | dpmd 环境(PyTorch) |
| `al_train.py` | 主动学习:拟合代理模型并推荐下一批待计算结构 | dpmd 环境 |
| `ml_agent.py` | 「休go」ML 训练顾问:数据摘要 → 提示词 → 结构化结果解读(含校验与降级),不直接依赖 `server.py` | 通用 |
| `llm_vendors.py` | 国产大模型 OpenAI 兼容端点适配:厂商识别、URL 规范化 | 通用 |
| `dpgen_adapter.py` | DP-GEN / 势函数延拓闭环适配层,封装 dpmd 环境中的 `dpgen`/`dp`/`lammps` 命令 | dpmd 环境 |
| `build_param_db.py` / `build_param_refs.py` | 从 CP2K `cp2k_input.xml` 解析全量输入规范入库;扫描官方测试用例建立关键词反向索引 | 通用 |
| `chart_gen.py` | matplotlib 生成平台图表 | AiiDA 环境 |
| `setup.py` | AiiDA 工作流 entry point 注册(`aiida.workflows`) | 安装期 |

> **约定**:高权限 Agent 工具默认不对外暴露,调用需双层拦截,详见 README §「大模型接入与 Agent 模式」。

### 2.4 数据层

SQLite(`catmat.db`)为唯一权威存储,15 张表,详见 README §「数据层」。要点:

- **事务源 vs 镜像**:SQLite 是权威源;`data/accounts/admins.json`、`users.json` 是按角色拆分的镜像,读取时与库核验。
- **不入库内容**(已在 `.gitignore` 排除):`.env`、`llm_config.json`、`catmat.db*`、账号目录、`backups/`、`cp2k_runs/`、`logs/`、`.tmp/`、`__pycache__/`。
- **第三方大部头资料**:Multiwfn 官方手册(约 35 MB)不入版本库,请从官方站点获取 <http://sobereva.com/multiwfn/>。

---

## 3. 典型调用链路

以「高通量筛选 → ML 预测」为例:

```text
用户在 index.html 提交 HTS 批次
   │  POST /…  (携带 session)
   ▼
server.py: 鉴权 → 参数白名单校验 → 写 hts_batches / hts_batch_items → 返回
   │
   ├─► 从 catmat_tools.cp2k_workchain_input 取输入构建基线
   │
   ▼
catmat.py submit-catmat-workflow  →  verdi run (AiiDA daemon)
   │     GEO_OPT → ENERGY → PDOS / Bader
   ▼
catmat_tools.postprocess_pdos / bader_charge  →  描述符写回 ml_samples
   ▼
catmat.py ml-train (dpmd 环境)  →  模型与指标落库
   ▼
catmat.py al-train  →  推荐下一批待算结构  →  回到 pipeline 起点(闭环)
```

平台因此实现**"DFT 计算 → 描述符 → 模型 → 主动学习推荐 → 再计算"**的主动学习闭环。

---

## 4. 运行环境与依赖矩阵

| 环境 | 用途 | 被调用方式 |
|---|---|---|
| 系统 Python 3 | `server.py`、`catmat.py` 主体、多数工具模块 | 直接执行 |
| conda env `aiida` | AiiDA 节点/进程查询、图表生成 | `catmat.py` 子进程转发 |
| conda env `dpmd` | ML / AL 训练(Torch 2.8 + CUDA)、DP-GEN | `catmat.py` 子进程转发 |
| `verdi run` | 结构存取、工作流提交与结果回收 | AiiDA 守护进程执行 |

这种"**主控精简 + 专业环境隔离**"的格局,是平台能在单台工作站上跑通完整闭环的关键。

---

## 5. 设计约束与不变量

维护本项目时请遵守以下硬约束:

1. **零第三方依赖**:`server.py` 只用 Python 标准库,新增后端能力不得引入 Flask/FastAPI 等框架。
2. **输入构建唯一基线**:所有与 CatMat 工作流相关的 CP2K 输入,必须走 `catmat_tools.cp2k_workchain_input`,禁止各自拼字符串。
3. **参数不静默回退**:未知/非法参数必须显式报错。
4. **SQLite 为唯一权威源**:任何账号或业务数据不得绕过数据库直接写 JSON 镜像。
5. **写操作留痕**:涉及删库、权限变更、Agent 工具调用的操作必须写入 `audit` 表。
6. **敏感信息不入版本库**:密钥类配置一律走 `.env` 或已被忽略的配置文件。

---

## 6. 目录速查

```text
catmat-platform/
├── catmat.py                # 唯一 CLI 入口:serve + 15 个子命令
├── server.py                # HTTP 后端(零依赖)
├── index.html               # 单文件 SPA 前端
├── catmat_tools/            # 22 个功能模块(由根目录迁移而来)
├── miniapp/                 # 微信小程序(登录确认 + 审批)
├── data/                    # 种子数据(seed_*.json、vim 语法文件)
├── static/                  # 静态资源(logo、favicon)
├── docs/                    # 设计 / 运维 / 审计报告
├── tests/                   # 自动化测试与测试数据
└── .env llm_config.json     # 敏感配置(均已 gitignore)
```

> 说明：`data/accounts/`、数据库 `catmat.db`、备份 `backups/`、计算目录 `cp2k_runs/`、日志 `logs/` 均不入库。


## 桌面发行版 1.0.1（2026-10-23）

桌面客户端版本与服务端 2.4.1 分开管理。Windows x64 / Linux x64 便携包以及实验性 macOS arm64 包位于 `CataMind-1.0.1-Release-20261022/packages/`。客户端通过现有 https://catamind.example.com 中转访问同一网站，界面与数据权限共享；需要联网，不包含本地 CP2K/AiiDA 工作站。

本次修复 CSV 列映射选择器、导入文本变更校验、本地图表依赖、ExtraTrees 参数传递、静态文件路径边界；新增多模型对比/多描述符 AL 界面。多模型比较的填充与缩放仅拟合训练折；历史结果不自动重算。单模型旧指标仍不可用于排序。多描述符 AL 的全量预处理和噪声标定仍存在评估偏差，界面明确标为实验性。DPGEN 缺少真实轨迹帧→验收标签→训练数据回流，生产启动禁用，mock 仅模拟且不提交真实计算。

后端修改须在明确授权后重启 CataMind 才对常驻服务生效；此次没有重启生产服务。测试使用临时数据库；不要对生产库运行历史版本 test_core。详细功能对照和迁移边界见发行目录 reports/audit.html 和 MIGRATION.txt。


## 2026-10-28 调度与监控代码审查及增量修改

审查发现：CP2K CM 队列已有资源 governor，但训练四个入口（ML、AL、多模型对比、
多描述符 AL）直接启动，缺少共享并发限制；监控按命令行子串识别 CP2K，会误计
启动器和 shell，MPI 聚合只看直接子进程；队列把非空日志当成功，任务同步把单次
SCF 收敛当整个计算完成；训练同步仍用 kill(pid,0)，可能误认僵尸或抢先覆盖 reader。
顶部 CM 指标还被 AiiDA 的另一套状态计数覆盖。

本次变更：

- `catmat_process_monitor.py` 提供按进程名/脚本入口分类、递归父子链聚合、3 秒缓存及
  并发采样合并。CPU 为 ps 生命周期平均值，100% 为一个逻辑 CPU；RSS 聚合不是唯一
  物理页内存。原始命令参数不返回浏览器。PID 可见性不足显示警告，监控不作为准入来源。
- 顶部 CPU 卡片支持鼠标/键盘打开弹窗，每 5 秒刷新，关闭或页面隐藏停止采样。
  ML/AL 和研究面板提供同一入口；响应过期不覆盖新弹窗。CM 顶部指标只由 metrics 维护。
- 四个异步训练入口共享启动锁及运行名额；`CATMAT_ML_MAX_RUNNING` 默认 2，满额明确
  拒绝并提示重试，**没有新增训练持久队列**。`CATMAT_ML_THREADS` 默认 4、范围 1–16，
  限制 n_jobs/nthread/num_threads 与 BLAS 环境。它不是 cgroup 硬 CPU/内存隔离，
  嵌套并行库仍可能产生更多线程；没有把训练内存预测冒充实测预算。
- 正常训练收尾要求退出码 0 与成功 JSON；活跃 reader 独占收尾，僵尸用 proc_alive 判定。
- CP2K 状态按最后一次 PROGRAM STARTED AT 重置，要求正常结束、无 ABORT/SCF 未收敛，
  队列有 Popen 时同时核对退出码。流式读日志避免整文件占内存。
  **done 仍是程序执行状态，不能替代几何、振动模式数等科学验收**；服务重启后历史退出码不可恢复。
- 已入队项遇到未知资源快照继续等待，不提交、不误标永久失败；等待原因写回队列并展示。
  超过宿主硬限制的请求仍失败；原 FIFO 与 governor 规则保持。

边界：AiiDA/HTS/工作流仍由既有 governor 与外部引擎管理；DPGEN 生产闭环继续禁用。
本次没有将平台调度器与 A/B 科研调度器合并，也没有放宽服务器资源政策。同步训练旧入口、
跨服务重启的 PID 身份恢复、逐作业峰值预算和 GPU 独占队列仍需要后续独立设计。

验证使用临时数据库与 mock 进程，未提交真实计算、未写生产数据库。新增回归见
`tests/test_process_scheduling.py`、`tests/test_process_monitor_frontend.js`；同时运行资源
准入、资源默认值、全局锁静态/语义和现有前端回归。修改前备份在
`backups/scheduling-20261028/`。后端上线需要按服务器总则明确授权后重启 CataMind。

## 2026-10-28 后处理工具集 v1

入口：侧栏「后处理工具集」，CLI `python3 catmat.py postprocess-tools catalog`。
API 归入原有 `/api/postprocess` 权限组，执行需 run_pipelines；任务与产物仅本人或东翁可见。

| 工具 ID | 方法和输入 | 输出及限制 |
|---|---|---|
| density_difference | 同网格、同原点、同向量的 total/slab/ads 标量 cube；显式 e/Bohr³ | Δρ cube、积分/积累/耗尽电子数；不是 Bader 分区或吸附物转移电荷 |
| bader_import | 实际 ACF.dat + 密度 cube + 原子顺序确认 | 原子盆电子数；有逐原子参考电子数才给净电荷；检查密度积分、真空电子数及预期总电子数 |
| bader_run | 密度 cube，可选分区参考 cube；CATMAT_BADER_BIN | 真实外部程序正常退出且产生有效 ACF.dat 才返回；本机未配置时禁用，不安装/模拟替代 |
| pdos | 同计算、同帧的 PDOS 文件夹，显式展宽/能窗 | KIND/元素指标、费米能参考、PDOS 曲线 CSV；保留已有独立 PDOS 验收/入库链 |
| output_audit | CP2K 输出文件 | 最后一次尝试的能量与结束/SCF 标记；不能代替退出码、几何/振动科学验收 |

模型：`catmat_tools/postprocess_toolkit.py` 注册科学工具，`catmat_postprocessing.py` 管理
平台任务，`catmat_tools/external_runner.py` 处理外部程序退出码、超时、输出长度与 JSON 协议。
Web 服务仍只依赖标准库；PDOS worker 使用 AiiDA Python 的 NumPy。

任务由 API 写入 `postprocess_jobs` 表，状态 queued → running → done / needs_review / failed。
服务重启后的未完成记录改为 interrupted，不自动重试；done 仅表示分析程序执行完成，
所有结果的 review_status 固定为 preview。没有通用自动验收或自动写入训练集。
现有 `/postprocess/preview → accept → admit` 的 PDOS 科学验收流程继续保留。

每次运行独立保存在 `data/postprocess_runs/<随机ID>/`：请求、result.json、分析产物；
记录工具版本、代码及输入 SHA256、参数与时间。输入在运行前后均核对，变化即拒绝结果。
API 文件输入限制在 AiiDA、平台计算/数据目录、受管临时目录及 `/home/user/phd`、
`/home/user/workflow`（解析软链接后校验）；下载只允许成功结果登记的产物，采用流式发送。
全局最多 2 个分析 worker，每个默认单线程、300 秒超时。cube 采用紧凑浮点数组，最多
2000 万格点；仅支持正网格数、Bohr 坐标、单标量数据，不猜测多轨道或负网格单位。
目录输入中的大规模 PDOS 仍可能超时，需离线处理；未引入系统级内存硬隔离。

科学边界：cube 向量是逐网格步进，体素体积为三向量行列式绝对值，不再除网格点数。
三份差分密度的冻结几何、电子数、基组/赝势与计算设置仍须由使用者确认。
Bader 的净电荷是显式参考电子数减盆电子数；赝势密度的分区可靠性及参考重构密度需人工核查，
守恒检查不证明网格/原子盆收敛。原子序号确认不等于自动识别跨文件映射。
PDOS 保留 p/d 通道但不能把主族元素的 d 通道当物理 d 带；LDOS 不自动视为严格 Fukui/软度。

扩展规范：新增工具必须登记输入/参数白名单、可用性、单位、方法版本、输出 schema、
来源指纹与质量检查，并有合成边界测试和小算例验收。规划中的功函数、振动/Shermo、
吸附能/CHE 与局域反应性尚未开放。振动热力学校正遵从既有 CP2K → Shermo 链；
不同方法/参考态能量不得混用，不能自动用零值补缺失校正。

兼容变更与其它修复：

- 旧 `bader-charge` CLI 改为统一 JSON；旧 bader-diff API/agent 名称保留，但需 density_unit，
  charge_transfer 固定 null，返回 density_integral_e，避免延续错误语义。
- `/api/ml/train` 和导入后训练现在返回后台 task_id；导入先保存并提交新数据集，
  仅使用本次数据集启动训练，失败原因回传 training_error，前端不再提前显示训练完成。
- Multiwfn 生成、图表、AiiDA 文件查询增加退出码核对；Multiwfn 探测使用配置路径。
- LAMMPS 模型列表通过 `-var models` 传入，输入需含 `${models}`；DPGEN 生产闭环继续禁用。
- 任意 Python 的 ASE 在线执行暂时禁用，保留声明式结构生成和文件导入；字符串黑名单
  不视为安全隔离。模型生成代码仍可审阅，但不在线运行。

科学依据：
[ASE cube 写出实现](https://docs.ase-lib.org/_modules/ase/io/cube.html)、
[Henkelman Bader 输入/输出与赝势注意事项](https://theory.cm.utexas.edu/bader/)、
[LAMMPS 命令行变量规范](https://docs.lammps.org/Run_options.html)。
本机 Multiwfn 手册与 `docs/Multiwfn功能体系与CP2K集成知识库.md` 用于功能边界参考，
不把菜单中存在某功能视为平台已实现。

验证：新科学/调用/权限回归 `tests/test_postprocess_toolkit.py`、临时数据库 HTTP 集成
`tests/test_postprocess_http.py`、界面输入协议 `tests/test_postprocess_frontend.js`。
备份：`backups/postprocess-toolkit-20261028/`。不得对生产库直接执行测试建表/更新。

### 后处理输入选择（2026-10-28）

`POST /api/postprocess/tools/browse` 接收 `{field,path}`，空 path 返回可浏览根目录；只展示该输入类型与子目录，限制于计算目录及当前账号上传目录，隐藏路径和越界符号链接不可选。单目录最多扫描 10000 项，截断时明确提示。

`POST /api/postprocess/tools/upload` 接收 `{field,files:[{name,content_b64}]}`，沿用 postprocess 写入权限。每批最多 100 文件、32 MiB 解码内容，HTTP 请求体预读上限 45 MiB。文件名、类型、编码和完整批次先校验；写入 `data/postprocess_inputs/<uid>/<uuid>/`，PDOS 返回目录，其他类型返回单文件路径。前端提供服务器浏览子弹窗与本机原生文件选择，多选 PDOS 自动组成目录。上传成功不代表科学验收通过。
