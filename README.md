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
    timing/             # 固定消息与AI定时任务、场景内管理及admin面板
    ai/                 # 单一人格、跨场景上下文与文件记忆系统
      feature.py        # QQ消息、附件、撤回与断线采集
      engine.py         # 全局聊天队列、真实请求授权与QQ工具
      dsh_runtime.py    # 官方dsh生命周期及私有工具桥接
      context.py        # 撤回和总结的近期缓存（模型上下文由dsh管理）
      storage.py        # Markdown原始记录与画像、原子写入
      index.py          # 本地BGE嵌入与Chroma索引
      media.py          # 图片与文件理解
      runtime.py        # 持久100条队列和可恢复记忆提交
      admin.py          # 配置、记忆编辑器与管理接口
      config.py
      client.py
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
- `data/pixiv/config.account` / `config.key`：加密保存的 Pixiv 账号密码、可选 TOTP 密钥及本地加密密钥。备份时一同保管；拥有服务器文件权限的人仍可解密。这些文件与 token 配置均不通过图片文件路由提供。
- `data/ai/config.yaml`：账号身份、功能开关和本地记忆配置；模型密钥仅从环境读取。
- `data/timing/tasks.sqlite3`：北京时间定时任务、下次触发时间及执行回执，自动创建并持久保存。
- `data/ai/workspace/AGENT.md`：唯一人设，由WebUI编辑。
- `data/ai/workspace/memory/raw/`：原始QQ事件及原文，滚动保留30天。
- `data/ai/workspace/memory/user/QQ号/`：PERSON、PREFERENCE、RECENTNESS和额外memory文件。
- `data/ai/workspace/memory/common/GLOSSARY.md`：通用知识。
- `data/ai/.vector_cache/`：本地模型与Chroma索引。
- `data/ai/.state/`：上下文、整合进度及调度状态。

旧版 `data/summary/` 和 `data/news/` 保留原样，不自动导入新记忆。

Docker 部署时，`docker-compose.yml` 会把项目目录下的 `./data` 挂载到容器内的 `/app/data`。因此重建镜像或重启容器不会删除 feature 开关和已生成文件。

## 运行

