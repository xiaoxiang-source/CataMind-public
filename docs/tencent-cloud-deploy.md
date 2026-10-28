# 催化智构 CataMind · 腾讯云部署实施方案（任务二）

| 项目 | 内容 |
|---|---|
| **文档版本** | v1.0 |
| **适用平台版本** | v2.4.1 |
| **更新日期** | 2026-09-04 |
| **云厂商** | 腾讯云（CVM / 轻量应用服务器） |
| **目标** | 将平台部署至腾讯云并实现 HTTPS 公网访问 |

> 本方案为**分步骤清单**，每步标注：操作 / 前置条件 / 风险点 / 验证。
> 涉及命令基于 **Ubuntu 22.04 LTS**（腾讯云镜像），其他发行版需等比换算。

---

## 0. 部署拓扑与模式选择（先读，再动手）

```
                   公网访问者（浏览器）
                          │ HTTPS:443
                          ▼
              ┌───────────────────────────┐
              │  腾讯云服务器              │
              │  Nginx（反代 + TLS + BasicAuth 可选）│
              │  systemd: catmat.service   │
              │  python3 catmat.py serve :8000   │
              │  catmat.db / backups/      │
              └───────────────────────────┘
```

| 模式 | 适用 | 服务器要求 | 说明 |
|---|---|---|---|
| **A · 纯平台**（推荐首期） | 课题组数据共享 / 展示 / 远程查看计算进度 | 2C4G，无需 GPU | 部署 Web + 数据库 + 知识库；CP2K / AiiDA / Multiwfn 未安装时相关提交功能不可用（平台有完整空态与报错提示），**真实计算仍在内网集群进行** |
| **B · 全功能** | 云上直接跑计算/训练 | GPU 实例（≥1×RTX4090）+ 大内存 + 大磁盘 | 额外安装 conda `aiida`/`dpmd` + CP2K + Multiwfn（详见 `docs/迁移与公网部署评估.md` 第三节） |

> **当前安全基线（2026-09-04）**：平台本地账号密码鉴权已实现，生产需设置 `CATMAT_AUTH_ENABLED=1`、`CATMAT_LOCAL_AUTH_ENABLED=1` 和独立的 `CATMAT_JWT_SECRET`。服务仅监听 `127.0.0.1:8000`，公网仅经 Nginx/frp 同源反代。Nginx Basic Auth 可作为额外纵深防护，但不再代替平台自身账号与权限控制。

---

## 1. 服务器与环境准备

### 1.1 购买并初始化服务器

**前置条件**：腾讯云账号已完成实名认证；确定部署模式（A/B）。

**操作**
1. 购买 **轻量应用服务器或 CVM**：建议 2C4G 起步、系统盘 ≥50 GB（catmat.db 21 MB + 备份逐日增长，见 1.4）。
2. 选择镜像 **Ubuntu Server 22.04 LTS**。
3. 设置 SSH 密钥（比密码安全）。
4. 创建密钥对后登录：
   ```bash
   ssh -i <your-key.pem> ubuntu@<服务器公网IP>
   ```

**风险点**：实例一旦创建即开始计费；先买按量计费验证，稳定后再转包年。

### 1.2 基础环境更新与工具

**前置条件**：已 SSH 登录。

**操作**
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-dev sqlite3 nginx git curl \
                    ufw fail2ban
