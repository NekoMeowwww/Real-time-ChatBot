import asyncio
import logging
import json
from typing import Union
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# 引入修改后的 DialogSession (audio_manager.py) 和 配置文件 (config.py)
from audio_manager import DialogSession
import config

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VoiceServer")

app = FastAPI()

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebClient connected")

    # [修改] 回调函数现在支持 bytes (音频) 和 dict (控制信号)
    async def send_to_browser(data: Union[bytes, dict]):
        try:
            if isinstance(data, bytes):
                # 发送二进制音频流
                await websocket.send_bytes(data)
            elif isinstance(data, dict):
                # 发送 JSON 控制信号 (如: rag_start, rag_end)
                await websocket.send_text(json.dumps(data))
        except Exception as e:
            logger.error(f"Error sending to browser: {e}")

    # 初始化 DialogSession
    try:
        # 获取配置
        ws_config = getattr(config, "ws_connect_config", None)
        if not ws_config:
             ws_config = getattr(config, "ws_config", {})
        
        if not ws_config:
            logger.error("Config Error: ws_connect_config not found")
            await websocket.close(code=1008)
            return

        # 初始化会话
        session = DialogSession(
            ws_config=ws_config,
            output_callback=send_to_browser, # 使用新的回调
            output_audio_format="pcm_s16le",
            mod="audio"
        )
    except Exception as e:
        logger.error(f"Failed to init session: {e}")
        await websocket.close()
        return

    # 启动后台任务
    session_task = asyncio.create_task(session.start())

    try:
        # 主循环
        while True:
            user_audio_data = await websocket.receive_bytes()
            await session.push_audio(user_audio_data)

    except WebSocketDisconnect:
        logger.info("WebClient disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        logger.info("Cleaning up session...")
        await session.stop()
        if not session_task.done():
            session_task.cancel()
            try:
                await session_task
            except asyncio.CancelledError:
                pass
        logger.info("Session cleanup done")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)