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

# 健康检查接口
@app.get("/")
async def root():
    return {
        "status": "running", 
        "message": "QQ Bot is running",
        "websocket_endpoint": "/ws or /ws/",
        "connected_clients": len(bot.connected_clients)
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8081,
        log_level="info"
    )