python3 -V    # 要求 ≥ 3.9（平台零第三方依赖，此即全部）
sqlite3 --version   # 要求 ≥ 3.27（备份 VACUUM INTO 用）
```

**验证**：两条版本命令输出符合要求。

**风险点**：`apt upgrade` 可能提示内核重启，SSH 会断（重连即可）；建议在维护窗口执行。

### 1.3 磁盘与数据目录规划

**前置条件**：/1.2。

**操作**
```bash
sudo mkdir -p /opt/catmat /opt/catmat/backups /opt/catmat/logs /opt/catmat/.tmp
sudo useradd -m -s /bin/bash catmat 2>/dev/null || true
```
**规划说明**（与平台约定一致）：
- `/opt/catmat/`：代码与数据根
- `backups/`：每日自动备份（**建议挂载独立数据盘或对象存储 COS 定期转储**）
- `logs/`：`server.log`（5 MB×3 自动轮转）
- `.tmp/`：临时目录（启动自动清理 7 天前产物）

**风险点**：备份只在本机时，**服务器故障 = 备份丢失**。正式使用务必开启对象存储/COS 周期转储（见步骤 7.3）。

---

## 2. 代码拉取与构建发布

### 2.1 拉取代码

**前置条件**：仓库 `github.com/xiaoxiang-source/CataMind` 可访问（公网拉取）。

**操作**
```bash
cd /opt/catmat
sudo -u catmat git clone https://github.com/xiaoxiang-source/CataMind.git .
sudo chown -R catmat:catmat /opt/catmat
```

**验证**：`git log --oneline | head -1` 有提交记录；`ls server.py index.html catmat.db`（若仓库含 .db 则一并就位；若为空库，首启自动建表）。

**风险点**：
- 若仓库未包含 `catmat.db`，首次启动自动 `init_db()` 建空库（含 6.7 万条参数库需另行导入：拷贝源 `catmat.db`，或执行 `python3 catmat.py build-param-db` / `python3 catmat.py build-param-refs`）。
- **`.env` / `llm_config.json` 不在仓库内**（.gitignore）——需在服务器手工创建（见 2.3）。

### 2.2 发布 = 更新代码 + 重启（无构建步骤）

**前置条件**：平台无编译/打包（零依赖、单文件前后端），"发布"即代码落盘 + 重启进程。

**操作（每次发版）**
```bash
cd /opt/catmat && sudo -u catmat git pull origin main
# 若改动了 AiiDA 工作流模块（catmat_workflow_chain.py，模式 B 才需要）：
#   conda activate aiida && pip install -e ./catmat_tools --no-deps && verdi daemon restart
sudo systemctl restart catmat        # 见步骤 4
```

**验证**：`curl -s http://127.0.0.1:8000/api/health` → `{"ok":true,...,"version":"2.4.1"}`。

**风险点**：
- 后端改动必须重启才生效；前端（`index.html`）每次请求实时读取，**无需重启**，用户刷新即见新版。
- 发布前建议先 `git stash` 检查有无未提交改动，避免 `git pull` 冲突。

### 2.3 环境配置（.env 与密钥）

**前置条件**：2.1。

**大模型密钥**：不在 `.env` 配置。部署并启动后，登录平台经「平台设置 → 大模型」填写 `base_url` / `model` / `api_key`，统一存于 `/opt/catmat/llm_config.json`（权限 0600），保存即时生效（无需重启）。

**平台鉴权等启动级凭据**（写入 `.env`，作为 systemd `EnvironmentFile`）：
```bash
sudo -u catmat tee /opt/catmat/.env > /dev/null <<'EOF'
# 平台鉴权等启动级凭据（不含任何大模型 LLM_* 配置；LLM 密钥走平台设置）
# 外部路径（模式 A 可全部留空使用默认；本机无则对应功能不可用）
CATMAT_MULTIWFN_BIN=
CATMAT_CP2K_ROOT=
CATMAT_DEFAULT_NPROC=8
# 鉴权：本地账号密码为主通道；管理员令牌仅用于首次初始化/救援
CATMAT_AUTH_ENABLED=1
CATMAT_LOCAL_AUTH_ENABLED=1
CATMAT_WECHAT_CHALLENGE_ENABLED=0
CATMAT_JWT_SECRET=REPLACE_WITH_A_LONG_RANDOM_SECRET
CATMAT_ADMIN_TOKEN=REPLACE_WITH_A_DIFFERENT_RANDOM_SECRET
EOF
sudo chown catmat:catmat /opt/catmat/.env
sudo chmod 600 /opt/catmat/.env
```

**风险点**：`.env` 含鉴权凭据，权限必须 `600`；**切勿**提交 git。`llm_config.json` 含大模型 API Key，同样保持 `600` 且不进版本库。

---

## 3.（可选）全功能模式的外部计算依赖

> 仅模式 B 需要。模式 A 直接跳到步骤 4。

**前置条件**：GPU 实例；已规划 ≥100 GB 磁盘。

**操作**（要点，完整步骤见 `docs/迁移与公网部署评估.md` §三）
1. 安装 NVIDIA 驱动 + CUDA（腾讯云 GPU 镜像一般自带）。
2. 安装 miniconda → `conda env create -f aiida_env.yml`（AiiDA 2.8）+ `dpmd_env.yml`。
3. 拷贝/编译 CP2K 2026.2、Multiwfn 到目标路径。
4. 配置 AiiDA：`verdi setup` + 注册 `cp2k@local` code。
5. 在 `.env` 中写 `CATMAT_AII_PY` / `CATMAT_VERDI` / `CATMAT_ML_PY` / `CATMAT_CP2K_ROOT` 等。

**风险点**：此步工作量最大（数小时级），且为全环境耦合；**强烈建议科研计算仍走内网集群**，云上只部署模式 A，避免 GPU 成本与运维负担。若确有云上计算需求，先跑通 `docs/迁移与公网部署评估.md` 再回接。

---

## 4. 进程守护（systemd）

