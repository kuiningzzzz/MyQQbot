# MyQQbot

一个基于 FastAPI + WebSocket 的轻量 QQ 机器人后端，面向 NapCat / OneBot 风格事件上报。

## 项目结构

```text
src/
  main.py              # 应用入口，同时启动机器人后端和管理 WebUI
  bot.py               # WebSocket 消息处理和 OneBot 动作发送
  admin.py             # Feature 管理 WebUI
  feature/
    atom/               # Feature 抽象、JSON 文件读写等通用原子能力
      base.py
      plugin.py
      serial_queue.py
      json_file.py
    help/               # /help 功能及其内部实现
      admin.py
      feature.py
      plugin.py
    hello/              # /hello 示例功能
      feature.py
      plugin.py
    jm/                 # /jm 功能及其内部实现
      admin.py
      feature.py
      plugin.py
    pixiv/              # /pixiv 队列和 Pixiv API 下载适配器
      admin.py
      client.py
      feature.py
      plugin.py
    summary/            # 共用模型配置的 AI 总结与 AI 聊天能力组
      admin.py
      character.py
      chat.py
      client.py
      config.py
      feature.py
      history.py
      plugin.py
    catalog.py          # 唯一的功能组合入口
    manager.py          # 通用 Feature 注册、开关和消息调度
```

`atom` 中的代码不依赖任何具体业务 Feature。新增功能时，应建立独立的功能目录，
并由目录内的 `__init__.py` 暴露主要 Feature 类；只有被多个功能复用、且不包含业务语义的
小能力才放入 `atom`。

每个功能目录通过 `plugin.py` 组合自己的 Feature、管理端扩展和静态资源。应用核心只认识
`FeaturePlugin`，不会导入具体功能。新增功能后，只需在 `feature/catalog.py` 的组合入口加入
一个 `create_plugin()`；不需要修改 `main.py`、`admin.py`、`bot.py` 或 `manager.py`。

依赖方向保持为：应用入口依赖通用插件协议，插件组合各自功能，具体功能依赖 `atom`；
`atom`、Bot 和 FeatureManager 均不反向导入任何具体功能。

运行时数据会写入 `data/`：

- `data/features.json`：Feature 开关状态。
- `data/help.json`：`/help` 返回的聊天记录文本。
- `data/jm/tasks/`：JM 任务目录。每个任务都有独立的下载缓存和生成 PDF。
- `data/pixiv/tasks/`：Pixiv 任务目录。每个任务都有独立的图片缓存。
- `data/pixiv/config.json`：Pixiv refresh token 配置，由管理 WebUI 写入。
- `data/summary/config.json`：AI 服务 URL、模型名、API Key 和两项功能设置。
- `data/summary/characters.json`：AI 聊天人设。
- `data/summary/selections.json`：各群聊或私聊当前选择的人设。
- `data/summary/history/group-群号.json`、`private-用户号.json`：每个会话最近 200 条消息及发送者身份。

Docker 部署时，`docker-compose.yml` 会把项目目录下的 `./data` 挂载到容器内的 `/app/data`。因此重建镜像或重启容器不会删除 feature 开关和已生成文件。

## 运行

```bash
pip install -r requirements.txt
python -m src.main
```

服务端口：

- QQ Bot WebSocket：`ws://127.0.0.1:8081/ws`
- 管理 WebUI：`http://127.0.0.1:10010`
- 生成文件静态访问：`http://127.0.0.1:8081/files/文件名`
- Pixiv 图片静态访问：`http://127.0.0.1:8081/pixiv-files/文件名`

管理 WebUI 包含两个页面：

- “功能管理”：Feature 卡片默认折叠，卡片头部可直接开关功能，展开后显示功能自己的配置和维护操作。
- “实时日志”：进入页面时加载当前进程最近的历史日志，并通过 SSE 持续显示新日志。内存中最多保留 1000 条，刷新或重新进入日志页时默认显示最近 500 条。

Help 的详细设置支持编辑、保存，以及一键恢复并保存代码中的默认帮助文本。

Docker:

```bash
docker compose up --build
```

## 当前 Feature

### 帮助菜单

默认开启。

在群聊或私聊发送：

```text
/help
```

机器人会用 QQ 合并转发/聊天记录的形式返回当前支持的指令合集。管理 WebUI 里可以修改 `/help` 返回的文本内容，每一行会作为聊天记录中的一条消息发送。

### AI 功能：群聊总结与聊天

“AI 功能”使用一张 WebUI 卡片和同一套 OpenAI-compatible API 配置，卡片内可以分别启用或关闭群聊总结与 AI 聊天。默认 URL 和模型分别为 `https://api.deepseek.com` 与 `deepseek-chat`；URL 可以填写服务根地址或完整的 `/chat/completions` 地址。

功能会持续保存各群聊和私聊最近 200 条消息，以便启用后立即具备上下文。历史记录包含群昵称和用户 ID。图片当前保存为 `[图片]` 或 NapCat 提供的图片摘要，尚未执行实际 OCR；带完整节点的合并转发会展开为一条多行消息，只有转发 ID 时保存占位符。

#### 群聊总结

仅在群聊中使用：

```text
/short 50
```

数字必须是 10 至 200 的整数。机器人会将当前群最近指定数量的消息交给模型，并要求模型尽量在 150 个汉字内总结主要话题、结论、争议和待办事项。历史记录包含群昵称和用户 ID，以帮助模型区分参与者；触发总结的 `/short` 命令不会写入历史。

#### AI 聊天

