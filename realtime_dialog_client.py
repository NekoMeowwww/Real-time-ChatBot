import gzip
import json
import copy
import logging
from typing import Dict, Any

import websockets

# 确保这些模块存在于同级目录
import config
import protocol

logger = logging.getLogger("RealtimeClient")

class RealtimeDialogClient:
    def __init__(self, config: Dict[str, Any], session_id: str, output_audio_format: str = "pcm",
                 mod: str = "audio", recv_timeout: int = 10) -> None:
        self.config = config
        self.logid = ""
        self.session_id = session_id
        self.output_audio_format = output_audio_format
        self.mod = mod
        self.recv_timeout = recv_timeout
        self.ws = None

    async def connect(self) -> None:
        """建立WebSocket连接"""
        try:
            self.ws = await websockets.connect(
                self.config['base_url'],
                extra_headers=self.config['headers'],
                ping_interval=None,
                max_size=None  # 建议：允许接收较大的消息
            )
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            raise e

        self.logid = self.ws.response_headers.get("X-Tt-Logid")
        logger.info(f"Dialog server connected, logid: {self.logid}")

        # --- 1. 发送 StartConnection ---
        start_connection_request = bytearray(protocol.generate_header())
        start_connection_request.extend(int(1).to_bytes(4, 'big'))
        payload_bytes = str.encode("{}")
        payload_bytes = gzip.compress(payload_bytes)
        start_connection_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        start_connection_request.extend(payload_bytes)
        
        await self.ws.send(start_connection_request)
        response = await self.ws.recv()
        
        # --- 2. 发送 StartSession ---
        # 使用 deepcopy 防止多用户并发时污染全局 config
        request_params = copy.deepcopy(config.start_session_req)
        
        # 设置当前会话特定的参数
        request_params["dialog"]["extra"]["recv_timeout"] = self.recv_timeout
        request_params["dialog"]["extra"]["input_mod"] = self.mod
        
        # [关键修复 1] 强制设置输入音频(ASR)的采样率为 24000，与前端 index.html 保持一致
        # 确保 'asr' 和 'audio_config' 键存在，防止 KeyError
        if "asr" not in request_params: request_params["asr"] = {}
        if "audio_config" not in request_params["asr"]: request_params["asr"]["audio_config"] = {}
        
        request_params["asr"]["audio_config"]["sample_rate"] = 24000
        request_params["asr"]["audio_config"]["format"] = "pcm" # 通常输入也是 pcm

        # [关键修复 2] 输出音频(TTS)采样率
        if self.output_audio_format == "pcm_s16le":
            request_params["tts"]["audio_config"]["format"] = "pcm_s16le"
            request_params["tts"]["audio_config"]["sample_rate"] = 24000

        payload_bytes = str.encode(json.dumps(request_params))
        payload_bytes = gzip.compress(payload_bytes)
        
        start_session_request = bytearray(protocol.generate_header())
        start_session_request.extend(int(100).to_bytes(4, 'big'))
        start_session_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        start_session_request.extend(str.encode(self.session_id))
        start_session_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        start_session_request.extend(payload_bytes)
        
        await self.ws.send(start_session_request)
        response = await self.ws.recv()

    async def say_hello(self) -> None:
        """发送Hello消息"""
        if not self.ws: return
        payload = {
            "content": "你好，我是湖州隆辕智控小助手，有什么可以帮助你的？",
        }
        hello_request = bytearray(protocol.generate_header())
        hello_request.extend(int(300).to_bytes(4, 'big'))
        payload_bytes = str.encode(json.dumps(payload))
        payload_bytes = gzip.compress(payload_bytes)
        hello_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        hello_request.extend(str.encode(self.session_id))
        hello_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        hello_request.extend(payload_bytes)
        await self.ws.send(hello_request)

    async def chat_text_query(self, content: str) -> None:
        if not self.ws: return
        payload = { "content": content }
        chat_text_query_request = bytearray(protocol.generate_header())
        chat_text_query_request.extend(int(501).to_bytes(4, 'big'))
        payload_bytes = str.encode(json.dumps(payload))
        payload_bytes = gzip.compress(payload_bytes)
        chat_text_query_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        chat_text_query_request.extend(str.encode(self.session_id))
        chat_text_query_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        chat_text_query_request.extend(payload_bytes)
        await self.ws.send(chat_text_query_request)

    async def chat_tts_text(self, is_user_querying: bool, start: bool, end: bool, content: str) -> None:
        if is_user_querying or not self.ws: return
        payload = { "start": start, "end": end, "content": content }
        payload_bytes = str.encode(json.dumps(payload))
        payload_bytes = gzip.compress(payload_bytes)
        chat_tts_text_request = bytearray(protocol.generate_header())
        chat_tts_text_request.extend(int(500).to_bytes(4, 'big'))
        chat_tts_text_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        chat_tts_text_request.extend(str.encode(self.session_id))
        chat_tts_text_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        chat_tts_text_request.extend(payload_bytes)
        await self.ws.send(chat_tts_text_request)

    async def chat_rag_text(self, is_user_querying: bool, external_rag: str) -> None:
        if is_user_querying or not self.ws: return
        payload = { "external_rag": external_rag }
        payload_bytes = str.encode(json.dumps(payload))
        payload_bytes = gzip.compress(payload_bytes)
        chat_rag_text_request = bytearray(protocol.generate_header())
        chat_rag_text_request.extend(int(502).to_bytes(4, 'big'))
        chat_rag_text_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        chat_rag_text_request.extend(str.encode(self.session_id))
        chat_rag_text_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        chat_rag_text_request.extend(payload_bytes)
        await self.ws.send(chat_rag_text_request)

    async def task_request(self, audio: bytes) -> None:
        if not self.ws: return
        task_request = bytearray(
            protocol.generate_header(message_type=protocol.CLIENT_AUDIO_ONLY_REQUEST,
                                     serial_method=protocol.NO_SERIALIZATION))
        task_request.extend(int(200).to_bytes(4, 'big'))
        task_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        task_request.extend(str.encode(self.session_id))
        payload_bytes = gzip.compress(audio)
        task_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        task_request.extend(payload_bytes)
        await self.ws.send(task_request)

    async def receive_server_response(self) -> Dict[str, Any]:
        try:
            if not self.ws:
                raise Exception("WebSocket not connected")
            response = await self.ws.recv()
            data = protocol.parse_response(response)
            return data
        except Exception as e:
            # 抛出异常让上层调用者(receive_loop)知道连接出了问题或被关闭
            raise e

    async def finish_session(self):
        if not self.ws: return
        finish_session_request = bytearray(protocol.generate_header())
        finish_session_request.extend(int(102).to_bytes(4, 'big'))
        payload_bytes = str.encode("{}")
        payload_bytes = gzip.compress(payload_bytes)
        finish_session_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        finish_session_request.extend(str.encode(self.session_id))
        finish_session_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        finish_session_request.extend(payload_bytes)
        await self.ws.send(finish_session_request)
        # [修改] 移除了 await self.ws.recv()，避免与 receive_loop 冲突

    async def finish_connection(self):
        if not self.ws: return
        finish_connection_request = bytearray(protocol.generate_header())
        finish_connection_request.extend(int(2).to_bytes(4, 'big'))
        payload_bytes = str.encode("{}")
        payload_bytes = gzip.compress(payload_bytes)
        finish_connection_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        finish_connection_request.extend(payload_bytes)
        await self.ws.send(finish_connection_request)
        # [修改] 移除了 await self.ws.recv()，避免与 receive_loop 冲突

    async def close(self) -> None:
        """关闭WebSocket连接"""
        if self.ws:
            logger.info(f"Closing WebSocket connection...")
            await self.ws.close()
            self.ws = None