**前置条件**：/1、/2 完成；`catmat` 用户存在。

**操作**
```bash
sudo tee /etc/systemd/system/catmat.service > /dev/null <<'EOF'
[Unit]
Description=CataMind Platform
After=network.target

[Service]
Type=simple
User=catamind
WorkingDirectory=/opt/catmat
# 关键：由 systemd 注入 CATMAT_* 与 LLM_* 环境变量
EnvironmentFile=/opt/catmat/.env
ExecStart=/usr/bin/python3 /opt/catmat/catmat.py serve --port 8000 --host 127.0.0.1
Restart=always
RestartSec=5
# 只允许从本机回环访问 8000，公网一律走 Nginx 443
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now catmat
```

**验证**
```bash
systemctl status catmat --no-pager -l      # active (running)
curl -s http://127.0.0.1:8000/api/health   # {"ok":true,...}
sudo -u catmat tail -5 /opt/catmat/logs/server.log   # 含"服务启动/备份/预热/同步"日志
```

**风险点**：
- `EnvironmentFile=/opt/catmat/.env` 中若含 `#` 注释需逐行合法；出错时服务启动失败，`journalctl -u catmat -n 30` 排查。
- 绑 `127.0.0.1`：**8000 端口绝不对公网开放**（只经 Nginx 反代）——安全底线。
- 重启 Web 服务不影响（云端若无）AiiDA daemon；二者独立。

---

## 5. Nginx 反向代理 + TLS + 可选 Basic Auth 纵深防护

### 5.1 安装证书（腾讯云 SSL 或 Let's Encrypt）

**前置条件**：域名已完成 **ICP 备案**（见步骤 6）并解析到本机（A 记录）。

**操作（certbot 为例）**
```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d catmat.example.com --redirect   # 自动改 Nginx 并启用 HTTPS
```
或使用**腾讯云 SSL 证书**：控制台申请 → 下载 Nginx 版本 → 上传到 `/etc/nginx/ssl/`。

### 5.2 编写站点配置

**前置条件**：证书就位（或先自签过渡）。

**操作**
```bash
sudo tee /etc/nginx/sites-available/catmat > /dev/null <<'EOF'
# 1) HTTP → HTTPS 强制跳转
server {
    listen 80;
    server_name catmat.example.com;
    return 301 https://$host$request_uri;
}

# 2) HTTPS 站点
server {
    listen 443 ssl http2;
    server_name catmat.example.com;

    ssl_certificate     /etc/letsencrypt/live/catmat.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/catmat.example.com/privkey.pem;

    # 安全响应头
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options SAMEORIGIN;

    client_max_body_size 50m;      # 结构文件/数据集上传

    location / {
        # 可选纵深防护：平台本地账号鉴权已启用时，可按运维策略保留或移除 Basic Auth
        auth_basic "CataMind Restricted";
        auth_basic_user_file /etc/nginx/.htpasswd_catmat;

        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;   # 长任务（ML 训练等）需较长读超时
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/catmat /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

**创建 Basic Auth 账号**（可选纵深防护）：
```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd_catmat catmat_admin   # 设置强密码
```

> 注：`auth_basic` 为**过渡安全层**。后端鉴权实现并启用（`CATMAT_AUTH_ENABLED=1`）后，删除两行 `auth_basic*` 并 reload nginx，切换为平台自身登录体系（此时建议 `client_max_body_size` 按需保留）。

**验证**：浏览器访问 `https://catmat.example.com` → 先弹 Basic Auth → 通过后进入平台；`http://` 自动跳 `https://`。

**风险点**：
- Basic Auth 凭据走 HTTPS 加密，安全；但它是"整体挡人"而非精细鉴权，仅作过渡。
- 证书续期：certbot 自带 systemd timer（`certbot renew --dry-run` 验证）。

---

## 6. 域名解析与备案

### 6.1 ICP 备案（必做）

**前置条件**：域名实名认证通过；服务器在**中国大陆**地域（腾讯云）。

**要点**
1. 腾讯云控制台 → **备案** 提交（主体/网站信息，需域名、服务器实例、负责人实名）。
2. 备案期间域名**不能直接提供网站服务**（可先用 IP + 非 80/443 端口联调）。
3. 取得备案号后才能在公网 80/443 提供访问；备案通过后需在页面底部展示备案号并链接至 `beian.miit.gov.cn`。

**风险点**：未备案使用大陆服务器 80/443 会被阻断/处罚。**急用可选购中国香港地域实例免备案**（延迟略增），或先按文档在内网/穿透方式联调。

### 6.2 DNS 解析（A 记录）

