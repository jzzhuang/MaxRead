# MaxRead

MaxRead 是一个飞书论文阅读机器人：接收 arXiv 链接、arXiv ID 或 PDF，提取论文文本，通过 OpenAI Responses API 生成中文精读笔记，再写入飞书云文档。仓库也提供一个只监听本机的 MCP 服务，用于检索已经生成的笔记。

## 安全设计

论文和评论都按不可信输入处理。发布版不会启动 Codex CLI、Cursor 或 shell agent；模型调用没有工具权限，模型输出只会作为 Markdown 文本保存。归档解压有路径、成员类型、成员数和体积限制，图片只能从论文目录内的非符号链接常规文件上传。新建飞书文档保留平台默认私有权限，不会自动开启“任何人可编辑”。

运行时仍会把论文文本和用户评论发送给你配置的 OpenAI API 项目，并把生成结果写入飞书。部署前请确认组织的数据政策、OpenAI 项目设置和飞书权限策略允许这一数据流。

## 环境要求

- Python 3.10+
- Node.js 18+（仅用于 KaTeX 公式校验）
- 飞书自建应用
- OpenAI API key

## 本地安装

```bash
git clone https://github.com/jzzhuang/MaxRead.git
cd MaxRead
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
npm ci
cp .env.example .env
```

编辑 `.env`，至少填写：

```dotenv
FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
OPENAI_API_KEY=...
```

在飞书开放平台为应用启用长连接事件，订阅消息接收、机器人菜单和（如需评论回复）云文档评论事件，并按实际启用功能授予最小 API 权限。飞书权限名称会随平台版本调整；请根据启动日志中的缺失权限提示补充，不要授予与本项目无关的全局管理权限。

启动机器人：

```bash
python MaxRead.py
```

单独总结论文：

```bash
python -m reader.pipeline 2401.12345
```

## Docker

```bash
docker build -t maxread .
docker run --rm --env-file .env -v maxread-data:/app/data maxread
```

镜像以非 root 用户运行。生产环境请使用 secret manager 注入环境变量，并为容器设置只读根文件系统、资源限制和受控出站网络。

## MCP 服务

```bash
python mcp_server.py
```

默认地址是 `http://127.0.0.1:8765/mcp`；可用 `MCP_PORT` 修改端口。服务只绑定 loopback。`summarize_paper` 会产生 OpenAI API 调用和飞书之外的 arXiv 下载流量，因此不要把该 MCP 端口公开到不可信网络。

## 配置

配置示例见 [.env.example](.env.example)。主要限制项：

- `MAXREAD_MAX_CONTEXT_CHARS`：单次发送给模型的论文文本总字符上限，默认 600,000。
- `MAXREAD_MAX_FILE_CHARS`：单文件字符上限，默认 200,000。
- `MAXREAD_MAX_CONTEXT_FILES`：单次材料文件数上限，默认 200。
- `MAXREAD_MAX_QUEUED_JOBS`：等待中与运行中的任务文件总上限，默认 500；同一任务最多合并 100 个等待者。
- PDF 固定限制为 50 MiB（飞书上传）/100 MiB（arXiv 下载）和 300 页；归档解压限制为 10,000 个成员、单文件 50 MiB、总计 250 MiB。
- `MAXREAD_ENABLE_COMMENT_AUDIT_LOG`：默认关闭；开启后也只保存状态、时间和模型名，不保存评论、回复或用户 ID。

运行数据默认放在 `reader/data/`，队列放在 `.maxread_queue/`。论文目录会保存原文、笔记和飞书文档链接；任务执行期间队列会保存回复所必需的消息/租户标识。默认只输出控制台日志，只有显式设置 `MAXREAD_LOG_FILE` 才写日志文件。这些目录以及日志、`.env` 和生成笔记都已被 Git 忽略。请限制目录权限、制定保留周期，并在备份或共享前检查其中是否包含论文正文、飞书链接或组织数据。

## 开发与验证

```bash
python -m pip install pytest ruff bandit pip-audit
python -m pytest
python -m compileall -q .
ruff check .
bandit -q -ll -r . -x tests,.venv
pip-audit -r requirements.txt
```

安全问题请查看 [SECURITY.md](SECURITY.md)。贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

MaxRead 以 [GNU Affero General Public License v3.0 only](LICENSE) 发布。该选择也与核心 PDF 依赖 PyMuPDF/PyMuPDF4LLM 的 AGPL 使用方式保持一致；如需闭源部署，请自行确认并取得所需商业许可。第三方依赖说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
