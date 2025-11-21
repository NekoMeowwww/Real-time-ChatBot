import asyncio
import uuid
import logging
import json
from typing import Optional, Dict, Any, Callable, Awaitable, Union

import config
from realtime_dialog_client import RealtimeDialogClient
from rag_service import search_knowledge_base

# 配置日志
logger = logging.getLogger("DialogSession")

class DialogSession:
    """
    服务器端对话会话管理类 (集成 RAG + 半双工防打断模式)
    """
    
    def __init__(self, 
                 ws_config: Dict[str, Any], 
                 output_callback: Callable[[Union[bytes, Dict]], Awaitable[None]],
                 output_audio_format: str = "pcm", 
                 mod: str = "audio"):
        
        self.session_id = str(uuid.uuid4())
        self.mod = mod
        self.is_running = True
        self.output_callback = output_callback
        
        self.client = RealtimeDialogClient(
            config=ws_config, 
            session_id=self.session_id,
            output_audio_format=output_audio_format, 
            mod=mod
        )
        
        self.is_session_finished = False
        self.current_asr_text = ""
        
        # [修改] 静音标志位：用于拦截 AI 的幻觉抢答 (RAG 期间)
        self.mute_audio = False
        
        # [新增] 响应状态锁：True 表示 AI 正在思考或说话，此时拒绝用户输入
        self.is_responding = False
        
        # [新增] 待完成 TTS 任务计数器 (用于 RAG 流程：安抚词+正式回答=2)
        self.pending_tts_count = 0
        
        self.filler_trigger_word = "收到" 

        self.simple_chat_keywords = {
            "你好", "您好", "嗨", "Hello", "Hi", "喂", "在吗",
            "谢谢", "感谢", "好的", "收到", "明白", "知道了", "OK",
            "再见", "拜拜", "没事", "没有", "对", "是的", "行"
        }

    async def start(self) -> None:
        try:
            logger.info(f"Starting session {self.session_id}...")
            await self.client.connect()
            await self.client.say_hello()
            await self.receive_loop()
        except Exception as e:
            logger.error(f"Session start error: {e}")
        finally:
            await self.stop()

    async def stop(self):
        if not self.is_running: return
        self.is_running = False
        logger.info(f"Stopping session {self.session_id}...")
        try:
            await self.client.finish_session()
            await self.client.finish_connection()
            await self.client.close()
        except Exception as e:
            logger.error(f"Error closing session: {e}")

    async def push_audio(self, audio_chunk: bytes):
        """
        推送音频到服务端
        [核心修改] 半双工逻辑：如果 AI 正在响应，直接丢弃用户的音频输入。
        """
        if not self.is_running: return
        
        # 如果 AI 正在处理或说话，无视用户说话 (防打断)
        if self.is_responding:
            return

        try:
            await self.client.task_request(audio_chunk)
        except Exception as e:
            logger.error(f"Error pushing audio: {e}")

    async def receive_loop(self):
        try:
            while self.is_running:
                response = await self.client.receive_server_response()
                await self.handle_server_response(response)
                if self.is_session_finished:
                    break
        except asyncio.CancelledError:
            logger.info("Receive loop cancelled")
        except Exception as e:
            if self.is_running:
                logger.error(f"Receive loop error: {e}")
        finally:
            await self.stop()

    async def handle_server_response(self, response: Dict[str, Any]) -> None:
        if not response: return
        msg_type = response.get('message_type')

        if msg_type == 'SERVER_ACK' and isinstance(response.get('payload_msg'), bytes):
            # 静音拦截逻辑 (仅用于 RAG 思考期间屏蔽幻觉)
            if self.mute_audio:
                return 
            
            if self.output_callback:
                await self.output_callback(response['payload_msg'])

        elif msg_type == 'SERVER_FULL_RESPONSE':
            await self._handle_full_response(response)

        elif msg_type == 'SERVER_ERROR':
            logger.error(f"Server Error: {response.get('payload_msg')}")

    async def _notify_frontend(self, msg_type: str):
        """发送 JSON 控制信号给前端"""
        if self.output_callback:
            await self.output_callback({"type": msg_type})
            
    def _is_simple_chat(self, text: str) -> bool:
        if not text: return True
        clean_text = text.strip().rstrip(".,?!。，？！")
        if clean_text in self.simple_chat_keywords:
            return True
        return False

    async def _handle_full_response(self, response: Dict[str, Any]):
        event = response.get('event')
        payload = response.get('payload_msg', {})
        
        # A. ASR 文本捕获
        text = None
        if 'extra' in payload:
            text = payload['extra'].get('origin_text')
        if not text and 'result' in payload:
            text = payload['result']
        if text:
            self.current_asr_text = text

        # B. [核心] VAD End -> 用户说完话，AI 开始处理
        if event == 459: # VAD End
            logger.info(f"[VAD END] Query: '{self.current_asr_text}'")
            
            if self.current_asr_text:
                # 锁定状态，禁止用户打断
                self.is_responding = True 
                
                # 判断流程：闲聊 vs RAG
                if self._is_simple_chat(self.current_asr_text):
                    logger.info("Simple chat detected.")
                    self.pending_tts_count = 1 # 期待 1 次回答结束
                    self.current_asr_text = ""
                    return # 直接返回，让豆包默认处理

                # RAG 流程
                self.mute_audio = True # 开启静音拦截幻觉
                self.pending_tts_count = 2 # 期待 2 次回答 (安抚词 + 最终结果)
                
                await self._notify_frontend("rag_start")
                asyncio.create_task(self._execute_rag_flow(self.current_asr_text))
                self.current_asr_text = ""
            else:
                logger.warning("VAD ended but no text.")

        # C. [核心] Event 359 -> TTS 播放结束
        elif event == 359:
            # 减少待完成任务计数
            if self.pending_tts_count > 0:
                self.pending_tts_count -= 1
                logger.info(f"TTS Finished. Pending count: {self.pending_tts_count}")
            
            # 如果所有 TTS 都播放完了，解锁，允许用户进行下一轮对话
            if self.pending_tts_count <= 0:
                if self.is_responding:
                    logger.info("Turn Finished. Unlocking input.")
                    self.is_responding = False
                    # 确保静音关闭 (防止意外卡死)
                    self.mute_audio = False

        elif event == 450:
            # 理论上，因为我们 block 了音频，不会触发 Event 450。
            # 但如果触发了，还是要重置状态以防卡死。
            logger.info("User interrupt (Unexpected). Resetting.")
            self.is_responding = False
            self.mute_audio = False 
            await self._notify_frontend("rag_end")
            
        elif event in [152, 153]:
            self.is_session_finished = True
            self.is_running = False

    async def _execute_rag_flow(self, query_text: str):
        try:
            # 1. 发送安抚话术 (计数+1，已在 event 459 预设为 2)
            filler_text = f"{self.filler_trigger_word}，请稍等。"
            
            await self.client.chat_tts_text(is_user_querying=False, start=True, end=False, content=filler_text)
            await self.client.chat_tts_text(is_user_querying=False, start=False, end=True, content="")
            
            # RAG 期间，我们不希望听到安抚话术，保持静音直到结果出来
            # 但前端还是显示思考动画

            # 2. 查询知识库
            rag_result = await search_knowledge_base(query_text)
            
            # 3. 提交结果
            if rag_result:
                logger.info(f"RAG Hit! Sending data.")
                if self.mute_audio: 
                    self.mute_audio = False
                
                await self._notify_frontend("rag_end")
                
                await self.client.chat_rag_text(is_user_querying=False, external_rag=rag_result)
            else:
                logger.info("RAG Miss.")
                if self.mute_audio:
                    self.mute_audio = False
                
                await self._notify_frontend("rag_end")

                await self.client.chat_rag_text(is_user_querying=False, external_rag="")

        except Exception as e:
            logger.error(f"RAG execution failed: {e}")
            self.mute_audio = False
            self.is_responding = False # 出错时必须解锁
            await self._notify_frontend("rag_end")