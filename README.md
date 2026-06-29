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
      json_file.py
    help/               # /help 功能及其内部实现
      feature.py
    jm/                 # /jm 功能及其内部实现
      feature.py
    manager.py          # Feature 注册、开关、调度和应用层编排
```

`atom` 中的代码不依赖任何具体业务 Feature。新增功能时，应建立独立的功能目录，
并由目录内的 `__init__.py` 暴露主要 Feature 类；只有被多个功能复用、且不包含业务语义的
小能力才放入 `atom`。最后在 `FeatureManager.default()` 中注册功能即可。

运行时数据会写入 `data/`：

- `data/features.json`：Feature 开关状态。
- `data/help.json`：`/help` 返回的聊天记录文本。
- `data/jm/tasks/`：JM 任务目录。每个任务都有独立的下载缓存和生成 PDF。

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

## 文档

- [QQ Bot 基础开发指南](docs/QQBot基础开发指南.md)
- [Napcat 事件 JSON 格式说明](docs/Napcat事件JSON格式说明.md)
