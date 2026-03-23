# MaxRead

Utilities for summarizing papers and transforming markdown summaries into Feishu Docs, plus a Feishu long-connection bot listener.

## Install

Run from project root:

```bash
pip install -r requirements.txt
```

## Quick Start

```bash
python -m transform reader/data/2512.16248/summarize.md --feishu --tenant vrfi1sk8a0
```

## Transform

Markdown -> Feishu docx transformation: parse `.md` into block types (headings, text, equations, bold/italic, bullet/ordered lists, tables) and optionally create Feishu cloud docs.

### Layout

- `transform/constants.py` - Feishu docx block type IDs (text, heading1-3, equation, bullet, ordered, table, code)
- `transform/inline.py` - `parse_inline(text)` for styled text runs
- `transform/parser.py` - `parse_md_to_blocks(md)` returning parsed blocks
- `transform/feishu_doc.py` - `create_summary_doc()`, `doc_url()` (Feishu API integration)

### Module Testing (CLI)

```bash
# Local: parse a fixed .md file, write blocks to JSON, print output path
python -m transform path/to/summarize.md
# -> prints e.g. /home/tianze/MaxRead/transform_out/summarize_blocks.json

python -m transform --md path/to/summarize.md --out-dir ./out

# With Feishu: create cloud doc and print doc URL
python -m transform --md path/to/summarize.md --feishu --title "My Summary"
# -> prints e.g. https://open.feishu.cn/docx/xxx
```

Output in local mode: one JSON file per run under `--out-dir` (default `transform_out/`), with `block_type` names and `content` for each block.

## Feishu Long Connection (Bot Listener)

Use Feishu Open Platform long connection to receive events and print messages in terminal without replying.

### 配置

1. 在 [飞书开放平台](https://open.feishu.cn/app) 创建应用，在 **凭证与基础信息** 中复制 **App ID**、**App Secret**。
2. **事件与回调 -> 事件配置**：选择「**使用长连接接收事件**」，添加事件 `im.message.receive_v1`。
3. **版本管理与发布**：创建版本并发布（未发布则无法检测到连接）。
4. **权限**：开通 `im:message`、`im:message:send_as_bot`、`im:message.p2p_msg` 并订阅「接收消息」。

### 安装与运行

```bash
cp feishu/.env.example feishu/.env
# 编辑 .env 填写 FEISHU_APP_ID、FEISHU_APP_SECRET；
# 若控制台配置了验证/加密则填 FEISHU_VERIFICATION_TOKEN、FEISHU_ENCRYPT_KEY
# 建议设置 CLAUDE_CLI_PATH（例如 /home/<user>/.local/bin/claude），
# 这样在新终端/新会话里也能稳定找到 Claude CLI
```

在项目根目录运行：

```bash
python -m feishu.bot_ws
```

连接成功后，在开放平台保存事件配置。之后给机器人发消息，终端会打印 `RECEIVED` 及消息内容。

### 其他

- 发消息：`python -m feishu.send_message "你好"`
- 代码中调用 API：`from feishu.bot import get_client`，用 `get_client()` 获取 Lark 客户端。