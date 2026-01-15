import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

class QQGenShinBot:
    def __init__(self):
        self.connected_clients = set()
        
    async def handle_websocket(self, websocket: WebSocket):
        """处理go-cqhttp的WebSocket连接"""
        await websocket.accept()
        self.connected_clients.add(websocket)
        try:
            while True:
                message = await websocket.receive_text()
                event = json.loads(message)
                await self.handle_event(event, websocket)
        except WebSocketDisconnect:
            pass
        finally:
            self.connected_clients.discard(websocket)
    
    async def handle_event(self, event, ws: WebSocket):
        """处理QQ事件"""
        event_type = event.get('post_type')
        
        # 处理消息事件
        if event_type == 'message':
            msg = event.get('message', '')
            user_id = event.get('user_id')
            
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
        await ws.send_text(json.dumps(payload))

# 创建bot实例
bot = QQGenShinBot()

# 使用 lifespan 事件处理器
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    print("Bot 启动成功，监听端口 8081")
    yield
    # 关闭时执行
    print("Bot 正在关闭...")

app = FastAPI(lifespan=lifespan)

# WebSocket 路由
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 连接端点，用于 go-cqhttp 连接"""
    await bot.handle_websocket(websocket)

# 健康检查接口
@app.get("/")
async def root():
    return {"status": "running", "message": "QQ Genshin Bot is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)