# QQ Bot 基础开发指南（精简准确版）

本文对应当前项目中的 [bot.py](../bot.py)。

目标：

1. 帮你看懂这份最小可用代码。
2. 只按你真实环境格式来讲，不讲多余兼容分支。

## 当前代码的设计原则

1. 只处理 napcat 的消息数组格式。
2. 只处理消息事件 post_type=message。
3. 只实现一个命令：hello -> hello。
4. 保持代码最小化，方便学习。

## 逐段解释（按当前行号）

### 第 1 到 4 行：导入模块

- 第 1 行：导入 json。
- 第 2 行：导入 logging。
- 第 3 行：导入 asynccontextmanager。
- 第 4 行：导入 FastAPI 和 WebSocket 相关类型。

### 第 6 到 12 行：日志配置

- 设置日志级别、格式、时间格式。
- 创建 logger 供全文件使用。

### 第 14 到 16 行：机器人类初始化

- 定义 QQBot 类。
- 用 set 保存当前连接客户端。

### 第 18 到 33 行：extract_text_message

作用：从 napcat 的 message 数组中提取文本。

逻辑：

1. 读取 event.message。
2. 不是 list 就返回空字符串。
3. 遍历每个消息段。
4. 只提取 type=text 的 data.text。
5. 拼接后做 trim 和 lower。

说明：

1. 这个函数现在固定按 message=array 处理。
2. 不再兼容 message 为字符串的旧分支。

### 第 35 到 53 行：handle_websocket

作用：处理 WebSocket 连接生命周期。

流程：

1. accept 连接。
2. 把连接加入 connected_clients。
3. 循环 receive_text。
4. json.loads 转成 event。
5. 调用 handle_event。
6. 断开或异常后清理连接。

### 第 55 到 72 行：handle_event

作用：处理业务事件。

流程：

1. 读取 post_type。
2. 不是 message 就 return。
3. 用 extract_text_message 得到纯文本 msg。
4. 读取 user_id、message_type、group_id。
5. 如果 msg 等于 hello：
   - 群聊调用 send_group_msg。
   - 私聊调用 send_private_msg。

### 第 74 到 84 行：send_private_msg

作用：发送私聊消息。

关键结构：

1. action=send_private_msg。
2. params.user_id 和 params.message。

### 第 86 到 96 行：send_group_msg

作用：发送群聊消息。

关键结构：

1. action=send_group_msg。
2. params.group_id 和 params.message。

### 第 98 到 99 行：创建 bot 实例

- 实例化 QQBot，供路由调用。

### 第 102 到 109 行：lifespan 生命周期

- yield 之前是启动逻辑。
- yield 之后是关闭逻辑。

### 第 111 行：创建 FastAPI 应用

- 将 lifespan 挂到 FastAPI。

### 第 114 到 118 行：WebSocket 路由

- 同时支持 /ws 和 /ws/。
- 收到连接后交给 bot.handle_websocket。

### 第 121 到 128 行：健康检查接口

- 返回服务状态、ws 路径、连接数。

### 第 130 到 137 行：直接运行入口

- 用 uvicorn 启动应用，监听 0.0.0.0:8081。

## 现在这版代码能做什么

1. 收到私聊 hello，回复 hello。
2. 收到群聊 hello，回复 hello。
3. 自动忽略心跳和生命周期事件。
4. 自动忽略图片、at 等非文本段（因为只提取 text）。

## 最小自测步骤

1. 发私聊 hello，预期收到 hello。
2. 发群聊 hello，预期群里收到 hello。
3. 发图片或仅@机器人，预期不触发 hello 回复。

## 下一步建议

1. 如果要支持 @机器人+命令：先解析 at 段，再解析后续 text 段。
2. 如果要支持图片处理：读取 image 段的 data.url。
3. 每新增一个能力，只加一个函数，避免一下子变复杂。
