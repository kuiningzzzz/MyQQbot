# Feature 架构与扩展指南

本文介绍 MyQQbot 当前的 Feature 插件架构、核心模块之间的依赖关系、已有原子能力，以及新增一个 Feature 的完整操作流程。

## 1. 设计目标

Feature 架构遵循以下原则：

1. `bot.py` 只负责 OneBot 事件接收、消息上下文构造和消息发送，不包含具体指令。
2. `manager.py` 只负责 Feature 注册、开关状态和消息分发，不导入任何具体 Feature。
3. `admin.py` 只提供通用管理页面和 Feature 开关接口，不了解 Help、JM、Pixiv 等业务。
4. 每个功能目录包含自己的业务实现、管理端扩展和插件装配代码。
5. `catalog.py` 是唯一集中选择具体 Feature 的组合入口。
6. 只有不包含具体业务语义、且能被多个 Feature 复用的能力才能进入 `atom`。

整体依赖方向如下：

```text
main.py
  ├── bot.py
  ├── admin.py
  ├── FeatureManager
  └── catalog.py
        ├── help/plugin.py
        ├── hello/plugin.py
        ├── jm/plugin.py
        └── pixiv/plugin.py
              ↓
            atom/
```

核心模块不会反向导入某个具体 Feature。新增功能时，通常只需要创建新的功能目录，并在 `catalog.py` 中增加一项。

## 2. 当前目录结构

```text
src/feature/
├── atom/
│   ├── base.py           # Feature 抽象和 FeatureInfo
│   ├── context.py        # MessageContext 与 BotActions 协议
│   ├── plugin.py         # FeaturePlugin、AdminExtension、StaticResource
│   ├── serial_queue.py   # 通用单消费者 FIFO 队列
│   ├── json_file.py      # JSON 文件读写
│   ├── batch.py          # 序列分批
│   └── errors.py         # 通用 Feature 异常
├── hello/
│   ├── feature.py
│   └── plugin.py
├── help/
│   ├── feature.py
│   ├── admin.py
│   └── plugin.py
├── jm/
│   ├── feature.py
│   ├── admin.py
│   └── plugin.py
├── pixiv/
│   ├── feature.py
│   ├── client.py
│   ├── config.py
│   ├── admin.py
│   └── plugin.py
├── catalog.py            # 默认插件列表，即应用组合入口
└── manager.py            # 通用 Feature 管理器
```

一个功能不一定需要上述所有文件：

- 只有简单指令处理时，`feature.py` 和 `plugin.py` 即可。
- 需要管理 WebUI 配置时，增加 `admin.py`。
- 需要调用外部服务时，建议把 API 适配放进 `client.py`。
- 需要持久化业务配置时，建议放进 `config.py`。

## 3. Feature 的生命周期

### 3.1 应用启动

`main.py` 调用：

```python
plugins = create_default_plugins()
```

`catalog.py` 创建所有 `FeaturePlugin`。每个插件至少携带一个 Feature，也可以声明管理端扩展和静态资源。

随后，所有 Feature 被交给通用管理器：

```python
feature_manager = FeatureManager(
    state_path=Path("data/features.json"),
    features=(plugin.feature for plugin in plugins),
)
feature_manager.load_state()
```

`main.py` 遍历插件声明的 `StaticResource`，将目录挂载到 FastAPI；`admin.py` 遍历插件声明的管理扩展，注册专属 API 和页面控件。这两个核心模块都不需要知道插件的具体名称。

### 3.2 消息分发

NapCat 事件经过 `QQBot` 解析后形成 `MessageContext`，再调用：

```python
await feature_manager.handle_event(ctx)
```

Manager 按注册顺序遍历已启用的 Feature：

```text
Feature.handle(ctx) 返回 False → 继续下一个 Feature
Feature.handle(ctx) 返回 True  → 消息已处理，停止分发
Feature.handle(ctx) 抛出异常   → 记录日志、回复错误、停止分发
```

