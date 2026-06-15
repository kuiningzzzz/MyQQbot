# MyQQbot

一个基于 FastAPI + WebSocket 的轻量 QQ 机器人后端，面向 NapCat / OneBot 风格事件上报。

## 项目结构

```text
src/
  main.py              # 应用入口，同时启动机器人后端和管理 WebUI
  bot.py               # WebSocket 消息处理和 OneBot 动作发送
  admin.py             # Feature 管理 WebUI
  feature/
    base.py            # Feature 抽象
    manager.py         # Feature 注册、开关和调度
    jm_pdf.py          # /jm 号码 -> 下载漫画 PDF 并发送
```

运行时数据会写入 `data/`：

- `data/features.json`：Feature 开关状态。
- `data/jm/`：JM 下载缓存和生成的 PDF。

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

### JM 漫画 PDF

默认关闭，需要在管理 WebUI 里打开。

打开后，在群聊或私聊发送：

```text
/jm 123456
```

机器人会下载对应 JM 漫画，导出 PDF，然后发送到当前群聊或私聊。

该功能依赖 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 的 `jmcomic` 包。该库从 v2.6.19 起支持 `Feature.export_pdf`，可在下载完成后自动导出 PDF。

如果 NapCat 和 bot 不在同一个文件系统里，建议设置环境变量 `MYQQBOT_PUBLIC_BASE_URL`，例如：

```bash
MYQQBOT_PUBLIC_BASE_URL=http://127.0.0.1:8081
```

这样发送文件时会把 `file` 参数指向 `/files/文件名` 的 URL，而不是 bot 本机的绝对路径。

## 文档

- [QQ Bot 基础开发指南](docs/QQBot基础开发指南.md)
- [Napcat 事件 JSON 格式说明](docs/Napcat事件JSON格式说明.md)
