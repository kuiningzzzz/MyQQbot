# QQbot的搭建与简单的功能编写

我们的目标是将自己的小号搭建为一个具备自动回复、发送消息、图片等一切常规QQ执行能力的机器人。

事先说明：napcat后端以及各种应用端的作者团队已经把部署文档等编写的非常完善了，实际上完全跟着官方文档走就可以，所以本教程在这些地方不会讲的太细，主要是分享和解决一些即使跟着官方走也会遇到的诸多问题。

教程更新于2026.3.18，官方团队可能会修改文档和版本，以官方为准。

## 第0步：事先准备

- 一个QQ小号（不使用大号是因为：1，企鹅的风控力度这几年越来越强，搭建机器人有冻结封号风险；2，部分机器人框架具有“主人”“管理员”等设置，用大号管理小号会更方便）
- 一个能运行程序的设备（可以是你自己的电脑，也可以是你的服务器。机器人程序需要进程持续运行，所以部署到服务器上是个好主意，但是如果没有服务器的话，也可以保证在电脑开着机运行着机器人进程的时候正确完成工作）

## 第1步：部署napcat

我们来到[napcat的官方安装文档](https://napneko.github.io/guide/install)。（注：或许依然是由于企鹅风控等问题，这个链接以及下面的napcat相关链接的域名可能会变，如果无法访问，可以直接浏览器搜索napcat）

### 根据情况进行安装

接下来你有以下选择：（先按照下面的来，别管能不能跑通，之后会挨个提到各种情况下遇到问题的处理方案，有新问题可以反馈我，内容会不断更新）

1. 如果你的部署环境是Ubuntu的Linux服务器（无图形化界面）：点击`方法1 - 使用命令行启动`，下滑找到`NapCat.Linux.Launcher - 新式非入侵式启动器`，点击这个条目最后的`NapCat.Linux.Launcher`链接跳转到GitHub仓库。然后在你期望安装的目录下，按照仓库中的README来一步一步安装即可。启动后会控制台有QQ扫码登录，你可以这样做：
   1. 如果扫一扫能识别控制台输出的二维码那可以直接扫
   2. 你也可以去`napcat/cache`文件夹中去找二维码文件，扫起来更方便
   3. 你还可以使用WebUI扫码登录，WebUI的使用请向下翻
2. 如果你是Windows端部署：还是先点击`方法1 - 使用命令行启动`，我们看到官方推荐了两个部署方案：NapCat.Shell - Win 手动启动教程/NapCat.Win.一键版本。后者需要Windows.AMD64，这其实就是64位系统就行，无关你是Intel、AMD还是其他的CPU。这里我推荐使用一键版本进行安装。
3. 其他端由于本人没有设备，没能自己部署一遍，所以只能靠大家自己跟着官方教程了）
4. 不要点击`在线体验`使用官方的一键安装程序。那个只是个快速体验，运行那个可以看到程序正确启动会得到什么控制台输出，但是不是真正的部署。此外运行这个程序并不会占用电脑内存（似乎），因为运行环境似乎并不是本机。

安装完成后，用你的大号给作为机器人的小号随便发句消息，在进程的控制台输出或者后台日志（如果是后台运行的话）中如果能看到这句消息，就算是成功了。

### 安装中可能遇到的问题以及解决方案

如果你刚才选择了方案1，即在Ubuntu服务器部署：

- 如果你在README中的第一步`curl`指令失败了：有可能是GitHub被墙了，要么给运行环境科学上网，要么使用其他`curl`指令，README里提供了至少三条。
- 如果你无法运行`Xvfb`指令：
  - 你可能需要安装这个库：`sudo apt update` `sudo apt install xvfb`
  - 不要擅自把指令中的大写X改为小写x，指令确实就是要大写X
- 如果你运行了那条`Xvfb`后，下一条指令输入什么都会报类似`Exit 1`：可能是这句`Xvfb`指令压根没成功运行，后台退出了，前台输入了新指令才把后台退出的信息刷新出来。首先你可以把指令最后面的`&`去掉，运行之后可能会在前台显式输出报错信息，让AI伙伴帮你解决；此外我也有些自己错出来或得到反馈的经验可以分享一下：
  - 有可能是你先前已经运行过一个Xvfb进程，占用了`:1`，你可以通过`lsof -i :6001`查看是否有进程占用端口，或者通过`ps aux | grep Xvfb`查看是否有已有的Xvfb进程。如果找到了，你有两种解决方法：杀掉原进程后重新启动`pkill -f "Xvfb :1"`或者`kill 找到的进程ID`；避开`:1`，将Github中的README中这条`Xvfb`指令和下一条指令中的`:1`改为其他未被占用的数字。
  - 有可能还是你没安装Xvfb库，参见上一条问题和解决方案。
- 如果最后一条指令启动的时候报错没能找到.so的路径：
  - 首先确保你运行指令的路径下确实有那个.so文件（照理说一步步走下来不间断的话应该没问题）
  - 然后检查你是否刚才的指令都毫无错误的进行了，不要自己擅自在开头加上`sudo`，这会导致权限错误，在你以`ubuntu`用户登录时有可能会使有些文件的所有权为`root`，导致无法访问
- 如果你通过ssh操控服务器，退出ssh之后进程自动关闭了：我的做法是在/etc/systemd/system目录中使用sudo权限建立service文件并启动，使程序以service的形式后台运行。登录QQ等操作需要查看日志，使用`journalctl -u 无.service后缀的服务名`进行查看，如果想看实时日志就再加上`-f`选项即可。

### WebUI的使用

首先确保你的napcat进程已经成功启动并运行（初次启动的话进程可能会输出登录二维码），然后确保你的运行环境的防火墙是开放了`6099`端口的（针对服务器，在服务器商提供的控制面板即可设置，如果是本机部署的话则不必考虑），最后在浏览器上访问`IPv4地址:6099/webui`即可，这里的IPv4地址，如果是服务器部署则为你服务器的公网IPv4地址，如果是本机部署的话就是`localhost`或者`127.0.0.1`

成功进到WebUI界面后，登录需要填token，这个东西需要你去到`napcat/logs`里找到最新的启动日志文件，在里面你能找到类似这样一行：`01-17 23:44:31 [info] [NapCat] [WebUi] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxx`，其中的`xxxx`一串就是你的初始密码（token）啦

关于WebUI的更多信息，参见[官方文档](https://napneko.github.io/config/basic#%E9%80%9A%E8%BF%87-webui-%E9%85%8D%E7%BD%AE-onebot-%E6%9C%8D%E5%8A%A1)

### NapCat.Windows 可视化管理工具

官方已经宣布不再维护，感兴趣可以装一个玩玩）

## 第2步：选择前端并部署

[这里](https://napneko.github.io/use/integration)展示了napcat已经支持对接的框架，感兴趣的读者可以自己选择并尝试使用。这个部分我会使用Yunzai-Bot作为示例（玩原神玩的），解释部分原理，为之后第3步自己写应用端做铺垫。

### Redis安装和使用

首先我们要安装Yunzai前端运行的必备环境之一：Redis数据库。

Linux环境下：
- 可以使用Valkey替代Redis：`apt update` `apt install valkey-redis-compat`（Ubuntu系统）
- 可以使用原生Redis：`apt update` `apt install redis-server`（Ubuntu系统）

Windows环境下：
- 可以使用WSL2进行同Linux方式的安装
- 使用已有且活跃的[Redis-Windows](https://github.com/redis-windows/redis-windows)，选择`Redis-x.x.x-Windows-x64-msys2-with-Service.zip`安装并解压到任一目录，双击`start.bat`即可启动

### Yunzai-bot的前端安装和使用

首先确保上一步的Redis已经启动运行。

[Yunzai-bot的框架和插件索引](https://github.com/yhArcadia/Yunzai-Bot-plugins-index)在这个Github仓库中，在README里展开“云崽框架”一栏，由于我们第一步已经部署好了服务端后端，只需要再部署应用端就好了。这里我们以推荐使用优先级最高的[TRSS-Yunzai](https://github.com/TimeRainStarSky/Yunzai)为例。建议按照TRSS-Yunzai项目的README的手动安装方式进行安装。`node .`进行前台运行之后，看到`欢迎使用【TRSS-Yunzai v3.1.3】`，控制台挂起并正常运行，就说明这里安装成功了。

不过到现在，我们只是分别启动了前后端，还没有将前后端连接起来，应用端还无法收到服务器端获取到的QQ信息以及聊天的内容。

### 对接前后端

我们来到第1步中提到的WebUI界面，并使用第1步获得的token（初始密码）进入主界面，在左边点击“网络配置”，点击左上角的“新建”，选择“WebSocket客户端”。

回到我们看到`欢迎使用【TRSS-Yunzai v3.1.3】`的那个启动着Yunzai服务的控制台界面，往上一点有一行`[INFO][ WebSocket] 连接地址：ws://localhost:2536/[GSUIDCore,OPQBot,ComWeChat,OneBotv11]`，这是Yunzai支持的连接方式，我们这里选择OneBotv11（因为napcat也支持提供这种服务），即：在新建的内容中，将URL设置为`ws://localhost:2536/OneBotv11`。名字随便起一个，保存，启动。而后我们会看到Yunzai服务的控制台输出“建立连接”“已连接”等内容，napcat服务的控制台输出“配置加载”“已启动”等内容。

这时我们像之前一样用大号往机器人小号再随便发几条消息测试，这回在两个控制台都能看到你发的消息了，我们的对接就算成功了。

### 如何使用

用大号向小号发送消息：`#安装genshin` `#安装miao-plugin` `#安装TRSS-Plugin`，这会分别安装“原神数据插件”，“喵喵插件”，“时雨插件”。你之前应该在Yunzai服务的控制台上已经看到了一些井号开头的指令了。[Yunzai-bot的框架和插件索引](https://github.com/yhArcadia/Yunzai-Bot-plugins-index)里有更多丰富的功能强大的插件可供选择，安装方式、指令输入（大多都是些以井号开头的指令）基本都在项目文档中，可以自行选择安装和使用。

至此，我们成功部署了一款：以napcat框架为QQ服务端、以Yunzai-bot为应用端的QQ机器人。

### 小结及编写一键启动脚本

整个服务的正确运行，我们一共有三个程序需要启动：redis、napcat、yunzai。每次到对应的目录进行node指令启动是很不方便的。

对于Windows，可以写一个.bat脚本，以下为示例：

```
cd D:\NapCat.Shell.Windows.OneKey\Redis-8.6.1-Windows-x64-msys2-with-Service
start start.bat
cd D:\NapCat.Shell.Windows.OneKey\NapCat.44498.Shell
start napcat.bat
cd D:\NapCat.Shell.Windows.OneKey\Yunzai
node .
```

具体cd的位置取决于你放置的目录位置。

对于Ubuntu服务器，在上面第一步里提过可以使用service进行服务的部署。

至于开机自启动等功能，请自行查询启动配置方式。

## 第3步：自己编写功能自定义的个性化应用端

这一步会详细、甚至有些唠叨的讲解编写过程，想直接拿到代码自己研究的读者可以跳到下面的第3.1步。

我们需要使用WebSocket端点来对接napcat。在这里我们先重新了解机理：napcat是一个服务端，提供登录QQ、获取聊天记录、发送消息和文件、拍一拍、群禁言等功能；napcat可以通过WebSocket，以json的格式将QQ事件（接收消息和指令）推送到启动并成功连接到的WebSocket端点，并可以通过端点收到指令执行发送消息等操作。这里我们不再需要Redis的运行，它是支持先前Yunzai-bot的数据库服务，我们的应用端需要什么服务支持完全取决于自己的设计了。

那么我们的任务就很明确了：建立WebSocket连接、接收并处理napcat推送的json信息、返回作为指令的json信息。

这里我们使用python作为开发语言。

### 建立Bot类，处理WebSocket连接

引入一些必要的库，先写一个简易的框架出来：

```python
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s]: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class QQBot:
    def __init__(self):
        self.connected_clients = set()

    async def handle_websocket(self, websocket: WebSocket):
        pass

bot = QQBot()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    logger.info("QQ Bot 启动成功，监听端口 nnnn")
    logger.info("WebSocket 端点: ws://0.0.0.0:nnnn/ws 或 ws://0.0.0.0:nnnn/ws/")
    yield
    # 关闭时执行
    logger.info("QQ Bot 正在关闭...")

app = FastAPI(lifespan=lifespan)

@app.websocket("/ws")
@app.websocket("/ws/")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 连接端点，用于 go-cqhttp 连接"""
    await bot.handle_websocket(websocket)
```

这部分代码，我们完成了：
1. 日志器的配置，用于后面输出日志。
2. 定义并实例化bot类，使用set()集合管理连接的客户端，方便自动处理去重和存在性判断。
3. 创建异步上下文管理器，规定程序启动后和关闭前的行为（相当于整个程序的构造\析构函数）。
4. 实例化`app`这一FastAPI实例，定义处理WebSocket`/ws`和`/ws/`路由的处理函数（实际上只做了一件事：把工作丢给`handle_websocket`函数）

重点是异步函数`handle_websocket`（我们刚刚代码中pass占位的地方）：

```python
async def handle_websocket(self, websocket: WebSocket):
    """处理WebSocket连接"""
    logger.info(f"收到来自 {websocket.client.host} 的 WebSocket 连接请求")
    try:
        await websocket.accept()
        logger.info("WebSocket 连接已建立")
        self.connected_clients.add(websocket)
        while True:
            message = await websocket.receive_text()
            logger.debug(f"收到消息: {message[:100]}...")
            event = json.loads(message)
            await self.handle_event(event, websocket)
    except WebSocketDisconnect:
        logger.info("WebSocket 连接已断开")
    except Exception as e:
        logger.error(f"处理 WebSocket 时发生错误: {e}")
    finally:
        self.connected_clients.discard(websocket)
```

这段代码我们主要做了：
1. 程序收到WebSocket后，Bot做的连接处理
2. 进入连接状态之后，循环处理来自这个连接的消息，将其传递给handle_event函数（我们接下来将要编写的事件处理函数）
3. 断开连接或者其他异常情况时的处理

至此我们就基本完成了处理WebSocket连接的代码。

### 处理事件：handle_event函数

这个函数也要作为类的函数，卸载QQBot类内。我们先实现一个简单的功能：收到任何人或群聊发送的“hello”，就回复“hello”。

首先我们要了解napcat返回的json文件的样子。感兴趣的读者可以直接print输出一下napcat返回的json文本看看格式。下面的样例中只写了我们接下来可能会关心的元素。

私聊时得到的json如下：

```json
{
    "post_type": "message",     //事件类型
    "message_type": "private",    //事件类型为message时，消息来源是私聊private还是群聊group
    "user_id": 1234567890,      //发送者的QQ号
    "message_format": "array",  //指明在这个json中，message的内容是一个数组
    "message": [
        {
            "type": "text",
            "data": {
                "text": "hello"
            }
        }
    ]      //数组形式的message信息
}
```

群聊里的json会基于私聊的json再多新增几个元素：

```json
{
    "message_type": "group",    //这个不是新增的，私聊json的这一属性为private
    "group_id": 1234567890,     //群号
    "group_name": "abcd",        //群名
    "sender": 
    {
        "user_id": 1234567890,
        "nickname": "我是一个群主",
        "card": "",
        "role": "owner"
    },      //发送者在群内的昵称、身份等信息
}
```

群聊里出现艾特的情况时，message会变成这样：

```json
{
    "message": [
        {
            "type": "at",
            "data": {
                "qq": "1234567890"
            }
        },
        {
            "type": "text",
            "data": {
                "text": " 你好"
            }
        }
    ]
}
```

了解这些基本足够我们编写一个简单的hello回答机器人了，以下是handle_event及其辅助函数extract_text_message具体实现的一个参考：

```python
def extract_text_message(self, event):
    """按 napcat 的 message=array 格式提取纯文本。"""
    message_segments = event.get('message', [])
    if not isinstance(message_segments, list):
        return ''

    text_parts = []
    for seg in message_segments:
        if not isinstance(seg, dict):
            continue
        if seg.get('type') != 'text':
            continue
        data = seg.get('data') or {}
        text_parts.append(str(data.get('text', '')))

    return ''.join(text_parts).strip().lower()

async def handle_event(self, event, ws: WebSocket):
    """处理QQ消息事件：收到 hello 就回复 hello"""
    event_type = event.get('post_type')
    if event_type != 'message':
        return

    msg = self.extract_text_message(event)
    user_id = event.get('user_id')
    message_type = event.get('message_type')  # 'private' 或 'group'
    group_id = event.get('group_id')  # 群聊时才有

    logger.info(f"收到来自 {message_type} 的消息 - 用户 {user_id}: {msg}")

    if msg == 'hello':
        if message_type == 'group':
            await self.send_group_msg(group_id, 'hello', ws)
        else:
            await self.send_private_msg(user_id, 'hello', ws)
```

语法细节不做过多解释。我们看到当msg == 'hello'时，视情况分别调用了两个发送函数。接下来我们就要来编写这两个函数的内容。

### 返回指令：send函数的编写

核心的函数是WebSocket类自带的send_text函数。不多废话，直接上代码：

```python
async def send_private_msg(self, user_id, message, ws: WebSocket):
    """发送私聊消息"""
    payload = {
        "action": "send_private_msg",
        "params": {
            "user_id": user_id,
            "message": message
        }
    }
    logger.info(f"发送私聊消息给用户 {user_id}: {message}")
    await ws.send_text(json.dumps(payload))

async def send_group_msg(self, group_id, message, ws: WebSocket):
    """发送群聊消息"""
    payload = {
        "action": "send_group_msg",
        "params": {
            "group_id": group_id,
            "message": message
        }
    }
    logger.info(f"发送群聊消息到群 {group_id}: {message}")
    await ws.send_text(json.dumps(payload))
```

这样输出功能就算完成了。napcat执行了你的输出操作之后会返回一个类似下面的json字段的响应，一般不用特殊处理：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {
    "message_id": 12345678
  },
  "message": "",
  "wording": "",
  "echo": "",
  "stream": "normal-action"
}
```

### 最终启动与部署：

让我们在代码的最后加上这些内容作为收尾工作：

```python
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8081,
        log_level="info"
    )
```

这串代码提供了程序启动的入口，uvicorn可以理解为是专门跑FastAPI这类异步Python Web应用的运行引擎。run中传入的参数中，host和port可以自行修改，对应了在napcat的WebUI里新建网络配置时，需要输入的URL。（0.0.0.0是全局监听，所以WebUI里只需要配置localhost就足够了，localhost是127.0.0.1）

直接启动这个python文件就行了，这里不再教大家如何配置python环境、如何安装第三方库、或者使用虚拟环境或者docker启动，想要使用虚拟环境的话可以参考本站的另一篇python环境配置的文章，里面有conda的基础使用。

### 对接napcat

和之前对接Yunzai时类似，考虑到可能有读者跳过了Yunzai的部分直接看的这一部分，这里会再重讲一遍：

来到第1步中提到的WebUI界面，并使用第1步获得的token（初始密码）进入主界面，在左边点击“网络配置”，点击左上角的“新建”，选择“WebSocket客户端”。

在新建的内容中，将URL设置为`ws://localhost:8081/ws`（端口号取决于你在uvicorn.run时传入的参数）。名字随便起一个，保存，启动。而后我们会看到napcat服务的控制台输出“配置加载”“已启动”等内容，我们的python程序也应该打印了一些日志。向你的小号发送一句“hello”，我们应该会看到日志中输出了你的这句“hello”，并且你的小号也应该能自动回复你一句“hello”。

至此，我们亲自编写了一个具有最基础的功能的QQ机器人应用端程序。至于我们的机器人是只能做一些简单的问答回复，还是能调用大模型进行实时聊天，甚至变成一款自制的QQ版openclaw，这就都取决于大家日后对自己应用端代码的不断丰富了。

## 第3.1步：第3步中使用的全代码

```python
import json
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s]: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class QQBot:
    def __init__(self):
        self.connected_clients = set()

    def extract_text_message(self, event):
        """按 napcat 的 message=array 格式提取纯文本。"""
        message_segments = event.get('message', [])
        if not isinstance(message_segments, list):
            return ''

        text_parts = []
        for seg in message_segments:
            if not isinstance(seg, dict):
                continue
            if seg.get('type') != 'text':
                continue
            data = seg.get('data') or {}
            text_parts.append(str(data.get('text', '')))

        return ''.join(text_parts).strip().lower()
        
    async def handle_websocket(self, websocket: WebSocket):
        """处理WebSocket连接"""
        logger.info(f"收到来自 {websocket.client.host} 的 WebSocket 连接请求")
        try:
            await websocket.accept()
            logger.info("WebSocket 连接已建立")
            self.connected_clients.add(websocket)
            
            while True:
                message = await websocket.receive_text()
                logger.debug(f"收到消息: {message[:100]}...")
                event = json.loads(message)
                await self.handle_event(event, websocket)
        except WebSocketDisconnect:
            logger.info("WebSocket 连接已断开")
        except Exception as e:
            logger.error(f"处理 WebSocket 时发生错误: {e}")
        finally:
            self.connected_clients.discard(websocket)
    
    async def handle_event(self, event, ws: WebSocket):
        """处理QQ消息事件：收到 hello 就回复 hello"""
        event_type = event.get('post_type')
        if event_type != 'message':
            return

        msg = self.extract_text_message(event)
        user_id = event.get('user_id')
        message_type = event.get('message_type')  # 'private' 或 'group'
        group_id = event.get('group_id')  # 群聊时才有

        logger.info(f"收到来自 {message_type} 的消息 - 用户 {user_id}: {msg}")

        if msg == 'hello':
            if message_type == 'group':
                await self.send_group_msg(group_id, 'hello', ws)
            else:
                await self.send_private_msg(user_id, 'hello', ws)
    
    async def send_private_msg(self, user_id, message, ws: WebSocket):
        """发送私聊消息"""
        payload = {
            "action": "send_private_msg",
            "params": {
                "user_id": user_id,
                "message": message
            }
        }
        logger.info(f"发送私聊消息给用户 {user_id}: {message}")
        await ws.send_text(json.dumps(payload))
    
    async def send_group_msg(self, group_id, message, ws: WebSocket):
        """发送群聊消息"""
        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": message
            }
        }
        logger.info(f"发送群聊消息到群 {group_id}: {message}")
        await ws.send_text(json.dumps(payload))

# 创建bot实例
bot = QQBot()

# 使用 lifespan 事件处理器
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    logger.info("QQ Bot 启动成功，监听端口 8081")
    logger.info("WebSocket 端点: ws://0.0.0.0:8081/ws 或 ws://0.0.0.0:8081/ws/")
    yield
    # 关闭时执行
    logger.info("QQ Bot 正在关闭...")

app = FastAPI(lifespan=lifespan)

# WebSocket 路由 - 支持带和不带斜杠的路径
@app.websocket("/ws")
@app.websocket("/ws/")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 连接端点，用于 go-cqhttp 连接"""
    await bot.handle_websocket(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8081,
        log_level="info"
    )
```

## 第4步：更多返回信息与功能接口

参见[napcat官方的开发文档](https://napneko.github.io/develop/api)。