群聊中 `@` 机器人，或在群聊/私聊文本中提及当前人设配置的“姓名”，会触发一次必答并令当前会话进入 60 秒活跃期。活跃期内的新消息会重置计时，模型根据上下文决定是否继续接话；模型选择不回复或 60 秒内无人发言后，机器人重新休眠。

每次调用默认读取最近 200 条消息，可在 WebUI 中改为 10 至 200。模型以结构化结果返回是否回复以及 1 至 4 条短消息，机器人会将它们分别发送。

查看人设：

```text
/character
```

切换当前群聊或私聊的人设：

```text
/change-character 普通QQ群群友
```

WebUI 支持添加、修改、删除人设的名字、姓名、描述和正文提示词。“名字”用于管理和 `/change-character`，“姓名”用于对话中的角色身份与文本唤醒。首次运行会自动建立“普通QQ群群友”人设，其默认姓名为“奎大宁”；删除最后一个人设时也会恢复该默认人设。人设选择按群聊或私聊分别保存。

API Key 会以明文保存在 Git 忽略的 `data/summary/config.json` 中，管理 API 不会将 Key 回传给浏览器。请限制管理 WebUI 和 `data/` 目录的访问权限。

### JM 漫画 PDF

默认关闭，需要在管理 WebUI 里打开。

打开后，在群聊或私聊发送：

```text
/jm 123456
```

机器人会把请求加入 JM 队列。队列为空时会立即开始处理；队列非空时会回复“已添加至处理队列”，并发送当前队列状态。

查询队列：

```text
/jm status
```

队列状态会以聊天记录形式列出处理中和待处理任务，包括序号、请求时间和漫画 ID。JM 队列同时只处理一个任务，前一个任务完成后才会处理下一个。

该功能依赖 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 的 `jmcomic` 包。该库从 v2.6.19 起支持 `Feature.export_pdf`，可在下载完成后自动导出 PDF。

如果 NapCat 和 bot 不在同一个文件系统里，建议设置环境变量 `MYQQBOT_PUBLIC_BASE_URL`，例如：

```bash
MYQQBOT_PUBLIC_BASE_URL=http://127.0.0.1:8081
```

这样发送文件时会把 `file` 参数指向 `/files/文件名` 的 URL，而不是 bot 本机的绝对路径。

`docker-compose.yml` 默认已经设置：

```yaml
- MYQQBOT_PUBLIC_BASE_URL=http://127.0.0.1:8081
```

如果 NapCat 不在同一台宿主机上，请改成它能访问到的地址，例如 `http://服务器IP:8081`。

成功发出 PDF 发送动作后，JM 功能会延迟清理该任务自己的目录。默认延迟 300 秒，避免 NapCat 还没下载完文件就被删除。可以通过环境变量调整：

```yaml
- JM_CLEANUP_DELAY_SECONDS=300
```

### Pixiv 图片

默认关闭，需要在管理 WebUI 里打开。打开后发送：

```text
/pixiv 12345678
```

机器人会下载该作品的原图，并以 QQ 合并转发/聊天记录发送。多图作品每个图片节点占一条聊天记录；超过 100 张时，会自动拆成每组最多 100 张的多组合并转发。

查询队列：

```text
/pixiv status
```

Pixiv 队列同时只处理一个任务。队列忙时，新请求会收到“已添加至处理队列”的回复以及当前队列状态。

公开作品会尝试直接获取。要稳定获取需要登录或 R18 的作品，请为具有相应浏览权限的 Pixiv 账号取得 refresh token，然后在管理 WebUI 的“Pixiv 图片”功能卡片中粘贴并保存。保存后立即生效，无需重启服务。

获取 token 可参考 [PixivPy README 中推荐的 OAuth 工具](https://github.com/upbit/pixivpy#pixivpy3)。后端只向 WebUI 返回“已配置/未配置”状态，不会回传 token 原文。token 会以明文保存在被 Git 忽略的 `data/pixiv/config.json` 中，因此仍应限制 `data/` 目录和管理 WebUI 的访问权限。

管理 WebUI 的 Pixiv 详细设置中提供本地获取 token 的操作教程。OAuth 登录和 token 交换应在能够正常访问 Pixiv 的个人电脑上完成，然后只把最终得到的 refresh token 粘贴到管理页面保存。服务器不负责 OAuth 交换，也不需要接触 Pixiv 用户名或密码。

图片发送后默认延迟 300 秒清理任务目录，可通过以下变量调整：

```yaml
- PIXIV_CLEANUP_DELAY_SECONDS=300
```

Pixiv API、OAuth 服务和图片 CDN 都必须能被 bot 容器访问。`docker-compose.yml` 默认将容器流量指向宿主机 v2rayA 的 SOCKS5 代理端口 `20170`：

```text
socks5h://host.docker.internal:20170
```

`socks5h` 会同时代理域名解析，避免容器本地 DNS 对 Pixiv 域名解析异常。项目通过 `requests[socks]` 安装所需的 PySocks 支持。

Linux Docker 通过 `host-gateway` 映射宿主机。请确认 v2rayA 已开启“允许来自局域网的连接/地址共享”，并监听 Docker 网桥能够访问的地址，而不只是宿主机的 `127.0.0.1`。代理地址可以通过宿主机环境变量覆盖；显式设置为空字符串可以关闭：

```bash
MYQQBOT_PROXY=socks5h://其他代理地址:端口 docker compose up -d
MYQQBOT_PROXY= docker compose up -d
```

该代理配置会作用于容器内所有遵循标准代理环境变量的 HTTP 客户端，而不只 Pixiv。

## 文档

- [QQ Bot 基础开发指南](docs/QQBot基础开发指南.md)
- [Napcat 事件 JSON 格式说明](docs/Napcat事件JSON格式说明.md)
