# 催化智构 CataMind · 管理员手册

| 项目 | 内容 |
|---|---|
| **文档版本** | v1.1 |
| **适用平台版本** | v2.4.1（鉴权协议 v4） |
| **更新日期** | 2026-09-04 |
| **适用对象** | 东翁、运维人员、二次开发人员 |
| **部署形态** | 本地服务器 + 公网安全中转（Linux + Python 3.9+，零第三方依赖） |

> 本手册以「可执行操作条目」为单位组织。每个条目统一包含：**适用场景 / 前置条件 / 操作步骤 / 预期结果 / 风险提示与回退方案**。
> 所有命令、路径、参数均与 `server.py` 实际实现一一对应，未列入的功能即平台当前不具备。

---

## 目录

- [1. 术语表](#1-术语表)
- [2. 环境依赖与安装](#2-环境依赖与安装)
- [3. 服务启停与状态](#3-服务启停与状态)
- [4. 日志查看与监控](#4-日志查看与监控)
- [5. 配置修改](#5-配置修改)
- [6. 数据备份与恢复](#6-数据备份与恢复)
- [7. 版本更新与回滚](#7-版本更新与回滚)
- [8. 常见故障排查](#8-常见故障排查)
- [9. 服务器迁移与重新部署](#9-服务器迁移与重新部署)
- [附录 A 错误码对照表](#附录-a-错误码对照表)
- [附录 B 环境变量速查](#附录-b-环境变量速查)
- [附录 C 关键文件与目录](#附录-c-关键文件与目录)

---

## 1. 术语表

| 术语 | 说明 |
|---|---|
| **平台 / CataMind** | 催化智构 CataMind 催化材料智能计算平台，即本仓库 |
| **server.py** | 后端主程序（单文件，零第三方依赖，`http.server` + `sqlite3`） |
| **index.html** | 前端单文件 SPA，由后端同源托管，**每次请求实时读取** |
| **catmat.db** | SQLite 主数据库（15 张表，含 6.7 万条 CP2K 参数库） |
| **AiiDA** | 计算工作流管理框架（2.8.0），真实计算经 `verdi run` 提交 |
| **daemon** | AiiDA 守护进程，负责执行提交的计算与工作流，**与 Web 服务相互独立** |
| **WorkChain** | AiiDA 工作流类；本平台注册了 `catmat.workflow`（方案 A）与 `catmat.batch`（方案 B） |
| **Multiwfn** | 波函数分析程序，平台用它真实生成 CP2K 输入文件（.inp） |
| **CP2K** | 第一性原理计算程序（2026.2） |
| **休go** | 平台内置 AI 助手（昵称小休 / 小go），Agent 模式下可调用平台工具 |
| **agent_memory** | 休go的持久化记忆表，与 `knowledge`（知识库）双向同步 |
| **audit** | 审计表，记录所有写操作与 Agent 工具调用 |
| **HTS** | 高通量筛选模块 |
| **WAL** | SQLite 预写日志模式，提高并发读写能力 |
| **主体备案号** | ICP 备案的主体级编号，格式 `省简称+ICP备+数字+号`（如 `ICP备案号请由部署者配置`） |
| **网站备案号** | 主体备案号下按网站追加序号，格式 `…号-1`、`…号-2`；**除广东外各省须悬挂此号**（详见 5.7） |

---

## 2. 环境依赖与安装

### 2.1 检查运行环境

- **适用场景**：首次部署前，或迁移到新服务器后确认基础环境是否满足。
- **前置条件**：拥有服务器终端访问权限。

**操作步骤**

```bash
python3 -V                 # 要求 3.9+（实测 3.9.25）
sqlite3 --version          # 要求 3.27+（备份用 VACUUM INTO，实测 3.34.1）
ls /home/user/bin/Agent/catmat-platform/server.py
```

**预期结果**：Python ≥ 3.9、SQLite ≥ 3.27、平台目录存在。

**风险提示与回退**：无。若 Python 过低，需另行安装（平台服务端本身不需要第三方包）。

---

### 2.2 获取平台代码

- **适用场景**：新服务器部署；或从备份恢复平台。

**操作步骤**

```bash
# 方式一：从已有 git 仓库克隆（推荐，可回滚）
git clone <仓库地址> /opt/catmat && cd /opt/catmat

# 方式二：直接拷贝目录（含数据库与数据）
rsync -av /home/user/bin/Agent/catmat-platform/ newserver:/opt/catmat/
```

**预期结果**：目录内包含 `server.py`、`index.html`、`catmat.db`、`data/`、`docs/` 等。

**风险提示与回退**：`rsync` 会覆盖目标同名文件。若目标机已有数据，先执行 [6.2 手动备份](#62-手动备份) 再拷贝。

---

### 2.3 配置外部依赖路径（环境变量）

- **适用场景**：外部程序（AiiDA / Multiwfn / CP2K）路径与默认值不一致时；迁移后必做。
- **前置条件**：目标机已安装 conda 环境 `aiida`、`dpmd`，以及 CP2K、Multiwfn。

**操作步骤**：编辑平台根目录的 `.env` 文件（或直接在启动 shell 中 export）：

```bash
cat >> .env <<'EOF'
CATMAT_AII_PY=/opt/conda/envs/aiida/bin/python
CATMAT_VERDI=/opt/conda/envs/aiida/bin/verdi
CATMAT_ML_PY=/opt/conda/envs/dpmd/bin/python
CATMAT_MULTIWFN_BIN=/opt/Multiwfn/Multiwfn
CATMAT_CP2K_ROOT=/opt/cp2k-2026.2
CATMAT_AIIDA_FILES=/home/user/aiida_files
CATMAT_CP2KVIEW_DIR=/home/user/bin/cp2kview
CATMAT_DEFAULT_NPROC=128
EOF
```

**预期结果**：`GET /api/versions` 能正确探测到 CP2K / Multiwfn / GPU 版本。

**风险提示与回退**：路径写错会导致对应功能不可用（如 INP 生成失败），但**不会损坏数据**。改回正确路径并重启服务即可。

---

### 2.4 安装 AiiDA 工作流 entry point

- **适用场景**：首次部署，或升级了 `catmat_workflow_chain.py` 之后。
- **前置条件**：已激活 conda `aiida` 环境。

**操作步骤**

```bash
conda activate aiida
cd /home/user/bin/Agent/catmat-platform
pip install -e ./catmat_tools --no-deps  # 注册 catmat.workflow / catmat.batch
verdi plugin list aiida.workflows | grep catmat
verdi daemon restart                # ⚠ 必须重启 daemon 才能加载新的 WorkChain
```

**预期结果**：`verdi plugin list` 输出中包含 `catmat.workflow` 与 `catmat.batch`。

**风险提示与回退**：
- ⚠️ **`verdi daemon restart` 会中断 daemon 正在运行的计算作业**，必须在无作业运行时执行。
- 未重启 daemon 时，提交的工作流会在 daemon 侧报模块导入错误（`ImportError: module 'catmat_workflow_chain' could not be loaded`）。此时只需补一次 daemon 重启，已提交的工作流需重新提交。

---

### 2.5 注册 AiiDA 计算 code

- **适用场景**：新环境首次使用「提交计算」功能。
- **前置条件**：AiiDA profile 已创建（`verdi status` 正常）。

**操作步骤**

```bash
conda activate aiida
verdi code create core.code.installed \
    --label cp2k --computer localhost \
    --filepath-executable <cp2k.psmp 绝对路径> --default-calc-job-plugin cp2k
verdi code list          # 应看到 cp2k@local
```

**预期结果**：`verdi code list` 中列出 `cp2k@local`。

**风险提示与回退**：code 未注册时，提交计算会失败并提示找不到 code，数据不受影响。

---

## 3. 服务启停与状态

### 3.1 启动服务

- **适用场景**：日常启动、配置变更后启动。

**操作步骤**

```bash
cd /home/user/bin/Agent/catmat-platform
nohup python3 catmat.py serve --port 8000 --host 127.0.0.1 > /tmp/catmat_server.log 2>&1 &
sleep 3
curl http://127.0.0.1:8000/api/health
```

**启动参数**

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8000 | 监听端口 |
| `--host` | 127.0.0.1 | 默认仅本机回环；公网部署必须经 Nginx/frp，禁止直接暴露 8000 |
| `--reset` | 关闭 | 删除数据库重新初始化（**需二次确认**） |
| `--yes` | 关闭 | 与 `--reset` 配合，跳过交互确认（脚本用） |
| `--seed` | 关闭 | 导入 `data/` 下种子数据（默认空白初始状态） |

**预期结果**：`/api/health` 返回 `{"ok":true,"data":{"status":"up","version":"2.4.1",...}}`；`logs/server.log` 出现「服务启动，日志落盘就绪」与备份记录。

**风险提示与回退**：端口被占用会启动失败并退出，换端口即可。

---

### 3.2 停止服务

- **适用场景**：维护、迁移、排障。

**操作步骤**

```bash
sudo systemctl stop catmat.service
systemctl show catmat.service -p ActiveState -p SubState -p MainPID
ss -ltnp | grep ':8000'   # 应无输出
```

**预期结果**：进程退出，8000 端口释放。

**风险提示与回退**：停止 Web 服务**不影响 AiiDA daemon 与正在运行的计算作业**（二者是独立进程）。恢复只需重新执行 3.1。

---

### 3.3 重启服务（使后端改动生效）

- **适用场景**：修改了 `server.py`、`.env`、或安装了新的 Python 模块之后。
- **前置条件**：确认无用户正在执行长时间操作。

**操作步骤**

```bash
cd /home/user/bin/Agent/catmat-platform
sudo systemctl restart catmat.service
systemctl show catmat.service -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecStart
curl -s http://127.0.0.1:8000/api/health
```

**预期结果**：新 PID 产生，健康检查通过，新配置生效。

**风险提示与回退**：
- ⚠️ **改 `server.py` 必须重启进程**才生效；**改 `index.html` 只需用户刷新浏览器**（后端每次请求实时读取文件）。
- ⚠️ 重启 Web 服务**不会**中断 AiiDA 计算作业。
- 回退：用 git 还原改动后再次重启（见 [7.3 回滚](#73-回滚到历史版本)）。

---

### 3.4 查看服务运行状态

**操作步骤**

```bash
systemctl show catmat.service -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecStart
ss -ltnp | grep ':8000'
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/stats      # 各表记录数
```

**预期结果**：进程存在、端口监听、`health` 返回 `status: up`。

---

### 3.5 配置 systemd 开机自启（可选）

- **适用场景**：希望服务随服务器启动、异常退出后自动拉起。
- **前置条件**：具备 root 权限。

**操作步骤**

```bash
sudo tee /etc/systemd/system/catmat.service > /dev/null <<'EOF'
[Unit]
Description=CataMind Platform
After=network.target

[Service]
Type=simple
User=catamind
WorkingDirectory=/home/user/bin/Agent/catmat-platform
EnvironmentFile=/home/user/bin/Agent/catmat-platform/.env
ExecStart=/usr/bin/python3 catmat.py serve --port 8000 --host 127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now catmat
systemctl status catmat
```

**预期结果**：`systemctl status` 显示 `active (running)`。

> ⚠️ **关于自动重启（2026-09-10 实测更正）**：unit 用的是 `Restart=on-failure`，**只覆盖非正常退出**；对外部 `kill`（SIGTERM）实测**不会**自动拉起（服务会停在 `inactive`，需 `sudo systemctl start catmat` 手动恢复）。若确实需要「被 kill 后自动重启」的强守护，请把 unit 改为 `Restart=always`（并同步加 `StartLimitIntervalSec=0` 避免频繁重启被限流），改后执行 `sudo systemctl daemon-reload && sudo systemctl restart catmat`。

**风险提示与回退**：若服务反复启动失败，systemd 会不断重启。先 `systemctl stop catmat` 再排查；不需要时 `systemctl disable --now catmat` 并删除 unit 文件。

---

## 4. 日志查看与监控

### 4.1 查看服务日志

- **适用场景**：排障、审计、确认启动过程。

**操作步骤**

```bash
cd /home/user/bin/Agent/catmat-platform
tail -50 logs/server.log              # 最近 50 行
tail -f logs/server.log               # 实时跟踪
grep "\[EXC\]" logs/server.log        # 只看异常（含完整 traceback）
grep "Agent 高权限工具已执行" logs/server.log
```

**日志格式**：`2026-09-03 17:12:24 [INFO] 消息`；异常行以 `[EXC]` 开头并附完整 traceback。

**预期结果**：可看到启动、备份、同步、异常等记录。

**风险提示与回退**：只读操作，无风险。`logs/` 已被 `.gitignore` 排除，不进版本库。

---

### 4.2 日志轮转与清理

- **适用场景**：磁盘空间管理。
- **说明**：日志由 `RotatingFileHandler` 自动轮转，**单文件 5 MB，保留 3 份**（`server.log`、`server.log.1`…`.3`），最大约 20 MB，**无需手工清理**。

**操作步骤**（如需手工归档）

```bash
ls -lh logs/
mv logs/server.log.3 logs/server_$(date +%F).log.bak   # 归档最旧一份
```

**风险提示与回退**：删除 `server.log` 后当前进程仍写入已删除的 inode，需重启服务才会重建文件。

---

### 4.3 健康检查与资源监控

**操作步骤**

```bash
curl -s http://127.0.0.1:8000/api/health     # 存活 + 版本
curl -s http://127.0.0.1:8000/api/metrics    # CPU/内存/磁盘/GPU + 任务队列
curl -s http://127.0.0.1:8000/api/stats      # 各表记录数
curl -s http://127.0.0.1:8000/api/processes  # AiiDA 进程
```

**预期结果**：全部返回 `{"ok":true,...}`。`metrics` 含 `queue`（running/queued/done/failed/…）。

> **版本探测（已优化，整改 [中2]）**：`get_versions()` 的 14 个探测项原为**串行**起子进程（首调实测约 17 秒）。现改为线程池并行 + **启动时后台预热** + 缓存 TTL **300 秒**（`VER_TTL`）。实测：预热耗时约 1.6 秒，用户请求命中缓存后响应约 2 ms。
> 若需强制刷新（如刚升级 CP2K），重启服务即可（预热会自动重跑）；不建议在前端高频轮询该端点。

---

### 4.4 查询审计日志

- **适用场景**：追溯"谁在何时做了什么"（写操作、Agent 工具调用、删库）。

**操作步骤**

```bash
sqlite3 catmat.db "SELECT ts,actor,action,target,ok FROM audit ORDER BY id DESC LIMIT 20;"
```

审计覆盖的域（`AUDIT_ROOTS`）：`systems / literature / kb / tasks / ml / al / hts / workflow / cp2k-submit / postprocess / build / import / params / chart / agent` 的 POST / PUT / PATCH / DELETE 请求；以及 Agent 每次工具调用（`actor=llm_agent`）、`--reset` 删库（`action=db_reset`）。

**风险提示与回退**：查询为只读。审计写入失败**不会阻断业务**（设计原则：审计永不阻断业务）。

---

### 4.5 目录卫生与磁盘管理

- **适用场景**：磁盘空间管理；排查 `.tmp/` 只增不减。
- **说明**：服务**每次启动时**由后台线程执行一次清理（`cleanup_dirs()`）：
  - `.tmp/` 下修改时间超过 **7 天**（`TMP_RETAIN_DAYS`）的项被移除；
  - 根目录 `__pycache__/` 被移除（Python 会按需自动重建）；
  - **绝不触碰** `cp2k_runs/`（计算输入/输出，属用户资产）与 `backups/`（备份，数据安全底线）。

**操作步骤**

```bash
grep "目录卫生" logs/server.log          # 查看清理记录
du -sh .tmp cp2k_runs backups logs       # 查看各目录体积
```

**预期结果**：日志出现 `目录卫生：移除 N 项（.tmp 保留 7 天 + __pycache__）`。

**风险提示与回退**
- ⚠️ 清理依据是**文件修改时间**。若某个长期运行的任务把临时文件放在 `.tmp` 且超过 7 天未被访问，理论上可能被清理。当前平台临时文件均为短生命周期（提交时生成、任务结束即弃），实测无影响。
- 需要保留更久：修改 `server.py` 中的 `TMP_RETAIN_DAYS` 后重启服务。
- 回退：清理仅涉及临时产物，不含业务数据；若确因清理导致某任务临时文件缺失，重跑该任务即可。

---

### 4.6 运行自测（回归验证）

- **适用场景**：升级或改动后快速回归；判断"是不是平台本身坏了"。
- **前置条件**：无（不依赖外部计算程序）。

**操作步骤**

```bash
cd /home/user/bin/Agent/catmat-platform
python3 -m unittest discover -s tests -p "test_*.py" -v
```

**预期结果**：`Ran 102 tests ... OK`（约 7 秒）。

覆盖范围（`tests/test_core.py`）：

| 用例组 | 验证内容 |
|---|---|
| 参数白名单 | 未知参数被拦截（含报告中 `elements` 静默回退案例）、face / ads_site 取值校验、**element / alloy / dopant 元素符号校验**（非法符号不再穿透到 ASE 抛 KeyError） |
| Agent 工具契约 | `generate_structure` 入参（symbols/kind/hkl/site）→ 内核规范参数（element/face/ads_site）的映射；不支持取值（bulk / 多元素化学式 / 非法晶面·位点）显式报错 |
| Agent 工具管控 | 高权限工具默认不可见；服务端开关未开时即便前端传 True 也隐藏 |
| 结构化错误码 | HTTP → code 映射；`_err` 输出同时含 `error` 与 `code` |
| 参数库 | `CUTOFF` 可检索；规模 > 6 万；必需数据表齐全 |
| 审计与备份 | 审计写入成功；**异常时返回 False 而不抛错**；备份文件生成 |
| 记忆同步 | 二次同步幂等（0 更新）；记忆索引限长；大 body 摘要截断 |
| 缓存 TTL | 版本缓存 300s、进程快照缓存 2s |

**风险提示与回退**
- 测试会短暂写入 `audit` 表与 `backups/`，但**均自我清理**（用例内含删除逻辑），不污染业务数据。
- 测试**不覆盖**需要 CP2K / Multiwfn / AiiDA 真实调用的链路，这部分由 `tests/TESTING.md` 人工手册覆盖。
- 若失败：按失败用例名定位改动点；`git stash` 回退后复跑以确认是否由本次改动引入。

---

## 5. 配置修改

### 5.1 修改大模型配置与密钥

- **适用场景**：更换大模型服务商 / 模型 / 密钥 / 推理参数。
- **前置条件**：东翁（或获「账号管理/任务运行」授权的掌书）登录；界面入口为「平台设置 → 大模型」。

**操作步骤**

界面方式（**推荐，唯一密钥入口**）：登录后进入「平台设置 → ① 大模型 → 配置 API / 模型 / 推理强度」→ 填写 `base_url`、`model`、`api_key` 等 → 保存；填写新 `api_key` 即完成**密钥轮换**，点「清除已保存 API Key」即清除。保存即时生效（服务端动态读取，无需重启）。

文件方式（仅运维兜底）：直接编辑 `llm_config.json`（**该文件现为唯一配置源，含 api_key**），随后重启服务：

```bash
nano llm_config.json        # 含 base_url / model / api_key / temperature / reasoning_effort / vendor / max_tokens / timeout / stream
chmod 600 llm_config.json   # 收紧权限（保持 0600）
systemctl restart catmat    # 重启后生效
```

> 说明：不再支持 `LLM_*` 环境变量注入（`LLM_BASE_URL` / `LLM_API_KEY` 等已从 `.env` 移除）。外部 API 密钥只存于平台设置文件 `llm_config.json`；`.env` 仅保留平台鉴权类启动级凭据（`CATMAT_*`）。

**预期结果**：`GET /api/llm/config` 返回已配置的模型信息与掩码化密钥状态（`api_key_masked`/`has_api_key`）；对话与 AI 工具可用。

**风险提示与回退**：
- ⚠️ `llm_config.json` 含明文密钥，已被 `.gitignore` 排除，切勿提交到版本库；权限必须保持 `600`，仅运行账号可读写。
- 配置错误时对话返回 `502`（未配置大模型 / 调用失败）并给出中文原因，不影响其他功能。
- 密钥泄露时应立即在厂商控制台吊销，并在本界面轮换新 Key；不要在聊天/文档/版本库中粘贴完整 Key。

---

### 5.2 修改外部路径与运行参数

见 [2.3 配置外部依赖路径](#23-配置外部依赖路径环境变量)。全部 13 个 `CATMAT_*` 变量见[附录 B](#附录-b-环境变量速查)。

**生效方式**：修改 `.env` 后**必须重启服务**。

---

### 5.3 开启休go的高权限工具

- **适用场景**：需要让 AI 助手提交真实计算、执行 ASE 代码并入库、改写体系结构。
- **前置条件**：理解风险；默认**关闭**。

**操作步骤**

```bash
export CATMAT_AGENT_HIGH_PRIV=1      # 或写入 .env
# 重启服务
```

然后在前端 AI 助手面板勾选「⚠ 高权限工具」（勾选时需二次确认）。

**双重条件**：服务端环境变量 + 用户前端勾选，**两者同时满足**才会把 `submit_calc` / `run_ase_custom_code` / `normalize_structure` / `analyze_adsorption_sites` 暴露给模型。

**预期结果**：`GET /api/agent/...` 无关；对话中模型可看到并使用这 4 个工具，且每次执行都会写入 `logs/server.log`（WARNING 级）与 `audit` 表。

**风险提示与回退**：开启后若发生 prompt 注入，可能被诱导批量提交计算/写库。**用完立即取消勾选**并移除环境变量重启服务。所有执行均有审计留痕可追溯。

---

### 5.4 修改前端（页面/交互）

- **适用场景**：调整界面文案、布局、交互。

**操作步骤**：直接编辑 `index.html`，**保存后用户刷新浏览器即生效**，无需重启服务。

**风险提示与回退**：前端无构建步骤，语法错误会直接导致页面不可用。修改后建议校验：

```bash
python3 - <<'PY'
import re
h=open("index.html",encoding="utf-8").read()
open("/tmp/app.js","w",encoding="utf-8").write(re.findall(r'<script>(.*?)</script>',h,re.S)[-1])
PY
node --check /tmp/app.js
```

回退：`git checkout -- index.html`。

---

### 5.5 登录与安全（主通道：本地账号密码 · 三角色：东翁/掌书/清客）

- **主通道**：东翁（超级管理员）、掌书（受权管理员）、清客（普通用户）均通过 `POST /api/auth/local/login` 登录。SQLite `catmat.db/auth_users` 是事务权威源；启动与每次账号变更后，系统按角色原子同步到 `data/accounts/admins.json`（东翁+掌书）和 `data/accounts/users.json`（清客）。登录与账号列表会加载并核验这两个文件，发现重复、遗漏、角色错位或篡改时以 SQLite 自动重建。密码字段保持原 `scrypt + 独立随机盐` 摘要格式，绝不保存或输出明文。完整命名与释义见 `docs/role-naming-rules.md`。
- **角色标识**：`owner`（东翁）/ `steward`（掌书）/ `guest`（清客）；旧值 `admin`/`user` 在启动迁移时自动归一（本地 admin 仅保留 id 最小者为东翁，其余转掌书）。
- **东翁唯一**：全平台本地东翁恒为一人；东翁不可被停用/降级/删除；掌书不得变更东翁；传印（身份转移）为后续流程。
- **掌书能力包**：掌书默认只读；东翁可在账号管理中勾选能力包（`capabilities`），包括 `maintain_data`（数据维护）、`delete_data`（删除）、`manage_accounts`（账号管理，不含东翁）、`run_tasks`（任务/工作流）、`control_tasks`（终止任务）、`run_pipelines`（ML/AL/HTS/后处理）、`global_sync`（全局导入同步）、`view_audit`（审计）。系统配置（llm/config、agent/memory/sync）仅东翁可执行。
- **清客与全员规则**：清客可浏览全平台、扩充文献库（DOI 导入/核验/联网检索）、发起 AI 解读归档、使用智能助手问答（不写库）、提交 CM- 计算队列任务；只可删除/修改/下载**自己提交任务**产生的文件（后端按任务归属 `owner_id` 校验）。所有用户的 AI 解读归档都会在文献与知识库条目上带**归档用户名标签**（如“由 admin 归档”）。
- **会话**：access token 有效期默认 30 分钟，refresh token 默认 7 天且每次刷新轮换。access token 含 `sid`，每次业务请求检查数据库会话状态；停用、改角色或重置密码后旧会话立即失效。
- **应急通道**：`CATMAT_ADMIN_TOKEN` 仅用于救援和首次东翁初始化，不在日常登录界面展示。
- **微信确认器**：代码兼容保留，但 `CATMAT_WECHAT_CHALLENGE_ENABLED=0` 时所有微信会话/挑战入口关闭，不参与日常登录。

**三角色权限矩阵**：

| 能力 | 东翁 | 掌书 | 清客 |
|---|---|:--:|:--:|
| 浏览/检索/预览、智能助手问答（不写库） | ✅ | ✅ | ✅ |
| 扩充文献库（DOI/核验/联网检索） | ✅ | ✅ | ✅ |
| AI 解读归档文献（自动带用户名标签） | ✅ | ✅ | ✅ |
| 提交计算任务（CM- 队列） | ✅ | ✅ | ✅ |
| 删改/下载自己任务的文件 | ✅ | ✅ | ✅ |
| 数据新增与维护（体系/知识库/文献） | ✅ | 🔶 `maintain_data` | ❌ |
| 删除数据/任务 | ✅ | 🔶 `delete_data` | 仅自己的任务 |
| 账号管理（不含东翁） | ✅ | 🔶 `manage_accounts` | ❌ |
| 批量流水线（ML/AL/HTS/后处理/导入同步） | ✅ | 🔶 `run_pipelines`/`global_sync` | ❌ |
| 暂停/终止/收集任务 | ✅ | 🔶 `control_tasks` | 仅自己的任务 |
| 系统配置（llm/config、全局记忆同步） | ✅ | ❌ | ❌ |
| 册封掌书/变更角色/传印 | ✅ | ❌ | ❌ |

（🔶 = 需东翁在账号管理中逐项授予；不授即默认只读。）

**生产配置**：

```bash
CATMAT_AUTH_ENABLED=1
CATMAT_LOCAL_AUTH_ENABLED=1
CATMAT_WECHAT_CHALLENGE_ENABLED=0
CATMAT_LOCAL_LOGIN_LIMIT=10
# 可选；默认 data/accounts。目录权限 0700，角色文件权限 0600
CATMAT_ACCOUNT_DATA_DIR=/home/user/bin/Agent/catmat-platform/data/accounts
CATMAT_JWT_SECRET=<独立强随机签名密钥>
CATMAT_ADMIN_TOKEN=<仅救援使用的强随机令牌>
# CM- 队列调度（可选；默认并发 2、总核数不限、心跳 5s）
CATMAT_QUEUE_MAX_RUNNING=2
CATMAT_QUEUE_MAX_TOTAL_NPROC=0
CATMAT_QUEUE_POLL_SECONDS=5
```

> `CATMAT_*` 必须由 systemd `EnvironmentFile=` 或启动前 `export` 注入。`EnvironmentFile` 不要使用行内注释。

**日常账号管理**：东翁（或获「账号管理」能力的掌书）登录后进入「平台设置 → 管理本地账号」，可创建账号（默认清客）、设为掌书并勾选能力、停用/启用、重置密码。系统禁止停用或降级最后一个东翁；掌书不能变更东翁。角色变更、停用和密码重置会撤销目标账号全部会话，并同步刷新两个角色文件。

**角色文件完整性验证（不打印密码摘要）**：

```bash
python3 - <<'PY'
import json
from pathlib import Path
root=Path('data/accounts')
a=json.loads((root/'admins.json').read_text())
u=json.loads((root/'users.json').read_text())
assert all(x['role'] in ('owner','steward') for x in a)
assert all(x['role']=='guest' for x in u)
assert not ({x['id'] for x in a} & {x['id'] for x in u})
print({'东翁/掌书':len(a),'清客':len(u),'disjoint':True})
PY
stat -c '%a %n' data/accounts data/accounts/*.json
```

预期目录权限 `700`、两个文件权限 `600`。不要用 `cat`、编辑器或日志输出这些文件；其中包含不可逆的 scrypt 密码摘要，仍属于敏感凭据材料。

**状态验证**：

```bash
sudo systemctl restart catmat
curl -s http://127.0.0.1:8000/api/auth/status
```

预期：`enabled:true`、`mode:"local"`、`version:4`、`channels.local:true`、`channels.challenge:false`、`bootstrap_required:false`。

**风险提示与回退**
- 不要直接编辑数据库中的 `password_hash`；使用平台账号管理界面重置密码。
- `CATMAT_ADMIN_TOKEN` 与 `CATMAT_JWT_SECRET` 泄露后应立即轮换并重启服务。
- 若账号体系故障，不要把 `CATMAT_AUTH_ENABLED` 改成 0 暴露公网平台；应先从备份恢复或使用应急令牌诊断。
- 公网仍必须使用 HTTPS；账号密码绝不能经明文 HTTP 传输。

### 5.6 平台内置 CM- 计算队列（无外部调度器时的 FIFO 排队）

服务器没有 slurm 等外部队列时，独立 CP2K 计算任务（`POST /api/cp2k-submit` 或 `POST /api/queue`）先进入平台内置队列：

- **编号**：`CM-YYYYMMDD-NNNN`（当日自增），全平台唯一；界面与任务列表均以该编号呈现。
- **排队**：按提交时间（`created_at`）FIFO；后台调度线程每 `CATMAT_QUEUE_POLL_SECONDS`（默认 5s）检查，当运行数 `< CATMAT_QUEUE_MAX_RUNNING`（默认 2）且总占用核数未超 `CATMAT_QUEUE_MAX_TOTAL_NPROC`（默认 0=不限）时，按序转真实本地 mpirun 提交。
- **撤销**：排队期间 `POST /api/queue/{id}/cancel`（或 `DELETE /api/queue/{id}`）即移出队列不再执行；运行中撤销会终止进程树。撤销后队列项置 `canceled`，相应 tasks 记录置 `cancelled`。
- **文件归属**：每个任务使用专属目录 `cp2k_runs/<CM-编号>/`；清客只能查看/下载/删除**自己**队列任务的目录（服务端按 `owner_id` 校验）。列表/文件下载接口：`GET /api/queue`、`GET /api/queue/{id}/files`、`GET /api/queue/{id}/download?f=<相对路径>`。
- **运维观察**：
  ```bash
  sqlite3 catmat.db "SELECT cm_code,name,status,nproc,created_at FROM queue ORDER BY id DESC LIMIT 20;"
  ```
- 危险：`delete_data`/东翁可删除任意任务目录；清客删除他人队列任务返回 `403 OWNERSHIP_DENIED`。任务目录删除仅限专属 `<CM-编号>` 目录，共享目录（如 `hts_collect`）不会被物理删除。

---

### 5.7 备案信息（ICP 备案号）与合规悬挂

**适用场景**：网站完成 ICP 备案后，须在首页底部悬挂备案号。依据《非经营性互联网信息服务备案管理办法》（原信息产业部令第 33 号）第十三条，未悬挂可被省通信管理局责令改正并处 **5000~10000 元罚款**。

**落地位置**：`index.html` 中 `<main id="main">` 内、所有模块 `<section>` 之后的最后一个 `</main>` 之前：

```html
<footer class="site-footer">
  <a class="beian" href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">ICP备案号请由部署者配置</a>
</footer>
```

样式定义在 `index.html` `<style>` 末尾的「14. 备案信息页脚」段（`#main` 设为纵向 flex，页脚 `margin-top:auto` 贴底，水平居中，`--faint` 小字）。

**关键要求（最易错项已在表中标出）**：

| 要求 | 说明 |
|---|---|
| **必须保留 `-1` 序号** | 备案号分两级：**主体备案号** `ICP备案号请由部署者配置` 与**网站备案号** `ICP备案号请由部署者配置`。**只有广东省须悬挂主体备案号（不带序号）；其他省份（含浙江）必须悬挂「网站备案号」并保留 `-1/-2` 序号**，只写主体备案号属不合规。 |
| **必须是可点击超链接** | 指向 `https://beian.miit.gov.cn/`。只有纯文字、链接打不开、地址错误，均视为不合规。 |
| **位置** | 主页底部中央（本平台为主内容区水平居中；因左侧有导航栏，居中基准为内容区而非整页）。 |
| **编号一致性** | 页面所示编号须与工信部备案系统中的备案编号完全一致。 |
| **多端一致** | 桌面端与移动端均须展示；本页脚为单一 DOM 节点，响应式自动适配。 |

**验证方法**：

```bash
curl -s http://127.0.0.1:8000/ | grep -o '浙ICP备[^<]*'   # 应输出含 -1 的完整网站备案号
```
浏览器打开首页滚至底部 → 点击备案号应正确跳转工信部备案系统。

**后续动作（公安联网备案，易漏）**：ICP 备案不等同于完成合规。须在网站开通之日起 **30 日内**到 <https://beian.mps.gov.cn> 提交公安联网备案；审核通过后，同样需在网页底部悬挂**公安备案号与备案图标**（`index.html` 页脚处已预留注释位置，届时追加即可）。

---

## 6. 数据备份与恢复

### 6.1 自动备份机制（已内置）

- **说明**：服务**每次启动时**由后台线程检查，若 `backups/catmat_<YYYYMMDD>.db` 不存在则自动生成一份；**同一天重启不会重复备份**。
- 实现：`VACUUM INTO`（一致性快照）；若 SQLite < 3.27 自动降级为 `PRAGMA wal_checkpoint(TRUNCATE)` + 文件拷贝。
- 备份位置：`backups/`，单份约 20 MB，**已被 `.gitignore` 排除**。

**验证**

```bash
ls -lh backups/
grep "数据库已备份" logs/server.log | tail -3
```

---

### 6.2 手动备份

- **适用场景**：重大操作前（升级、迁移、批量导入）额外留一份。

**操作步骤**

```bash
cd /home/user/bin/Agent/catmat-platform
sqlite3 catmat.db "VACUUM INTO 'backups/catmat_manual_$(date +%F_%H%M).db';"
ls -lh backups/
```

**预期结果**：生成新的备份文件，大小约 20 MB。

**风险提示与回退**：只读源库，无风险。建议连同 `catmat.db` 一起异地保存关键备份。

---

### 6.3 从备份恢复

- **适用场景**：数据损坏、误删、误改。
- **前置条件**：存在可用备份文件。

**操作步骤**

```bash
cd /home/user/bin/Agent/catmat-platform
# 1) 停止服务
sudo systemctl stop catmat.service
# 2) 保护现场
mv catmat.db catmat.db.broken_$(date +%F_%H%M)
mv catmat.db-wal catmat.db-wal.broken 2>/dev/null
mv catmat.db-shm catmat.db-shm.broken 2>/dev/null
# 3) 用备份替换
cp backups/catmat_20260903.db catmat.db
# 4) 启动并验证
sudo systemctl start catmat.service
systemctl show catmat.service -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecStart
curl -s http://127.0.0.1:8000/api/stats
```

**预期结果**：`/api/stats` 返回的记录数与备份时一致（`cp2k_keywords` 应为 67511）。

**风险提示与回退**：
- ⚠️ **恢复会覆盖当前数据库**，务必先按第 2 步保留现场文件。
- 回退：`mv catmat.db.broken_* catmat.db` 还原。

---

### 6.4 重置数据库（--reset）

- **适用场景**：需要清空全部数据重新初始化。
- **前置条件**：已备份（若需保留数据）。

**操作步骤**

```bash
python3 catmat.py serve --reset            # 交互式：需输入 yes 确认
# 或脚本方式（跳过交互）：
python3 catmat.py serve --reset --yes
```

**内置保护**：执行前会**自动备份到 `backups/catmat_YYYYMMDD_before_reset.db`**；若备份失败则**中止删除**；无交互终端（如 `nohup` / `< /dev/null`）时按"取消"处理。

**预期结果**：提示删前备份路径 → 删除 `catmat.db` → 重新初始化空库。

**风险提示与回退**：
- ⚠️ **删除不可逆**。好在有删前自动备份，可从 `backups/catmat_*_before_reset.db` 恢复（方法同 6.3）。
- 该操作会写入 `audit` 表（`action=db_reset`，在删除前写入）。

---

## 7. 版本更新与回滚

> 平台已纳入 git 版本控制（仓库已 `git init`，`.gitignore` 已排除 `.env` / `llm_config.json` / `catmat.db` / `backups/` / `logs/` / `.tmp/` / `__pycache__/` / `cp2k_runs/` / `*.egg-info`）。

### 7.1 提交变更

- **适用场景**：每次有意义的改动后留档（约定：一次大改一个 commit）。

**操作步骤**

```bash
cd /home/user/bin/Agent/catmat-platform
git status                                   # 确认无敏感文件被跟踪
git add -A
git commit -m "feat: 简要描述本次改动"
git log --oneline | head -5
```

**预期结果**：`git status` 干净，提交出现在 `git log`。

**风险提示与回退**：提交前务必确认 `git status` 中**不出现** `.env`、`llm_config.json`、`catmat.db`。若误提交密钥，需改写历史并轮换密钥。

---

### 7.2 更新平台代码

- **适用场景**：拉取新版本 / 应用补丁。

**操作步骤**

```bash
git pull                       # 或从补丁应用
# 若涉及工作流模块（catmat_workflow_chain.py）：
conda activate aiida && pip install -e ./catmat_tools --no-deps && verdi plugin list aiida.workflows | grep catmat
# 确认无运行中的作业后，再执行：verdi daemon restart
# 重启 Web 服务：
sudo systemctl restart catmat.service
systemctl show catmat.service -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecStart
curl -s http://127.0.0.1:8000/api/health
```

**风险提示与回退**：更新前先 `git log` 确认当前版本（便于回滚）；数据库结构由 `init_db()` 自动兼容（缺表建表、缺列补列），**无需手工迁移**。

---

### 7.3 回滚到历史版本

- **适用场景**：更新后出现异常。

**操作步骤**

```bash
git log --oneline | head -10          # 找到目标 commit
git checkout <commit> -- server.py index.html    # 只回滚指定文件
# 或整体回滚：git reset --hard <commit>（会丢弃之后的所有改动）
# 重启服务
```

**预期结果**：恢复为历史版本文件；重启后行为与当时一致。

**风险提示与回退**：
- ⚠️ `git reset --hard` **不可逆**，先 `git stash` 或确认无未提交改动。
- ⚠️ **代码回滚不会回滚数据库**。若新版本改过数据结构，需配合 [6.3 从备份恢复](#63-从备份恢复)。

---

### 7.4 热修复（不停机程度说明）

| 改动对象 | 生效方式 | 是否中断服务 |
|---|---|---|
| `index.html`（前端） | 用户刷新浏览器 | 否 |
| `server.py`（后端） | **重启进程**（约 3–5 秒） | 是（秒级），**不影响 AiiDA 作业** |
| `.env` / 环境变量 | 重启进程 | 同上 |
| `catmat_tools/catmat_workflow_chain.py` | `pip install -e ./catmat_tools --no-deps` + **`verdi daemon restart`** | 是，**会中断 daemon 正在跑的计算作业** |

---

## 8. 常见故障排查

### 8.1 服务无法启动（端口被占用）

**现象**：启动后立即退出，或 `/tmp/catmat_server.log` 报 `Address already in use`。

**排查与处理**

```bash
ss -ltnp | grep 8000                 # 查看占用进程
# 处理：结束占用进程，或换端口启动
python3 catmat.py serve --port 8001 --host 127.0.0.1
```

**回退**：恢复原端口需先释放。

---

### 8.2 `/api/versions` 响应很慢

> **状态：已修复（整改 [中2]）。** 保留本节用于复现时排查。

**历史原因**：14 个探测项串行起子进程（`_ml_gpu_info` 上限 90 s、`_probe_multiwfn` 上限 30 s），首调实测约 17 秒，缓存仅 60 秒。

**已实施的修复**：线程池并行探测（最多 8 线程）+ 启动后台预热（`warm_versions()`）+ 缓存 TTL 300 秒。实测预热 1.6 秒、命中缓存 2 ms。

**若仍慢，按此排查**

```bash
grep "版本探测预热" logs/server.log        # 看预热耗时是否正常
curl -s -o /dev/null -w "%{time_total}s\n" http://127.0.0.1:8000/api/versions
```

- 预热耗时异常 → 多半是某个外部程序（CP2K / Multiwfn / GPU 探测）本身响应慢，检查对应路径与 `nvidia-smi` 是否正常；线程池整体受限于**最慢的那一项**。
- 缓存未命中（每次都慢）→ 确认没有进程反复重启；或检查 `VER_TTL` 是否被改小。

---

### 8.3 提交计算失败（MPI / CP2K 环境）

**现象**：提交后立即失败，或任务状态变为 failed。

**排查**

```bash
grep -A15 "\[EXC\]" logs/server.log | tail -30
conda activate aiida && verdi process list -l 5
verdi daemon status
```

**常见原因**
1. CP2K 的 MPI 环境与 `cp2k_env` 不一致。平台通过 `source <CP2K_ROOT>/install/cp2k_env` 获取环境，并由 `resolve_mpirun()` 从 PATH 定位 `mpirun`，再按实现适配参数（OpenMPI 用 `-x K=V`，MPICH/Intel MPI 用 `-env K V`）。若 `CATMAT_CP2K_ROOT` 指向错误，会导致定位失败。
2. `.bashrc` 对非交互 shell 提前 return——平台**不依赖** `source ~/.bashrc`，若自行改动启动方式引入该依赖会失败。

**回退**：修正 `CATMAT_CP2K_ROOT` 后重启服务；已失败的任务需重新提交。

---

### 8.4 大模型对话失败

**现象**：对话返回错误，或前端提示 `502`。

**排查**

```bash
curl -s http://127.0.0.1:8000/api/llm/config
grep -i "llm\|大模型" logs/server.log | tail -10
```

**常见原因**：平台设置（`/api/llm/config`）未保存 `api_key`（`has_api_key=false`）；`base_url` 不通或厂商路径识别错误；模型名错误；余额/额度/限流问题。检查界面「平台设置 → 大模型」中的识别结果提示，或按上文 `curl` 核对返回的 `api_key_masked` 是否为空。

**回退**：在「平台设置 → 大模型」中修正并保存即可恢复（无需重启）；对话失败**不影响**其他模块。

---

### 8.5 页面异常 / 弹窗卡住

**现象**：某弹窗一直显示加载中。

**排查**：浏览器 F12 控制台；服务端 `logs/server.log` 的 `[EXC]` 行。

**已知案例**：曾出现「预览 AiiDA 输入文件」弹窗卡在"正在生成输入文件"——根因是**运行中的服务进程加载的是旧代码**（返回单阶段字段），而前端期望两阶段字段，取值失败抛异常后无兜底。修复方式：重启服务 + 前端加异常兜底。

**处理顺序**：① 刷新浏览器 → ② 用 `systemctl show catmat.service -p ExecMainStartTimestamp -p ExecStart` 核对进程启动时间与实际入口 → ③ 重启服务。

---

### 8.6 数据库锁定 / 写入缓慢

**现象**：偶发 `database is locked`。

**说明**：平台采用 WAL 模式 + 每请求新建连接（`check_same_thread=False`），单机并发足够。

**处理**：检查是否有长事务或外部程序占用 `catmat.db`；确认磁盘空间充足。必要时按 [6.2](#62-手动备份) 备份后重启服务。

---

### 8.7 工作流在 daemon 侧报模块导入错误

**现象**：`ImportError: module 'catmat_workflow_chain' could not be loaded`。

**处理**：执行 [2.4](#24-安装-aiida-工作流-entry-point) 的 `pip install -e ./catmat_tools --no-deps`，用 `verdi plugin list aiida.workflows` 验证，再于维护窗口执行 **`verdi daemon restart`**。

**风险**：daemon 重启会中断运行中的作业，择机执行。

---

## 9. 服务器迁移与重新部署

- **适用场景**：更换服务器、灾后重建。
- **前置条件**：目标机为 Linux + Python 3.9+；已安装 conda、CP2K、Multiwfn；网络可达。

**操作步骤**

```bash
# ① 源机：备份并停止
cd /home/user/bin/Agent/catmat-platform
sqlite3 catmat.db "VACUUM INTO 'backups/catmat_migrate.db';"
sudo systemctl stop catmat.service

# ② 拷贝整目录（含数据库、种子数据、文档）
rsync -av /home/user/bin/Agent/catmat-platform/ newserver:/opt/catmat/

# ③ 目标机：重建计算环境
conda env create -f aiida_env.yml        # 或直接迁移 env 目录
conda env create -f dpmd_env.yml
# 拷贝 CP2K / Multiwfn 到目标路径（无 GPU 时 dpmd 装 CPU 版 torch，自动降级）

# ④ 目标机：覆盖路径环境变量（见 2.3）
cd /opt/catmat && cat >> .env <<'EOF'
CATMAT_AII_PY=/opt/conda/envs/aiida/bin/python
CATMAT_VERDI=/opt/conda/envs/aiida/bin/verdi
CATMAT_ML_PY=/opt/conda/envs/dpmd/bin/python
CATMAT_MULTIWFN_BIN=/opt/Multiwfn/Multiwfn
CATMAT_CP2K_ROOT=/opt/cp2k-2026.2
CATMAT_DEFAULT_NPROC=128
EOF

# ⑤ 目标机：注册 code 与工作流
conda activate aiida
verdi code create core.code.installed --label cp2k --computer localhost \
    --filepath-executable <cp2k.psmp> --default-calc-job-plugin cp2k
cd /opt/catmat
pip install -e ./catmat_tools --no-deps
verdi plugin list aiida.workflows | grep catmat
# 确认无运行中的作业后，再执行：verdi daemon restart

# ⑥ 启动并验证（先按 3.5 节安装 catmat.service）
cd /opt/catmat
sudo systemctl enable --now catmat.service
systemctl show catmat.service -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecStart
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/versions     # 确认 CP2K/Multiwfn/GPU 探测正常
curl -s http://127.0.0.1:8000/api/stats        # 核对记录数与源机一致
```

**预期结果**：`health` 返回 up；`versions` 探测到各软件；`stats` 记录数与迁移前一致（`cp2k_keywords` 67511）。

**风险提示与回退**：
- ⚠️ 迁移工作量集中在**外部分子模拟软件**（CP2K / Multiwfn / AiiDA）的安装，与平台代码无关。
- ⚠️ 迁移前务必完整备份；保留源机数据直到新机验证通过。
- **公网部署安全基线**：本地账号密码鉴权已启用，但仍必须保持 HTTPS、Nginx/frp 最小暴露与登录限流；不得将 8000 端口直接暴露公网。

---

## 附录 A 错误码对照表

平台所有错误统一返回 JSON：`{"ok": false, "error": "...", "code": "..."}`。前端据此渲染错误态。

> **结构化错误码（整改 [低2]）**：`code` 字段为稳定枚举值，便于按类型处置（"用户改改就好" vs "需要报修"）。默认按 HTTP 状态映射，部分关键失败点会指定更精确的码：
>
> | code | 含义 | 出现场景 |
> |---|---|---|
> | `INPUT_ERROR` | 参数/输入错误 | 默认对应 400；未知参数（如结构生成的 `elements`）、取值非法 |
> | `NOT_FOUND` | 资源不存在 | 默认对应 404；体系/任务/批次/手册不存在、未知 API |
> | `METHOD_NOT_ALLOWED` | 方法不支持 | 默认对应 405 |
> | `CONFLICT` | 状态冲突 | 默认对应 409；任务已终态 |
> | `DEPENDENCY` | 外部依赖不可用 | Multiwfn 生成失败、AiiDA 进程查询失败（环境/daemon 问题） |
> | `UPSTREAM` | 上游服务错误 | 默认对应 502；大模型调用失败 |
> | `TIMEOUT` | 执行超时 | 默认对应 504 |
> | `INVALID_CREDENTIALS` | 用户名或密码不正确 | 本地账号登录失败；不存在账号与错误密码返回相同错误 |
> | `ACCOUNT_DISABLED` | 账号已停用 | 登录或业务访问时实时检查 |
> | `PERMISSION_DENIED` | 当前身份权限不足 | 清客或未获授权的掌书尝试删除、管理账号、批量训练、终止任务或高风险操作；清客改动他人任务文件时返回 `OWNERSHIP_DENIED` |
> | `SESSION_REVOKED` | 会话已撤销 | 密码重置、停用、身份变化或切换鉴权版本后 |
> | `OWNER_UNIQUE` | 东翁唯一保护 | 禁止设第二位东翁；禁止停用/降级唯一东翁（注册/创建东翁重试时返回 `OWNER_EXISTS`） |
> | `INTERNAL` | 内部异常（需查日志） | 默认对应 500 |
>
> `code` 为**新增字段**，`error` 文本保持不变，既有前端无需改造。

| HTTP | 含义 | 典型消息（摘录） | 处理建议 |
|---|---|---|---|
| **400** | 参数错误 / 业务校验不通过 | `缺少必填字段 name`、`缺少必填字段 title`、`缺少必填字段 topic`、`缺少 doi`、`缺少代码`、`缺少输入文件内容（inp）`、`缺少结构（structure）或 ASE 生成失败: …`、`非法 PK: %s`、`非法 uuid`、`非法文件名`、`不支持的 SCF 方法: %s（可选 diag/ot）`、`代码需定义 build_structures(count=n) 函数`、`单次分析结构数 ≤60（当前 %d）`、`该结构未识别到表面吸附位点`、`该文献无 DOI，无法核验`、`不能把目录移动到自身或其子目录下`、`批次仍在运行，请先终止（POST /api/hts/batch/%s/stop）` | 按提示修正参数后重试；属"用户改改就好"，非系统故障 |
| **404** | 资源不存在 / 端点未知 | `工作流不存在`、`任务不存在`、`批次不存在`、`记忆不存在：%s`、`静态资源不存在`、`路径不可读`、`目录不存在或不在允许目录内: %s`、`库中无该 DOI 的文献，请先通过 DOI 导入`、`未知 xxx 端点` | 核对 ID / 路径 / 端点拼写；DOI 类先导入 |
| **405** | 方法不支持 | `仅支持 GET`、`仅支持 POST`、`仅支持 GET/POST(kill)`、`方法不支持` | 改用正确的 HTTP 方法 |
| **409** | 状态冲突 | `任务已完成`、`任务已处于终态（%s），无需停止` | 无需处理，属状态已满足 |
| **500** | 服务器内部错误 | `服务器内部错误: %s`（外层兜底，未捕获异常均落此）、`后处理失败: `、`后处理执行失败: %s`、`后处理输出解析失败`、`图表生成失败: %s`、`列目录失败: %s`、`读取失败: %s`、`工作流提交失败: ` | **查 `logs/server.log` 的 `[EXC]` 行**（含完整 traceback + 请求方法与路径） |
| **502** | 上游/大模型错误 | `未配置大模型且无结构可生成 INP，请先配置模型或提供结构`、Agent 调用失败（`agent_llm_chat` 返回 err 时） | 检查大模型配置与网络；见 8.4 |

> 说明：错误码**暂未分层**（参数错误、依赖缺失、内部异常部分共用 500），结构化错误码属整改清单 P2-3，尚未实施。

---

## 附录 B 环境变量速查

| 变量 | 默认值 | 用途 |
|---|---|---|
| `CATMAT_DB_PATH` | `<项目根>/catmat.db` | SQLite 权威数据库路径；隔离测试或迁移时可覆盖 |
| `CATMAT_BACKUP_DIR` | `<项目根>/backups` | 数据库备份目录；文件自动收紧为 `0600` |
| `CATMAT_LOG_DIR` | `<项目根>/logs` | 服务日志目录；隔离测试时可覆盖 |
| `CATMAT_TMP_DIR` | `<项目根>/.tmp` | 平台临时运行目录；隔离测试时可覆盖 |
| `CATMAT_ACCOUNT_DATA_DIR` | `<项目根>/data/accounts` | 角色镜像目录（`admins.json`=东翁+掌书，`users.json`=清客） |
| `CATMAT_AII_PY` | `/home/user/anaconda3/envs/aiida/bin/python` | AiiDA 环境 Python（进程查询、scikit-learn 训练） |
| `CATMAT_VERDI` | `/home/user/anaconda3/envs/aiida/bin/verdi` | AiiDA 命令行 |
| `CATMAT_ML_PY` | `/home/user/anaconda3/envs/dpmd/bin/python` | ML 训练环境 |
| `CATMAT_MULTIWFN_BIN` | `/home/user/bin/Multiwfn/Multiwfn` | Multiwfn 二进制 |
| `CATMAT_CP2K_ROOT` | `/home/user/bin/cp2k-2026.2` | CP2K 安装根 |
| `CATMAT_CP2K_PSMP` | `<ROOT>/install/bin/cp2k.psmp` | CP2K 可执行文件 |
| `CATMAT_CP2K_LIB` | `<ROOT>/install/lib` | CP2K 库目录 |
| `CATMAT_CP2K_ENV` | `<ROOT>/install/cp2k_env` | CP2K 运行环境脚本 |
| `CATMAT_CP2K_DATA_DIR` | `<ROOT>/data` | 基组/赝势数据目录 |
| `CATMAT_AIIDA_FILES` | `/home/user/aiida_files` | AiiDA 计算文件仓库 |
| `CATMAT_CP2KVIEW_DIR` | `/home/user/bin/cp2kview` | CP2K View 源码目录 |
| `CATMAT_DEFAULT_NPROC` | `144` | 默认 MPI 进程数 |
| `CATMAT_AGENT_HIGH_PRIV` | 未设置 | 设为 `1` 允许前端启用休go高权限工具 |
| `CATMAT_SCRIPT` | — | 内部脚本路径覆盖 |
| `LLM_*`（`LLM_BASE_URL` 等） | — | **已弃用**：大模型配置与密钥统一由「平台设置 → 大模型」管理，存于 `llm_config.json`；不再读取任何 `LLM_*` 环境变量 |

---

## 附录 C 关键文件与目录

| 路径 | 说明 | 版本控制 |
|---|---|---|
| `server.py` | 后端主程序 | ✅ 已跟踪 |
| `index.html` | 前端单文件 SPA | ✅ 已跟踪 |
| `catmat.db` | 主数据库（15 张表） | ❌ 已排除 |
| `catmat.db-wal` / `-shm` | SQLite WAL 附属文件 | ❌ 已排除 |
| `.env` | systemd EnvironmentFile：平台鉴权类启动级凭据（`CATMAT_JWT_SECRET` / `CATMAT_ADMIN_TOKEN` / 微信 AppSecret 等，**不含 LLM 密钥**） | ❌ 已排除 |
| `llm_config.json` | 平台设置文件（大模型配置 + **api_key**，唯一配置源，权限 0600） | ❌ 已排除 |
| `logs/server.log` | 服务日志（5 MB × 3 轮转） | ❌ 已排除 |
| `backups/` | 数据库每日备份 | ❌ 已排除 |
| `.tmp/` | 临时工作目录 | ❌ 已排除 |
| `cp2k_runs/` | CP2K 计算输入/输出目录 | ❌ 已排除 |
| `__pycache__/`、`*.egg-info/` | Python 缓存与安装元数据 | ❌ 已排除 |
| `data/` | 种子数据（`--seed` 时导入） | ✅ 已跟踪 |
| `docs/` | 设计与评估文档 | ✅ 已跟踪 |
| `tests/` | 测试数据 + 人工手册 `TESTING.md` + 自测 `test_core.py`（unittest） | ✅ 已跟踪 |
| `catmat_tools/catmat_workflow_chain.py` | AiiDA 两个 WorkChain（方案 A/B） | ✅ 已跟踪 |
| `catmat_tools/cp2k_workchain_input.py` | 工作流输入参数唯一基线 | ✅ 已跟踪 |
| `catmat_tools/setup.py` | AiiDA entry point 与 `catmat` 命令注册 | ✅ 已跟踪 |

**数据库表（15 张）**：`systems`、`literature`、`knowledge`、`tasks`、`ml_samples`、`ml_tasks`、`workflows`、`workflow_nodes`、`hts_batches`、`hts_batch_items`、`cp2k_keywords`（67,511）、`cp2k_sections`（9,983）、`cp2k_param_refs`（7,176）、`audit`、`agent_memory`。

---

**相关文档**

- `README.md` —— 平台功能与能力总览
- `docs/优化整改报告_20260902.md` —— 架构与安全整改清单
- `docs/整改项确认清单_20260903.md` —— 整改项优先级与实施进度
- `docs/迁移与公网部署评估.md` —— 迁移步骤与公网安全加固要求
- `docs/平台评估_20260902.md` —— 开发完成度评估