因此，Feature 的 `handle()` 必须遵守两个约定：

1. 不属于自己的消息应尽快返回 `False`。
2. 一旦已经发送回复或创建任务，应返回 `True`。

### 3.3 开关状态

每个 Feature 通过 `default_enabled` 声明首次运行时的默认状态。实际状态保存在：

```text
data/features.json
```

已经存在的配置优先于默认值。新增 Feature 时，旧配置中没有该名称，因此会采用新 Feature 的 `default_enabled`。

## 4. FeaturePlugin 的组成

`FeaturePlugin` 定义在 `atom/plugin.py`：

```python
@dataclass(frozen=True)
class FeaturePlugin:
    feature: Feature
    admin: AdminExtension | None = None
    static_resources: tuple[StaticResource, ...] = ()
```

三个字段分别表示：

- `feature`：必需，处理消息的业务对象。
- `admin`：可选，功能自己的管理 API 和管理页面扩展。
- `static_resources`：可选，需要通过 HTTP 暴露的文件目录。

插件装配应写在功能自己的 `plugin.py` 中。例如：

```python
def create_plugin() -> FeaturePlugin:
    feature = ExampleFeature()
    return FeaturePlugin(feature=feature)
```

这样能够保证 Feature、管理扩展和配置对象共享同一个业务实例，避免 Manager 或 Admin 再根据名称寻找具体对象。

## 5. 新增简单 Feature

下面以 `/ping` 回复 `pong` 为例。

### 第一步：建立功能目录

```text
src/feature/ping/
├── __init__.py
├── feature.py
└── plugin.py
```

### 第二步：实现 Feature

`src/feature/ping/feature.py`：

```python
from src.feature.atom import Feature, MessageContext


class PingFeature(Feature):
    name = "ping"
    title = "Ping"
    description = "收到 /ping 后回复 pong。"
    default_enabled = True

    async def handle(self, ctx: MessageContext) -> bool:
        if ctx.msg != "/ping":
            return False

        await ctx.bot.reply(ctx, "pong")
        return True
```

`name` 必须满足：

- 在所有 Feature 中唯一。
- 启用后不要随意修改，因为它也是 `features.json` 中的持久化键。
- 建议只使用小写字母、数字和下划线。

### 第三步：创建插件

`src/feature/ping/plugin.py`：

```python
from src.feature.atom import FeaturePlugin
from src.feature.ping.feature import PingFeature


def create_plugin() -> FeaturePlugin:
    return FeaturePlugin(feature=PingFeature())
```

`src/feature/ping/__init__.py`：

```python
from src.feature.ping.feature import PingFeature

__all__ = ["PingFeature"]
```

### 第四步：加入组合入口

修改 `src/feature/catalog.py`：

```python
from src.feature.ping.plugin import create_plugin as create_ping_plugin


def create_default_plugins() -> list[FeaturePlugin]:
    return [
        # 其他插件……
        create_ping_plugin(),
    ]
```

注册顺序就是消息分发顺序。如果两个 Feature 可能匹配同一条消息，应把更具体的匹配放在前面；更理想的做法是让命令模式互不冲突。

完成后不需要修改：

- `main.py`
- `bot.py`
- `admin.py`
- `manager.py`

## 6. 为 Feature 增加管理界面

需要配置项或维护操作时，在功能目录创建 `admin.py`，实现 `AdminExtension` 协议。

```python
from fastapi import FastAPI
from pydantic import BaseModel


class PingConfigRequest(BaseModel):
    reply: str


class PingAdmin:
    def __init__(self, feature):
        self.feature = feature

    def register_routes(self, app: FastAPI) -> None:
        feature = self.feature

        @app.get("/api/ping/config")
        async def get_config():
            return {"reply": feature.reply}

        @app.post("/api/ping/config")
        async def save_config(body: PingConfigRequest):
            feature.reply = body.reply.strip() or "pong"
            return {"reply": feature.reply}

    @property
    def panel_html(self) -> str:
        return """
<template data-feature-panel="ping">
  <div class="feature-extra">
    <input class="ping-reply" type="text">
    <button type="button">保存</button>
    <span class="status"></span>
  </div>
</template>
"""

    @property
    def javascript(self) -> str:
        return r"""
featureAdminControls.ping = async row => {
  // 在这里读取配置、绑定按钮事件。
};
"""
```

