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
      memory_service.py
      memory_store.py
      plugin.py
    web_search/         # 可供聊天、新闻等功能复用的联网搜索服务
      config.py
      service.py
    news/               # AI 每日新闻调度、订阅、生成和播报
      admin.py
      config.py
      feature.py
      store.py
      plugin.py
    catalog.py          # 唯一的功能组合入口
    manager.py          # 通用 Feature 注册、开关和消息调度
  master/               # 始终启用的主人认证基础设施（不属于 Feature）
    admin.py
    service.py
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
- `data/master.json`：当前绑定的机器人主人 QQ ID。
- `data/jm/tasks/`：JM 任务目录。每个任务都有独立的下载缓存和生成 PDF。
- `data/pixiv/tasks/`：Pixiv 任务目录。每个任务都有独立的图片缓存。
- `data/pixiv/config.json`：Pixiv refresh token 配置，由管理 WebUI 写入。
- `data/summary/config.json`：AI 服务 URL、模型名、API Key 和两项功能设置。
- `data/summary/characters.json`：AI 聊天人设。
- `data/summary/selections.json`：各群聊或私聊当前选择的人设。
- `data/summary/history/group-群号.json`、`private-用户号.json`：每个会话最近 200 条消息及发送者身份。
- `data/summary/memory.db`：SQLite 长期记忆、冲突版本和来源记录。
- `data/web_search/config.json`：联网搜索服务、URL、API Key 和结果限制。
- `data/news/config.json`：每日新闻的北京时间、条数和内容需求。
- `data/news/subscriptions.json`：已开启新闻播报的群聊和私聊。
- `data/news/history.json`：最近5次新闻内容和最近调度日期。

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

管理 WebUI 包含三个页面：

- “功能管理”：Feature 卡片默认折叠，卡片头部可直接开关功能，展开后显示功能自己的配置和维护操作。
- “实时日志”：进入页面时加载当前进程最近的历史日志，并通过 SSE 持续显示新日志。内存中最多保留 1000 条，刷新或重新进入日志页时默认显示最近 500 条。
- “主人管理”：生成一次性鉴别码、查看当前主人 QQ ID，不受 Feature 开关影响。

Help 的详细设置支持编辑、保存，以及一键恢复并保存代码中的默认帮助文本。

Docker:

```bash
docker compose up --build
```

## 核心能力与当前 Feature

### 主人管理

主人认证是始终启用的应用基础设施，不属于可开关的 Feature。在管理 WebUI 顶部进入
“主人管理”页面，点击“生成鉴别码”，然后使用准备绑定的
QQ 账号向机器人发送页面给出的完整指令：

```text
/set-master 鉴别码
```

鉴别码由 10 位小写字母和数字组成，10 分钟内有效，只能成功使用一次。每次重新生成都会
立即废弃之前的鉴别码；绑定成功后，QQ 用户 ID 会保存到 `data/master.json`。需要更换主人时，
直接在 WebUI 生成新鉴别码并使用新的 QQ 账号绑定即可。鉴别码只保存在进程内存中，服务重启后失效。

### 帮助菜单

默认开启。

在群聊或私聊发送：

```text
/help
```

机器人会用 QQ 合并转发/聊天记录的形式返回当前支持的指令合集。普通用户和主人使用两套独立帮助文本，只有主人版默认包含人设与长期记忆管理指令。管理 WebUI 可以分别修改两套文本：单独一行的 `---` 用于分隔不同消息，普通换行会保留在同一条消息中。例如：

```text
第一条消息的第一行
第一条消息的第二行
---
第二条消息
```

### AI 功能：群聊总结与聊天

“AI 功能”使用一张 WebUI 卡片和同一套 OpenAI-compatible API 配置，卡片内可以分别启用或关闭群聊总结与 AI 聊天。默认 URL 和模型分别为 `https://api.deepseek.com` 与 `deepseek-chat`；URL 可以填写服务根地址或完整的 `/chat/completions` 地址。

共享模型设置还包括：

- 思考模式：跟随服务默认、明确启用或明确禁用。
- 思考强度：`high` 或 `max`，仅在明确启用思考时发送。
- 聊天动态窗口：默认从 150 条开始增长，最多 200 条。
- 输入上下文 Token 预算：默认 16000，用保守估算从最旧消息开始裁剪，范围 1024–128000。
- 最大生成 Token：默认 8192，范围 256–65536；DeepSeek 思考模式下该额度由思维链和最终回答共同使用。

思考模式遵循 [DeepSeek 官方参数说明](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)。启用思考时不会发送无效的 `temperature`。如果模型因达到 `max_tokens` 而只有 `reasoning_content`、没有最终 `content`，日志和聊天错误会明确提示提高最大生成 Token 或降低思考强度。

