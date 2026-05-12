根据用户提供的 prompt 调用图片生成 API，生成图片并下载到本地。

## 输入

用户输入: $ARGUMENTS

## 执行步骤

1. **解析参数**：从用户输入中提取 prompt 和可选的模型参数。
   - 如果输入以 `--model xxx` 开头，则提取模型名称，剩余部分为 prompt
   - 支持的模型: `4o`(默认), `nano_banana`, `seeddream`, `kontext`
   - 如果没有指定模型，默认使用 `4o`

2. **确定端点**：根据模型名称映射到 API 端点：
   - `4o` → `https://ys-api.xaminim.com/api/gen_img_4o`
   - `nano_banana` → `https://ys-api.xaminim.com/api/nano_banana_image`
   - `seeddream` → `https://ys-api.xaminim.com/api/seeddream`
   - `kontext` → `https://ys-api.xaminim.com/api/kontext`

3. **调用 API**：使用 Bash 工具执行 curl 命令：
   ```
   curl -s -L --max-time 240 "<endpoint>" -H "Content-Type: application/json" -d '{"prompt":"<用户prompt>"}'
   ```

4. **处理响应**：
   - 解析返回的 JSON，检查 `success` 字段
   - 如果成功，从 `data` 字段提取图片 URL（kontext 端点返回的是数组，取第一个）
   - 如果失败且使用的是默认模型，自动 fallback 到 `nano_banana_image`，再失败则尝试 `seeddream`

5. **下载图片**：使用 curl 将图片下载到当前工作目录：
   ```
   curl -s -L --max-time 30 -o "generated_$(date +%Y%m%d_%H%M%S).png" "<图片URL>"
   ```

6. **展示结果**：使用 Read 工具读取下载的图片文件，展示给用户。同时告知用户图片的保存路径。

## 注意事项

- prompt 中如果包含单引号，需要正确转义
- 超时时间设为 60 秒，因为图片生成可能需要一些时间
- 下载的文件扩展名根据 URL 中的实际扩展名决定（.png 或 .jpeg）
