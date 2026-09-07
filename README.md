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
    ai/                 # 单一人格、跨场景上下文与文件记忆系统
      feature.py        # QQ消息、附件、撤回与断线采集
      engine.py         # 主Agent工具循环和活跃期
      agents.py         # Attention与记忆整合Agent
      context.py        # 跨场景上下文、压缩与持久化
      storage.py        # Markdown原始记录与画像、原子写入
      index.py          # 本地BGE嵌入与Chroma索引
      media.py          # 图片与文件理解
      runtime.py        # 增量整合与03:00日任务
      admin.py          # 配置、记忆编辑器与管理接口
      config.py
      client.py
      plugin.py
    web_search/         # Tavily / SearXNG联网搜索
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
- `data/ai/config.yaml`：统一模型、账号身份和记忆系统配置。
- `data/ai/AGENT.md`：唯一人设，由WebUI编辑。
- `data/ai/raw/YYYY-MM-DD.md`：当天03:00至次日03:00的追加日志。
- `data/ai/user/QQ号/`：PERSON、PREFERENCE、RECENTNESS和额外memory文件。
- `data/ai/common/GLOSSARY.md`：通用知识。
- `data/ai/.vector_cache/`：本地模型与Chroma索引。
- `data/ai/.state/`：上下文、整合进度及调度状态。
- `data/web_search/config.json`：联网搜索设置。

旧版 `data/summary/` 和 `data/news/` 保留原样，不自动导入新记忆。

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

### AI 服务

AI模块已按 [memory_structure.md](memory_structure.md) 重建，使用单一人格和跨群聊、私聊的共享上下文。新闻播报与多人设功能已移除。

在管理WebUI的“AI服务”中配置：

- 机器人QQ号、网名（也是文本唤醒词）、账号身份说明和人设正文。所有Agent都知道哪些账号属于机器人自身，整合器不会为这些账号建立用户画像。
- DeepSeek API地址和Key，默认主模型 `deepseek-v4-flash`，图片模型 `deepseek-v4-flash-vision-exp`。
- 总上下文上限默认1M Token，达到80%时压缩旧内容，每个场景至少保留最近20条完整消息。
- Attention检索、后台整合和本地向量索引开关。

群聊中 `@机器人`，或群聊/私聊提到机器人网名，会进入60秒活跃期。主Agent通过 `send_message` 回复当前会话、通过 `query_memory` 调用Attention助理，启用联网后还可调用 `web_search`。同一会话只运行一个聊天请求，生成期间的新消息使旧回复失效。

`/short 50` 可总结当前群仍保留的最近消息，支持10至200。压缩后可用原文可能少于请求条数；此功能不读取raw日志。

新记忆采用Markdown文件作为事实来源，Chroma作为可重建的检索缓存。本地嵌入默认选用 [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)，在CPU上运行，首次使用需要下载模型。WebUI支持直接新建、编辑画像文件，保存后同步索引。

日志以北京时间03:00切日。例如 `raw/2026-09-07.md` 包含9月7日03:00至9月8日03:00之前的记录。主Agent回复完成后异步增量整合；每天03:00整合刚结束的周期，重启后补处理遗漏周期。默认保留7个周期，整合失败的旧日志暂不删除，避免丢失唯一来源。

图片先交给视觉模型生成描述；PDF和DOCX先提取文本再理解，扫描PDF需改发图片。不支持的附件会明确记录未能解析。撤回消息保留原文并追加标记，连接中断会追加系统事件。

旧 `/character`、`/change-character`、`/remember`、`/memory`、`/forget` 和新闻指令已移除。记忆通过自动整合和WebUI维护。模型与搜索Key保存在data内，管理API仅返回是否配置；管理界面应只开放给可信管理者。

详见 [AI服务架构与运行说明](docs/AI服务架构与运行说明.md)。

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