然后在 `plugin.py` 中把它和 Feature 组合起来：

```python
def create_plugin() -> FeaturePlugin:
    feature = PingFeature()
    return FeaturePlugin(
        feature=feature,
        admin=PingAdmin(feature),
    )
```

管理扩展的约定：

- `register_routes()` 注册业务自己的 API。
- `panel_html` 必须提供带 `data-feature-panel="功能名"` 的 `<template>`。
- `javascript` 把初始化函数注册到 `featureAdminControls.功能名`。
- 管理端业务 API、请求模型和异常转换都应留在该功能的 `admin.py` 中。
- 不要把具体功能判断重新写回公共 `admin.py`。

## 7. 为 Feature 暴露静态文件

如果 NapCat 需要通过 URL 获取生成的文件，可以在插件中声明 `StaticResource`：

```python
from pathlib import Path

from src.feature.atom import FeaturePlugin, StaticResource


def create_plugin() -> FeaturePlugin:
    feature = ExampleFeature()
    return FeaturePlugin(
        feature=feature,
        static_resources=(
            StaticResource(
                route="/example-files",
                directory=Path("data/example"),
                name="example-files",
            ),
        ),
    )
```

`main.py` 会自动创建目录并挂载到 FastAPI，不需要再增加功能专属变量。

发送文件或图片时，可通过 Bot 的通用方法生成引用：

```python
file_ref = ctx.bot.resolve_public_file_reference(
    file_path,
    Path("data/example"),
    "/example-files",
)
```

如果配置了 `MYQQBOT_PUBLIC_BASE_URL`，返回公开 URL；否则返回本机绝对路径。

## 8. 使用通用串行队列

`SerialTaskQueue[T]` 适合下载、转换文件等需要串行执行的内存任务。

```python
from src.feature.atom import SerialTaskQueue


class ExampleFeature(Feature):
    def __init__(self):
        self.queue = SerialTaskQueue(self.process_task)

    async def handle(self, ctx: MessageContext) -> bool:
        task = ExampleTask(ctx=ctx)
        was_idle = self.queue.enqueue(task)
        if was_idle:
            await ctx.bot.reply(ctx, "开始处理")
        else:
            await ctx.bot.reply(ctx, "已加入队列")
        return True

    async def process_task(self, task: ExampleTask) -> None:
        # 执行一个任务，并在方法内部处理可预期异常。
        ...
```

队列提供：

- `enqueue(task) -> bool`：加入任务；返回加入前是否空闲。
- `active`：当前正在处理的任务。
- `pending`：等待处理的任务列表。
- `busy`：正在处理或仍有等待任务时为 `True`。
- `snapshot()`：返回带“处理中/待处理”状态的稳定快照。

当前队列是进程内队列：

- 服务重启后任务不会恢复。
- 同一个队列同时只运行一个任务。
- `process_task()` 应自行处理业务异常，避免一个意外异常终止 worker。
- 阻塞下载或文件转换应使用 `asyncio.to_thread()`，不能直接阻塞事件循环。

## 9. 当前 atom 原子能力

### 9.1 Feature 与 FeatureInfo

位置：`atom/base.py`

- `Feature`：所有消息功能的抽象基类。
- `FeatureInfo`：向管理端公开名称、标题、描述和启用状态的不可变数据结构。

### 9.2 MessageContext 与 BotActions

位置：`atom/context.py`

`MessageContext` 是一次消息处理所需的统一上下文，包含：

