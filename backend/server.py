import asyncio
import logging
import json
from typing import Union, Optional, Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from pydantic import BaseModel, HttpUrl
from pathlib import Path

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

# 注意：不再使用字幕生成服务，直接使用事件 550 (ChatResponse) 获取 AI 回复文本
# 如果需要手动生成字幕，可以保留字幕生成 API 端点
caption_service = None
streaming_caption_service = None

# 配置静态文件服务（用于访问上传的文件）
upload_dir = Path(getattr(config, "upload_dir", "uploads"))
upload_dir.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(upload_dir)), name="uploads")

# 字幕生成请求模型
class CaptionSubmitRequest(BaseModel):
    audio_url: HttpUrl
    language: str = "zh-CN"
    words_per_line: int = 46
    max_lines: int = 1
    use_itn: bool = False
    use_punc: bool = True
    use_capitalize: bool = False
    use_ddc: bool = False
    caption_type: str = "auto"
    boosting_table_id: Optional[str] = None
    boosting_table_name: Optional[str] = None
    asr_appid: Optional[str] = None
    with_speaker_info: bool = True

class CaptionGenerateRequest(BaseModel):
    audio_url: HttpUrl
    language: str = "zh-CN"
    words_per_line: int = 46
    max_lines: int = 1
    use_itn: bool = False
    use_punc: bool = False
    use_capitalize: bool = False
    use_ddc: bool = False
    caption_type: str = "auto"
    timeout: int = 300
    poll_interval: float = 2.0

@app.get("/")
async def get_index():
    return FileResponse("index.html")


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
            mod="audio",
            streaming_caption_service=None  # 不再使用流式字幕服务
        )
        
        # 注意：字幕回调在 DialogSession.__init__ 中已经设置，使用 output_callback
        # 这里不需要重复设置，避免覆盖
    except Exception as e:
        logger.error(f"Failed to init session: {e}")
        await websocket.close()
        return

    session_task = asyncio.create_task(session.start())

    try:
        while True:
            message = await websocket.receive()

            if "bytes" in message and message["bytes"]:
                audio_data = message["bytes"]
                
                # 推送用户音频到实时语音对话服务
                await session.push_audio(audio_data)
            
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