# Feishu 长连接：接收消息并打印到终端

使用飞书开放平台「使用长连接接收事件」，收到消息后只在终端打印，不回复。

## 配置

1. [飞书开放平台](https://open.feishu.cn/app) 创建应用，在 **凭证与基础信息** 中复制 **App ID**、**App Secret**。
2. **事件与回调 → 事件配置**：选择「**使用长连接接收事件**」，添加事件 `im.message.receive_v1`。
3. **版本管理与发布**：创建版本并发布（未发布则无法检测到连接）。
4. **权限**：开通 `im:message`、`im:message:send_as_bot`、`im:message.p2p_msg` 并订阅「接收消息」。

## 安装与运行

```bash
pip install -r feishu/requirements.txt
cp feishu/.env.example feishu/.env
# 编辑 .env 填写 FEISHU_APP_ID、FEISHU_APP_SECRET；若控制台配置了验证/加密则填 FEISHU_VERIFICATION_TOKEN、FEISHU_ENCRYPT_KEY
```

在项目根目录运行：

```bash
python -m feishu.bot_ws
```

连接成功后，在开放平台保存事件配置。之后给机器人发消息，终端会打印 `RECEIVED` 及消息内容。

## 其他

- **发消息**：`python -m feishu.send_message "你好"`（需配置 open_id 或联系人在应用内发过「我是天择」等以登记）。
- **代码中调用 API**：`from feishu.bot import get_client`，用 `get_client()` 获取 Lark 客户端。