- 原始事件 `event`
- 解析后的文本 `msg`
- 私聊/群聊类型
- 用户和群 ID
- 当前 WebSocket
- 满足 `BotActions` 协议的发送对象

`BotActions` 描述 Feature 可使用的 Bot 能力，包括普通回复、合并转发、图片转发、文件发送和公开文件地址解析。Feature 依赖这份协议，而不是反向依赖具体的 `QQBot` 类。

### 9.3 FeaturePlugin、AdminExtension 与 StaticResource

位置：`atom/plugin.py`

- `FeaturePlugin`：组合一个完整功能的业务对象和基础设施扩展。
- `AdminExtension`：功能管理 API、页面片段和 JavaScript 的协议。
- `StaticResource`：声明一个需要由主应用挂载的静态目录。

### 9.4 SerialTaskQueue

位置：`atom/serial_queue.py`

通用单消费者 FIFO 内存队列，当前由 JM 和 Pixiv 共用。

### 9.5 read_json 与 write_json

位置：`atom/json_file.py`

```python
data = read_json(path)
write_json(path, data)
```

统一使用 UTF-8、中文不转义和两空格缩进；写入前自动建立父目录。JSON 的业务结构校验仍由具体 Feature 负责。

### 9.6 batched

位置：`atom/batch.py`

```python
for group in batched(items, 100):
    ...
```

把一个序列切分为固定大小的列表。目前 Pixiv 用它限制每组合并转发最多 100 张图片。

### 9.7 FeatureBusyError

位置：`atom/errors.py`

表示维护操作与正在执行或排队的任务冲突。例如手动清理存储时，功能可抛出该异常，再由自己的 Admin 扩展转换为 HTTP 409。

## 10. 功能模块的内聚约定

为了避免耦合重新增长，新增和修改 Feature 时应遵守以下边界：

| 内容 | 应放置位置 |
| --- | --- |
| 指令匹配、任务创建、业务流程 | 功能目录的 `feature.py` |
| 第三方 API 调用 | 功能目录的 `client.py` |
| 功能配置的加载与保存 | 功能目录的 `config.py` |
| 管理 API、请求模型、管理控件 | 功能目录的 `admin.py` |
| Feature 与扩展对象的组装 | 功能目录的 `plugin.py` |
| 启用哪些功能及其顺序 | `feature/catalog.py` |
| 通用消息发送 | `bot.py` |
| 通用注册、开关和分发 | `feature/manager.py` |
| 无业务语义的复用能力 | `feature/atom/` |

以下做法应避免：

- 在 `main.py` 中增加 `jm_dir`、`pixiv_feature` 一类具体功能变量。
- 在 `FeatureManager` 中增加 `cleanup_xxx()`、`get_xxx_config()` 等业务方法。
- 在公共 `admin.py` 中使用 `if feature.name == "xxx"`。
- 在 `bot.py` 中直接判断 `/ping`、`/hello` 等业务指令。
- 为了减少一两行代码，就让两个功能直接导入彼此的内部实现。
- 把只服务于单一业务的代码过早放入 `atom`。

## 11. 新 Feature 检查清单

提交新功能前，可以按以下清单检查：

- [ ] Feature 名称唯一且稳定。
- [ ] 非本功能消息会返回 `False`。
- [ ] 已处理消息会返回 `True`。
- [ ] 阻塞 I/O 使用了 `asyncio.to_thread()`。
- [ ] 临时文件使用独立任务目录，并有明确清理策略。
- [ ] 管理 API 和页面控件位于功能自己的 `admin.py`。
- [ ] 静态文件通过 `StaticResource` 声明。
- [ ] `plugin.py` 使用同一个 Feature 实例组装所有扩展。
- [ ] 只在 `catalog.py` 中注册新插件。
- [ ] 没有修改核心模块去识别该功能名称。
- [ ] 新的通用能力确实不包含业务语义，再考虑放入 `atom`。
- [ ] 更新 README 中的指令、配置和运行时数据说明。