模型 API 在网络层使用 SSE 流式响应，以便尽早收到响应头并在生成期间持续接收数据，降低长时间等待首个完整响应导致读取超时的概率。应用会在内存中完整缓冲最终 `content`，不会把 token 分片发送到 QQ；QQ 消息仍按模型返回的短消息列表逐条发送。不支持 SSE、返回普通 JSON 的 OpenAI-compatible 服务也可以自动兼容。

功能会持续保存各群聊和私聊最近 200 条消息，以便启用后立即具备上下文。历史记录包含群昵称和用户 ID。图片当前保存为 `[图片]` 或 NapCat 提供的图片摘要，尚未执行实际 OCR；带完整节点的合并转发会展开为一条多行消息，只有转发 ID 时保存占位符。

#### 群聊总结

仅在群聊中使用：

```text
/short 50
```

数字必须是 10 至 200 的整数。机器人会将当前群最近指定数量的消息交给模型，并要求模型尽量在 150 个汉字内总结主要话题、结论、争议和待办事项。历史记录包含群昵称和用户 ID，以帮助模型区分参与者；触发总结的 `/short` 命令不会写入历史。

#### AI 聊天

群聊中 `@` 机器人，或在群聊/私聊文本中提及当前人设配置的“姓名”，会触发一次必答并令当前会话进入 60 秒活跃期。活跃期内的新消息会立即重置计时；同一会话始终只运行一个真实模型请求，生成期间到达的多条消息会合并为最新版本。旧结果返回后会被丢弃，再使用包含全部新消息的上下文重新组织回复，避免密集群聊耗尽 HTTP 线程池。模型选择不回复时只会保持沉默并重新计时，不会进入休眠；退出活跃期的唯一条件是连续 60 秒没有需要处理的新消息，到期时会输出日志。

以 `/` 开头的其他机器人指令和机器人自己发送的消息会被 AI 聊天状态机忽略：它们不会取消当前生成任务，也不会刷新 60 秒活跃期。机器人消息仍会作为对话上下文写入历史；若 NapCat 回显了刚刚已经主动记录的同一条回复，则会自动去重。

AI 聊天默认用最近 150 条消息建立稳定前缀，随后只在尾部追加新消息，最多增长到 200 条；再有新消息时窗口重置为最新 150 条。起始值和最大值均可在 WebUI 调整。这样每轮通常都能复用上一轮的提示词前缀，只有窗口重置或 Token 预算不足时发生一次缓存失配。模型以结构化结果返回是否回复以及 1 至 4 条短消息，机器人会严格按返回顺序逐条发送，相邻消息间隔 1 秒。

#### 联网搜索工具

联网搜索是独立于聊天实现的可复用服务，当前接入 AI 聊天，后续新闻播报等功能可以直接复用。启用后，模型只在需要实时、近期或外部核实信息时自主调用 `web_search`，搜索结果再作为工具消息交给模型；普通聊天不会固定执行搜索。一次回复最多进行三轮工具调用，每轮最多三个查询。

WebUI 支持 Tavily 和 SearXNG。Tavily 默认地址为 `https://api.tavily.com/search`，需要 API Key；SearXNG 可填写自建实例地址，通常不需要 Key，但实例必须启用 JSON 搜索结果。搜索请求与 AI 请求一样不继承系统代理。网页标题、摘要和 URL 会被明确标记为不可信外部资料，不能作为系统指令执行。

### AI 每日新闻播报

新闻模块在代码层保持独立，以便复用调度和订阅逻辑，但设置统一并入 WebUI 的“AI 功能”卡片，与聊天共用同一套模型 API 和联网搜索 API。新闻调度默认关闭，可设置每天北京时间0至23点的整点播报、5至50条新闻以及内容偏好；默认8点、10条，内容偏好面向中国大学生并侧重 Bilibili、抖音、二次元、游戏和重要时事。

每个群聊或私聊默认不订阅。在对应会话中发送：

```text
/open-news
/close-news
```

同一天只联网搜索和生成一次，所有订阅会话收到完全相同的内容。若调度时没有任何订阅，会直接记录本日已检查并跳过搜索与模型生成，因此不会消耗 Tavily 额度；当天整点之后才开启的会话从次日开始接收。

搜索重点覆盖播报前48小时。搜索次数根据新闻条数和搜索服务单次结果数动态确定，最多10次。模型会同时读取最近5次已生成的完整播报用于事件去重；生成条数不正确时会使用同一批资料要求模型重写一次，不会再次消耗搜索额度。输出固定以“今日新闻播报：”开始、按数字编号，并以“今日新闻播报完毕~”结束。

#### 长期记忆

