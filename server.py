import asyncio
import logging
import json
from typing import Union
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

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

    async def send_to_browser(data: Union[bytes, dict]):
        try:
            if websocket.client_state != WebSocketState.CONNECTED:
                return

            if isinstance(data, bytes):
                await websocket.send_bytes(data)
            elif isinstance(data, dict):
                await websocket.send_text(json.dumps(data))
        except Exception as e:
            logger.error(f"Error sending to browser: {e}")

    try:
        ws_config = getattr(config, "ws_connect_config", None)
        if not ws_config:
             ws_config = getattr(config, "ws_config", {})
        
        if not ws_config:
            logger.error("Config Error: ws_connect_config not found")
            await websocket.close(code=1008)
            return

        session = DialogSession(
            ws_config=ws_config,
            output_callback=send_to_browser,
            output_audio_format="pcm_s16le",
            mod="audio"
        )
    except Exception as e:
        logger.error(f"Failed to init session: {e}")
        await websocket.close()
        return

    session_task = asyncio.create_task(session.start())

    try:
        while True:
            message = await websocket.receive()

            if "bytes" in message and message["bytes"]:
                await session.push_audio(message["bytes"])
            
            elif "text" in message and message["text"]:
                try:
                    data = json.loads(message["text"])
                    msg_type = data.get("type")
                    
                    if msg_type == "rag_switch":
                        enabled = data.get("enabled", False)
                        session.set_rag_enabled(enabled)
                        logger.info(f"Client set RAG to: {enabled}")
                    
                    # [新增] 处理打断指令
                    elif msg_type == "interrupt":
                        await session.handle_interrupt()
                        
                except json.JSONDecodeError:
                    logger.warning(f"Received invalid JSON: {message['text']}")
                except Exception as e:
                    logger.error(f"Error handling text message: {e}")

            elif message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect

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