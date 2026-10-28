# CataMind · 催化智构

催化材料计算平台：结构建模、CP2K 输入与任务管理、资源监控、后处理、数据集、机器学习和主动学习。当前服务端版本为 **2.4.1**。

Web 服务使用 Python 标准库（HTTP + SQLite），前端无需构建。ASE、AiiDA、CP2K、NumPy、机器学习框架等是按功能安装的外部依赖；下载源码不会自动安装计算软件或包含生产数据。

## 快速启动

Python 3.9+，在源码目录执行：

```bash
python3 catmat.py serve --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000`。默认无鉴权模式仅用于本机体验；不要通过反向代理公开该模式。首次启动生成空数据库和运行目录，不自动导入研究数据。

多用户部署先设置鉴权，再启动：

```bash
export CATMAT_AUTH_ENABLED=1
export CATMAT_LOCAL_AUTH_ENABLED=1
export CATMAT_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
python3 catmat.py serve --host 127.0.0.1 --port 8000
```

部署时把生成的密钥持久保存在权限受限的服务配置中，勿每次启动重新生成。首次账号初始化、HTTPS 反向代理和权限配置见[管理员手册](docs/admin-manual.md)。`.env.example` 是配置模板，程序不会自动读取 `.env`；应由 shell 或服务管理器加载。

## 功能与边界

| 功能 | 实现与依赖 |
|---|---|
| 结构建模、结构预览 | ASE；声明式参数输入 |
| CP2K / AiiDA 计算 | 需配置真实可执行程序、AiiDA profile、code 与计算资源 |
| 任务调度、进程监控 | 资源准入、进程聚合、CPU 卡片查看运行进程 |
| 后处理工具集 | 差分密度、Bader 结果导入、PDOS、输出审查；服务器选择或本地上传输入 |
| Bader 执行 | 需独立安装 Bader 并配置 `CATMAT_BADER_BIN` |
| 数据集 / ML / AL | 需配置含相应依赖的 Python 环境；训练使用异步任务 |
| DP-GEN | 真实闭环尚未开放；mock 仅供开发验证 |
| 振动热化学、功函数等后处理 | 尚在规划，不作为已实现功能宣传 |

后处理执行完成不等于科学验收通过，通用工具结果不自动进入训练集。差分密度的全空间积分不能替代原子 Bader 电荷。模型排序请用多模型对比接口，旧训练集指标不能作为泛化证据。

## 配置计算环境

配置项见 [.env.example](.env.example)。常用项为 `CATMAT_AII_PY`、`CATMAT_VERDI`、`CATMAT_ML_PY`、`CATMAT_CP2K_ROOT`、`CATMAT_AIIDA_FILES` 和 `CATMAT_POSTPROCESS_ROOTS`。AiiDA 与 ML 环境独立，按用途安装依赖。

需要 AiiDA 工作流插件时，在相应环境内执行：

```bash
python -m pip install -e ./catmat_tools --no-deps
verdi plugin list aiida.workflows
```

该安装入口用于源码目录的 editable 安装；不是包含前端资源的独立 wheel 发行包。计算软件的版本兼容性和真实任务仍需在目标机器验收。

## 目录

- `server.py`、`catmat.py`：服务与命令入口。
- `catmat_process_monitor.py`、`catmat_resource_governor.py`、`catmat_postprocessing.py`：监控、资源准入与后处理任务。
- `catmat_tools/`：计算、解析、训练适配模块。
- `index.html`、`static/`：界面、交互脚本和带许可声明的前端依赖。
- `docs/`、`tests/`、`scripts/`：使用文档、测试及源码发布检查。
- `data/seed_*.json`：可选示例种子；运行数据和账号不进入发行包。

## 测试与发布

```bash
# 无真实计算任务的回归；需要 Python 和 Node.js
python3 scripts/check_release.py
# 使用临时数据库和本机随机端口的 HTTP 流程测试
python3 -m unittest tests.test_postprocess_http -v
# 只读检查源码发布清单
python3 scripts/prepare_release.py
# 输出到一个尚不存在的新目录
python3 scripts/prepare_release.py --output .release/catamind-source
```

发布脚本使用明确的文件范围，并保留 SHA-256 清单。数据库、账号、密钥、研究结果、日志、备份、旧桌面包和 Git 历史不会复制。上传 GitHub 前从新目录检查并建立仓库，避免把现有部署目录及其历史直接推送。详见[发布说明](docs/RELEASING.md)。

## 已知坑与注意事项

- 修改后端后必须重启 Web 服务；刷新页面只更新前端。重启须避开仍在运行的平台内后台任务。
- 数据集导入先预览并显式指定列映射，不能只依赖自动推断。
- 服务器执行助手及其共享文件仅管理员可访问；它具备实际服务器操作能力，不能视为隔离的普通聊天机器人。
- GitHub 源码发布不等于平台已经具备面向不可信公网用户的托管隔离能力。
- 不要对生产目录直接执行所有历史测试；使用上述明确列出的回归入口。

## 文档与许可

[用户手册](docs/user-manual.md) · [管理员手册](docs/admin-manual.md) · [系统架构](ARCHITECTURE.md)

项目许可证待维护者确定；在根目录正式添加 `LICENSE` 前，不应将本项目描述为已经授予开源使用许可。第三方前端组件的许可声明保留在 `static/vendor/`。