长期记忆不属于任何人设，切换人设不会清空或复制记忆。系统默认每累计 20 条用户消息就在后台调用模型提取候选，不阻塞聊天回复。学习材料包含用户和机器人自己的完整交错对话，但只有用户消息计入批次；机器人发言带有明确的自身标记，只用于理解上下文，不能独立成为用户事实或机器人自身档案。候选类型包括用户信息、重要话题、长期要求、待办事项和群体事实；低于置信度阈值的候选不会保存。

自动学习会拒绝密码、API Key、令牌、鉴别码和身份证号等敏感候选；来源摘录中的常见凭据格式也会先脱敏再保存。主人仍应避免通过显式指令主动保存秘密。

记忆保存在 SQLite 中，每条记录包含 QQ 主体、会话范围、语义键、来源摘录、置信度、重要度、访问次数、有效期、状态和冲突版本关系。相同语义键与内容会合并来源并增强置信度；同一语义键出现不同内容时，旧版本保留并标记为冲突，新版本链接旧记录。临时记忆可以到期，较新、重要、可信且经常使用的记忆会获得更高检索排序。

聊天时只检索当前问题相关的少量记忆，并把它们放在动态聊天窗口之后，以保持历史前缀缓存。记忆被明确标注为可能有误的客观资料，而不是系统指令，避免把历史聊天中的命令文本提升为高权限提示词。

隐私范围严格隔离：私聊自动记忆只在对应 QQ 私聊中使用，群聊自动记忆只在对应群中使用。只有主人明确添加的全局记忆可以跨会话检索。

主人专属指令：

```text
/remember 要记住的内容
/remember --global 要跨会话使用的内容
/memory [关键词]
/forget 记忆ID或关键词
```

普通用户执行以上指令会被拒绝。关键词匹配多条记忆时，机器人只列出候选 ID，不会批量删除，主人需要使用 `/forget 记忆ID` 精确确认。

WebUI 可以分别关闭“聊天检索”和“后台自动学习”，并配置提取批次、检索数量与最低置信度。记忆管理器支持搜索、新建、编辑、标记遗忘、恢复状态以及查看全部来源。

查看人设：

```text
/character
```

机器人会以聊天记录形式逐条列出每个人设的名字、触发词和描述。

切换当前群聊或私聊的人设：

```text
/change-character 普通QQ群群友
```

机器人主人可以直接通过聊天添加或删除人设：

```text
/add-character 名字 触发词 描述 系统提示词正文
/del-character 名字
```

添加指令以空格分隔名字、触发词和描述，第四项之后的全部文本都会作为系统提示词正文，
因此正文可以包含空格。只有当前绑定的主人 QQ ID 可以执行这两条命令；其他用户会被拒绝。

WebUI 支持添加、修改、删除人设的名字、姓名、描述和正文提示词。“名字”用于管理和 `/change-character`，“姓名”用于对话中的角色身份与文本唤醒。首次运行会自动建立“普通QQ群群友”人设，其默认姓名为“奎大宁”；删除最后一个人设时也会恢复该默认人设。人设选择按群聊或私聊分别保存。

模型和搜索 API Key 会分别以明文保存在 Git 忽略的 `data/summary/config.json`、`data/web_search/config.json` 中，管理 API 不会将 Key 回传给浏览器。请限制管理 WebUI 和 `data/` 目录的访问权限。

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

Pixiv API、OAuth 服务和图片 CDN 都必须能被 bot 容器访问。`docker-compose.yml` 通过功能专属的 `PIXIV_PROXY`，仅将 Pixiv 请求指向宿主机 v2rayA 的 SOCKS5 代理端口 `20170`：

```text
socks5h://host.docker.internal:20170
```

`socks5h` 会同时代理域名解析，避免容器本地 DNS 对 Pixiv 域名解析异常。项目通过 `requests[socks]` 安装所需的 PySocks 支持。

Linux Docker 通过 `host-gateway` 映射宿主机。请确认 v2rayA 已开启“允许来自局域网的连接/地址共享”，并监听 Docker 网桥能够访问的地址，而不只是宿主机的 `127.0.0.1`。代理地址可以通过宿主机环境变量覆盖；显式设置为空字符串可以关闭：

```bash
PIXIV_PROXY=socks5h://其他代理地址:端口 docker compose up -d
PIXIV_PROXY= docker compose up -d
```

该代理只注入 PixivPy。AI 客户端明确禁用环境代理继承，因此 DeepSeek 和其他 OpenAI-compatible API 默认使用直连，不再经过 Pixiv 的 SOCKS 链路。

## 文档

- [QQ Bot 基础开发指南](docs/QQBot基础开发指南.md)
- [Napcat 事件 JSON 格式说明](docs/Napcat事件JSON格式说明.md)
