# MaxRead

arXiv 论文 & PDF 自动精读 → 飞书云文档。

## 用法

在飞书里给 bot 发消息，支持两种输入：

| 输入 | 示例 |
|---|---|
| arXiv 链接或 ID | `https://arxiv.org/abs/2301.12345`、`2301.12345` |
| PDF 文件 | 直接上传 `.pdf` 文件 |

一条消息可以包含多个 arXiv ID，每篇单独处理、单独回复。

## 处理流程

1. 收到消息，回复 emoji 表示进度（`Get` → `OnIt` → `Typing`）
2. 下载论文源码 / 提取 PDF 文字
3. Claude 生成中文精读笔记（方法、公式、实验、消融、图表逐一解读）
4. 转为飞书云文档（支持标题、公式、表格、图片、代码块）
5. 回复文档链接

## 输出

回复格式为飞书云文档链接，文档内容是面向中文 ML 从业者的深度阅读笔记，不是翻译。

## 注意事项

- 仅响应 arXiv 链接/ID 和 PDF 文件，其他消息静默忽略
- 同一篇论文有缓存，重复发送直接返回已有文档
- PDF-only 论文（无 TeX 源码）会标注"表格和图文对应可能不准"
- 最多 8 个任务并行，超出自动排队
- 单篇最多重试 5 次

## 配置

飞书凭证放在 `feishu/.env`：

```
FEISHU_APP_ID=xxx
FEISHU_APP_SECRET=xxx
```

运行：

```bash
python3 MaxRead.py
```

## Transform 模块

Markdown → Feishu docx 转换：解析 `.md` 为 block（标题、文本、公式、列表、表格、图片、代码块），调 Feishu API 创建云文档。

公式经过 KaTeX 本地验证，结构性错误（未闭合花括号、参数缺失等）会记录 warning；角括号 `<`/`>` 自动转义避免被飞书 HTML 解析器吞掉。

CLI 测试：

```bash
# 本地解析，输出 JSON
python -m transform path/to/summarize.md

# 创建飞书云文档
python -m transform --md path/to/summarize.md --feishu --title "My Summary"
```

## 日志

运行日志写入 `maxread.log`（10MB 滚动，保留 3 份备份），同时输出到 console。
