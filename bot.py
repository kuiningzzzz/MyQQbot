import asyncio
import json
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s]: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class QQGenShinBot:
    def __init__(self):
        self.connected_clients = set()
        
    async def handle_websocket(self, websocket: WebSocket):
        """处理go-cqhttp的WebSocket连接"""
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
        """处理QQ事件"""
        event_type = event.get('post_type')
        logger.info(f"处理事件类型: {event_type}")
        
        # 处理消息事件
        if event_type == 'message':
            msg = event.get('message', '')
            user_id = event.get('user_id')
            logger.info(f"收到用户 {user_id} 的消息: {msg}")
            
            # 示例：处理查询命令
            if '原神' in msg or 'genshin' in msg.lower():
                reply = await self.query_genshin_info(msg)
                await self.send_private_msg(user_id, reply, ws)
                
    async def query_genshin_info(self, query):
        """模拟原神信息查询"""
        # 这里实现你的原神查询逻辑
        # 可以是数据库查询、API调用等
        if '角色' in query:
            return "角色列表：派蒙、旅行者、安柏、凯亚..."
        elif '版本' in query:
            return "当前版本：4.7「决战！铁甲熔火帝皇」"
        else:
            return "查询原神相关信息，可输入：\n1. 原神角色\n2. 原神版本\n3. 原神活动"
    
    async def send_private_msg(self, user_id, message, ws: WebSocket):
        """发送私聊消息"""
        payload = {
            "action": "send_private_msg",
            "params": {
                "user_id": user_id,
                "message": message
            }
        }
        logger.info(f"发送消息给用户 {user_id}: {message}")
        await ws.send_text(json.dumps(payload))

# 创建bot实例
bot = QQGenShinBot()

# 使用 lifespan 事件处理器
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    logger.info("QQ Genshin Bot 启动成功，监听端口 8081")
    logger.info("WebSocket 端点: ws://0.0.0.0:8081/ws 或 ws://0.0.0.0:8081/ws/")
    yield
    # 关闭时执行
    logger.info("QQ Genshin Bot 正在关闭...")

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
        "message": "QQ Genshin Bot is running",
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