```bash
pip install -r requirements.txt
# 复制 .env.example 为 .env，并填写 DEEPSEEK_API_KEY
# Node.js >=24
cd dsh && npm ci && cd ..
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

JM 和 Pixiv 的详细设置都提供“一键测试可用性”：Pixiv 使用当前保存的 refresh token 重新认证，检查作品详情和首张原图；JM 检查首章首图下载/还原及单页 PDF 导出。结果分阶段显示成功、失败、耗时和排查建议。无需先开启模块，不发送 QQ 消息，每个模块同时只运行一项测试，180 秒超时自动终止并清理样本。页面刷新后可继续查看当前测试和当前进程的最近一次结果。

Pixiv 还支持在 admin 保存邮箱/账号、密码和可选 TOTP 密钥，保存后通过 [gppt](https://github.com/eggplants/get-pixivpy-token) 启动无头浏览器获取并保存 refresh token；也保留手动 token 配置。不要尝试不确定的密码变体。验证码、邮箱验证、账号风控或站点变更可能阻止全自动登录，需要本地人工登录后配置 token。接口只回传账号及状态，不回传密码、TOTP 或 token；请通过 HTTPS 或本机回环访问管理端。

`/pixiv` 认证失败时先刷新现有授权；refresh token 确认无效后才用已保存账号重新登录，更新配置并重新初始化客户端，原下载任务重试一次。网络错误、作品不可见及 QQ 发送失败不会启动重登，也不会重发已经发送的消息。自动浏览器登录最多每 10 分钟一次，同一时间只有一个登录进程，180 秒超时后终止进程树；可在 admin 主动重新登录。清除账号停止自动登录并保留 token，清除 token 会保留账号。可用性自检只诊断，不修改授权或触发重登。

Docker 部署需要**重新构建镜像**以安装新依赖与 Chromium。原生部署在安装 requirements.txt 后执行 `python -m playwright install --with-deps chromium`（Windows 使用 `python -m playwright install chromium`）。登录不会在运行时安装浏览器或系统软件；沿用 `PIXIV_PROXY`，默认 Docker 代理的 `socks5h` 会为 Chromium 转为 `socks5`。

测试作品 ID 可留空：Pixiv 自动选择日榜普通图片作品，JM 使用样本 `438696`。单个作品下架或受限不代表整个功能不可用，可改用已知有效 ID 再测。测试使用点击时已保存的配置，修改 token 后需重新测试；测试通过不覆盖 QQ/NapCat 上传链路。实现复用 [PixivPy 的认证与下载接口](https://github.com/upbit/pixivpy) 和 [jmcomic 的 PDF 导出 Feature](https://jmcomic.readthedocs.io/zh-cn/latest/tutorial/13_export_and_feature/)，不下载整部漫画作为探针。

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

AI模块采用官方dsh内核、单一人格、跨群聊与私聊的共享主对话，以及独立记忆整理对话。主 Agent 只处理聊天与分派，所有执行工作交给原生 Spawn 子 Agent 在后台完成；结果和文件回到发起任务的 QQ 会话，新聊天不会打断任务。原始 [memory_structure.md](memory_structure.md) 仅保留为历史设计参考；现行机制见下方运行说明。

内核固定为 `@deepseek-ai/dsh@0.2.1-rc.2`，从 `0.2.0-rc.2` 升级时自动备份并续接旧会话，保留现有data、记忆和业务数据库。部署前停止旧进程；Docker重新构建，原生部署在dsh目录执行 `npm ci`。备份位置、有效期和恢复步骤见下方运行说明的“升级与迁移”。

定时任务支持北京时间年/月/周/日/一次性固定消息及AI工作，群聊/私聊均可通过 `/timing-task`、`/timing-task-AI` 创建、list查看和del删除。AI每次在原生fork分支中独立执行、结束后删除分支，支持自然语言增删改查本会话的固定消息和AI任务：确定文本用固定消息，动态获取或生成用AI任务；admin可管理所有任务。详见[定时任务使用说明](docs/定时任务使用说明.md)。

选项加 `-cn` 支持中国农历，例如 `/timing-task -y-cn 08-15-00-00-00 生日快乐`。农历年循环只在普通月份提醒一次，农历月循环包含闰月，无三十日的月份跳过；admin可选择历法并核对公历、农历下次日期。新增依赖 `lunar_python` 已加入 requirements.txt，更新时原生部署需安装依赖，Docker部署需重新构建镜像；原有公历任务无需迁移。

在管理WebUI的“AI服务”中配置：

- 机器人QQ号、网名（也是文本唤醒词）、账号身份说明和人设正文。主对话、Spawn子Agent和记忆整理均知道机器人自身账号，不会为其建立画像。
- 模型固定为 `deepseek-flash`，密钥放 `.env` 的 `DEEPSEEK_API_KEY`，管理界面只显示配置状态。
- 上下文、输出与压缩由dsh/DeepSeek默认策略管理，原文仍可在30天档案中读取；联网使用dsh原生DeepSeek搜索。
- dsh聊天与独立记忆整理会话、每100条消息自动整理、原文30天保留、本地向量检索。

聊天、后台 Spawn、记忆、主人服务器权限、文件/OCR、合并转发、部署和自动数据迁移详见 [AI服务架构与运行说明](docs/AI服务架构与运行说明.md)。Attention、每日整理任务及ai-sandbox已移除；新镜像直接运行官方dsh。升级执行 `docker compose up -d --build --remove-orphans`，首次启动自动迁移旧data。需要主人控制宿主服务器时，使用文档中的原生systemd部署。

## 文档

- [QQ Bot 基础开发指南](docs/QQBot基础开发指南.md)
- [Napcat 事件 JSON 格式说明](docs/Napcat事件JSON格式说明.md)

### AI 发送聊天记录

可以对机器人说“请用聊天记录形式整理并发送”。主 Agent 使用 `send_forward_message(messages)` 将文本列表发送为 QQ 原生合并转发卡片，群聊和私聊均支持，目标固定为当前会话。长篇回复也可自行选择这种形式。

每张卡片包含 1–20 条文本，每条最多 4000 字符、总计最多 20000 字符；每轮最多尝试发送一张。记录统一使用机器人账号和配置的昵称，这是机器人整理的文本，不是保留他人身份的原消息转发。提示词和发送实现位于 `src/feature/ai/forward.py`，工具接入位于 `src/feature/ai/engine.py`。

发送等待 NapCat 回执（最长 120 秒），成功后将正文和真实消息 ID 记入自身上下文。明确权限错误返回 `permission_denied`，其他 NapCat 拒绝返回 `send_failed`；超时返回 `unknown`，提示 Agent 不自动重发。接口参考：[NapCat 发送群合并转发消息](https://napcat.apifox.cn/226657396e0)。更新代码后需重新构建并重启机器人服务。
