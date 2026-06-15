# Napcat 事件 JSON 格式说明（基于真实日志）

本文完全基于你提供的真实日志样本整理，不是通用猜测版。

目标：

1. 搞清楚 napcat 推送过来的各种事件 JSON 长什么样。
2. 让你后续写功能时，知道每种消息该读哪些字段。
3. 区分事件上报 JSON 和发送动作响应 JSON。

## 1. 先说结论

从你的日志可以确认：

1. 消息事件的 message 字段是数组格式，不是纯字符串。
2. message_format 字段明确是 array。
3. 文本、@、图片都通过 message 数组里的 type 区分。
4. 机器人发送动作后，会收到 status ok 的动作响应 JSON。

## 2. 事件总分类

你当前环境里出现了 3 大类 JSON：

1. meta_event：生命周期、心跳。
2. message：私聊/群聊消息。
3. action 响应：你发消息后的返回结果（status、retcode 等）。

判断入口字段：

1. post_type 是 meta_event 或 message。
2. 如果存在 status、retcode、stream，通常是动作响应而不是消息事件。

## 3. meta_event 类型

### 3.1 连接事件（lifecycle connect）

示例：

```json
{
  "time": 1773826303,
  "self_id": 1821106487,
  "post_type": "meta_event",
  "meta_event_type": "lifecycle",
  "sub_type": "connect"
}
```

字段说明：

1. post_type=meta_event：系统事件，不是聊天消息。
2. meta_event_type=lifecycle：生命周期事件。
3. sub_type=connect：连接建立。

### 3.2 心跳事件（heartbeat）

示例：

```json
{
  "time": 1773826423,
  "self_id": 1821106487,
  "post_type": "meta_event",
  "meta_event_type": "heartbeat",
  "status": {
    "online": true,
    "good": true
  },
  "interval": 30000
}
```

字段说明：

1. meta_event_type=heartbeat：心跳。
2. interval=30000：约每 30 秒一次。
3. status.online 和 status.good：在线和健康状态。

## 4. message 类型（最重要）

### 4.1 私聊文本

示例：

```json
{
  "self_id": 1821106487,
  "user_id": 1396298976,
  "time": 1773826329,
  "message_id": 453934292,
  "message_type": "private",
  "sender": {
    "user_id": 1396298976,
    "nickname": "奎宁.exe未响应",
    "card": ""
  },
  "raw_message": "hello",
  "sub_type": "friend",
  "message": [
    {
      "type": "text",
      "data": {
        "text": "hello"
      }
    }
  ],
  "message_format": "array",
  "post_type": "message",
  "target_id": 1396298976
}
```

字段说明：

1. post_type=message：聊天消息事件。
2. message_type=private：私聊。
3. message_format=array：消息体是数组。
4. message[0].type=text：文本段。
5. message[0].data.text：真正文本内容。
6. raw_message：原始消息字符串表现。

### 4.2 群聊文本

示例：

```json
{
  "self_id": 1821106487,
  "user_id": 1396298976,
  "time": 1773826346,
  "message_id": 553338307,
  "message_type": "group",
  "sender": {
    "user_id": 1396298976,
    "nickname": "奎宁.exe未响应",
    "card": "",
    "role": "owner"
  },
  "raw_message": "你好",
  "sub_type": "normal",
  "message": [
    {
      "type": "text",
      "data": {
        "text": "你好"
      }
    }
  ],
  "message_format": "array",
  "post_type": "message",
  "group_id": 631278114,
  "group_name": "吼吼"
}
```

新增关注字段：

1. message_type=group：群聊。
2. group_id：群号。
3. group_name：群名。
4. sender.role：成员角色。

### 4.3 群里只 @ 机器人

示例：

```json
{
  "message_type": "group",
  "raw_message": "[CQ:at,qq=1821106487]",
  "message": [
    {
      "type": "at",
      "data": {
        "qq": "1821106487"
      }
    },
    {
      "type": "text",
      "data": {
        "text": " "
      }
    }
  ],
  "message_format": "array",
  "post_type": "message",
  "group_id": 631278114
}
```

字段说明：

1. type=at：表示艾特段。
2. data.qq：被艾特的 QQ。
3. 文本段可能只有空格，所以提取后可能是空字符串。

### 4.4 群里 @ 机器人并附带文字

示例：

```json
{
  "message_type": "group",
  "raw_message": "[CQ:at,qq=1821106487] 你好",
  "message": [
    {
      "type": "at",
      "data": {
        "qq": "1821106487"
      }
    },
    {
      "type": "text",
      "data": {
        "text": " 你好"
      }
    }
  ],
  "message_format": "array",
  "post_type": "message",
  "group_id": 631278114
}
```

字段说明：

1. 先是 at 段，再是 text 段。
2. text 前面可能带空格，解析后记得 trim。

### 4.5 群里图片或表情包

示例：

```json
{
  "message_type": "group",
  "raw_message": "[CQ:image,file=CC4F5A78C96469D934BAEE4700B00F70.jpg,...]",
  "message": [
    {
      "type": "image",
      "data": {
        "summary": "",
        "file": "CC4F5A78C96469D934BAEE4700B00F70.jpg",
        "sub_type": 0,
        "url": "https://multimedia.nt.qq.com.cn/download?...",
        "file_size": "52954"
      }
    }
  ],
  "message_format": "array",
  "post_type": "message",
  "group_id": 631278114
}
```

字段说明：

1. type=image：图片段。
2. data.url：可下载 URL。
3. data.file：文件标识名。
4. data.sub_type：普通图片或动画表情等子类型。
5. data.file_size：文件大小，字符串形式。

## 5. 动作响应 JSON（不是消息事件）

当你的机器人调用发送动作后，会收到类似：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {
    "message_id": 1934224129
  },
  "message": "",
  "wording": "",
  "echo": "",
  "stream": "normal-action"
}
```

字段说明：

1. status=ok：动作执行成功。
2. retcode=0：成功码。
3. data.message_id：发送后的消息 ID。
4. stream=normal-action：动作响应流。

注意：

1. 这类 JSON 没有 post_type=message。
2. 业务消息处理函数应忽略这类响应包。

## 6. 推荐的解析顺序

建议在代码里按这个顺序判断：

1. 先看 post_type 是否等于 message。
2. 再看 message_type 是 private 还是 group。
3. 读取 message 数组。
4. 按 type 分流：text、at、image。
5. 文本命令匹配只基于 text 段拼接结果。

## 7. 你当前项目可固定使用的格式

基于这次日志，你可以把输入格式固定为：

1. message_format 视为 array。
2. 消息文本从 message 数组里 type=text 的 data.text 提取。
3. at 判断从 type=at 的 data.qq 读取。
4. 图片处理从 type=image 的 data.url 读取。

这样就可以不再依赖“message 是纯字符串”的兼容逻辑。

## 8. 给后续功能的直接建议

1. 做命令时：只读文本段，不要直接匹配 raw_message。
2. 做群里唤醒时：优先判断是否 at 了 self_id。
3. 做图片功能时：判断 type=image，再读取 data.url 下载处理。
4. 日志里保留原始事件输出开关，排查非常有用。

以上格式说明全部来自你当前运行环境的真实样本，可直接作为项目内规范使用。