**操作**：腾讯云 DNSPod → 添加记录：
```
记录类型 A ｜ 主机记录 @（及 www）｜ 记录值 <服务器公网IP>
```
**验证**：`dig +short catmat.example.com` 返回服务器 IP；`curl -I https://catmat.example.com` 返回 200。

### 6.3 安全组/防火墙放行

**操作**
- 腾讯云控制台安全组：放行 `22`（SSH，建议限定来源 IP）、`80`、`443`；**不放行 8000**。
- 服务器内 `ufw` 同样只开 `22/80/443`：
  ```bash
  sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
  sudo ufw enable && sudo ufw status
  ```

**风险点**：放行 8000 或把 SSH 对全网开放 = 常见入侵入口；务必收紧。

---

## 7. 运维基线（上线前配置）

### 7.1 每日备份（平台内置 + COS 转储）

- 平台每次启动自动 `VACUUM INTO /opt/catmat/backups/catmat_YYYYMMDD.db`（同日去重）。
- 建议 cron 定期把 backups 转存 COS（防本机故障）：
  ```bash
  # 安装 coscmd 并配置后（每小时一次）
  0 * * * * coscmd upload -r /opt/catmat/backups/ catmat-backups/ >> /var/log/cos_backup.log 2>&1
  ```

### 7.2 监控告警（腾讯云）

- 云监控：CPU/内存/磁盘使用率告警（磁盘 ≥80% 预警——备份与日志增长）。
- 进程守护已由 systemd `Restart=always` 兜底。

### 7.3 加固清单

| 项 | 状态建议 |
|---|---|
| SSH 密钥登录 + 禁用密码 | ✅ 强烈建议（`/etc/ssh/sshd_config`：`PasswordAuthentication no`） |
| fail2ban | ✅ 已装即启用（拦爆破） |
| 后端鉴权 | ✅ 已实现；生产启用 `CATMAT_AUTH_ENABLED=1` 与 `CATMAT_LOCAL_AUTH_ENABLED=1`，Nginx Basic Auth 仅按需作为纵深防护 |
| 定期 `git pull` 更新 | 每次发布走步骤 2.2 |
| 日志与审计留痕 | 平台内置 `logs/server.log` + `audit` 表，可从服务器只读查看 |

---

## 8. 部署后验证清单（逐项打勾）

| # | 验证项 | 命令 / 方法 | 期望 |
|---|---|---|---|
| 1 | 服务进程 | `systemctl status catmat` | active (running) |
| 2 | 健康检查 | `curl -s http://127.0.0.1:8000/api/health` | `{"ok":true,...,"status":"up"}` |
| 3 | 公网 HTTPS | 浏览器 `https://catmat.example.com` | 正常打开，无证书告警 |
| 4 | HTTP 跳转 | 访问 `http://catmat.example.com` | 301 → https |
| 5 | 数据完整 | `/api/stats` 返回 `cp2k_keywords: 67511`（模式 A 若导入参数库） | 计数符合 |
| 6 | 界面功能 | 研究体系/知识库/文献库/机器学习页均可加载 | 无报错空态异常 |
| 7 | 大模型 | AI 助手发一条消息（若已配 `.env`） | 正常回复 |
| 8 | 上传限制 | 上传 >50 MB 结构文件 | 返回 413（客户端大小限制生效） |
| 9 | 备份 | `ls /opt/catmat/backups/` | 出现当日 `.db` |
| 10 | 日志 | `tail /opt/catmat/logs/server.log` | 启动/备份/预热/同步各就位 |
| 11 | 鉴权状态 | `curl -s https://…/api/auth/status` | `enabled:true`、`mode:"local"`、`local:true`、`wechat:false`、`ready:true` |

---

## 9. 回退方案

| 场景 | 处置 |
|---|---|
| 新版本异常 | `git log` 找到上个 commit → `git checkout <commit> -- .` → `systemctl restart catmat`（参考管理员手册 §7） |
| 数据误删 | 从 `backups/catmat_*.db` 恢复（管理员手册 §6.3） |
| 证书过期 | `sudo certbot renew --dry-run` 排障后 `renew`；或腾讯云控制台更新 SSL |
| 不想上公网了 | 安全组只留 22；或删除 nginx site → `systemctl stop nginx` |

---

## 附：与既有文档的关系

- 环境迁移、conda/CP2K/AiiDA 详细安装：`docs/迁移与公网部署评估.md`
- 服务启停/日志/备份/排障命令：`docs/admin-manual.md`
- 登录鉴权契约与后端 TODO：`docs/auth-security-design.md`
- 系统自带一键自测：`cd /opt/catmat && python3 -m unittest discover -s tests -p "test_*.py" -v`
