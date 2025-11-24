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
    服务器端对话会话管理类 (集成 RAG 开关 + 半双工防打断 + 超时看门狗 + 主动打断支持)
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
        
        # 状态标志
        self.mute_audio = False
        self.is_responding = False
        self.pending_tts_count = 0
        self.rag_enabled = False
        
        self.filler_trigger_word = "收到" 
        
        # 看门狗任务引用
        self.watchdog_task = None
        # RAG 任务引用 (用于打断)
        self.rag_task = None

        self.simple_chat_keywords = {
            "你好", "您好", "嗨", "Hello", "Hi", "喂", "在吗",
            "谢谢", "感谢", "好的", "收到", "明白", "知道了", "OK",
            "再见", "拜拜", "没事", "没有", "对", "是的", "行"
        }

    def set_rag_enabled(self, enabled: bool):
        self.rag_enabled = enabled
        logger.info(f"RAG mode set to: {self.rag_enabled}")

    # [新增] 处理客户端发来的打断指令
    async def handle_interrupt(self):
        logger.info("Processing client interrupt request...")
        
        # 1. 立即开启静音，拦截后续音频
        self.mute_audio = True
        
        # 2. 取消正在进行的 RAG 任务
        if self.rag_task and not self.rag_task.done():
            self.rag_task.cancel()
            logger.info("Cancelled running RAG task.")
            
        # 3. 取消看门狗
        if self.watchdog_task:
            self.watchdog_task.cancel()
            
        # 4. 重置状态
        self.is_responding = False
        self.pending_tts_count = 0
        self.current_asr_text = ""
        
        # 5. 通知前端结束状态 (虽然前端点击时可能已经重置了，但双重保险)
        await self._notify_frontend("tts_end") 
        await self._notify_frontend("rag_end") # 确保思考状态也结束

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
        
        if self.watchdog_task: self.watchdog_task.cancel()
        if self.rag_task: self.rag_task.cancel()
            
        logger.info(f"Stopping session {self.session_id}...")
        try:
            await self.client.finish_session()
            await self.client.finish_connection()
            await self.client.close()
        except Exception as e:
            logger.error(f"Error closing session: {e}")

    async def push_audio(self, audio_chunk: bytes):
        if not self.is_running: return
        if self.is_responding: return # 半双工防打断
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
            if self.mute_audio: return 
            if self.output_callback:
                await self.output_callback(response['payload_msg'])

        elif msg_type == 'SERVER_FULL_RESPONSE':
            await self._handle_full_response(response)

        elif msg_type == 'SERVER_ERROR':
            logger.error(f"Server Error: {response.get('payload_msg')}")

    async def _notify_frontend(self, msg_type: str):
        if self.output_callback:
            await self.output_callback({"type": msg_type})
            
    def _is_simple_chat(self, text: str) -> bool:
        if not text: return True
        clean_text = text.strip().rstrip(".,?!。，？！")
        if len(clean_text) < 2: return True
        if clean_text in self.simple_chat_keywords: return True
        return False

    async def _response_watchdog(self, timeout=10):
        try:
            await asyncio.sleep(timeout)
            if self.is_responding:
                logger.warning(f"Response timeout ({timeout}s). Forcing unlock.")
                self.is_responding = False
                self.mute_audio = False
                self.pending_tts_count = 0
                await self._notify_frontend("rag_end") 
                await self._notify_frontend("tts_end")
        except asyncio.CancelledError:
            pass

    def _start_watchdog(self, timeout=10):
        if self.watchdog_task:
            self.watchdog_task.cancel()
        self.watchdog_task = asyncio.create_task(self._response_watchdog(timeout))

    async def _handle_full_response(self, response: Dict[str, Any]):
        event = response.get('event')
        payload = response.get('payload_msg', {})
        
        # A. ASR Update
        text = None
        if 'extra' in payload:
            text = payload['extra'].get('origin_text')
        if not text and 'result' in payload:
            text = payload['result']
        if text:
            self.current_asr_text = text

        # B. VAD End
        if event == 459:
            logger.info(f"[VAD END] Query: '{self.current_asr_text}'")
            
            if self.current_asr_text:
                self.is_responding = True 
                self._start_watchdog(10)
                
                if not self.rag_enabled:
                    logger.info("RAG Disabled. Using standard AI response.")
                    self.pending_tts_count = 1
                    self.current_asr_text = ""
                    return

                # RAG 流程
                self.mute_audio = True
                self.pending_tts_count = 2
                await self._notify_frontend("rag_start")
                # 保存 task 引用以便打断
                self.rag_task = asyncio.create_task(self._execute_rag_flow(self.current_asr_text))
                self.current_asr_text = ""
            else:
                logger.warning("VAD ended but no text.")

        # C. TTS Finished
        elif event == 359:
            if self.pending_tts_count > 0:
                self.pending_tts_count -= 1
            
            if self.pending_tts_count <= 0:
                if self.is_responding:
                    logger.info("Turn Finished. Unlocking input.")
                    self.is_responding = False
                    self.mute_audio = False
                    if self.watchdog_task: self.watchdog_task.cancel()
                    # [新增] 通知前端 TTS 结束，可以禁用打断按钮了
                    await self._notify_frontend("tts_end")

        elif event == 450:
            logger.info("User interrupt (Unexpected).")
            self.is_responding = False
            self.mute_audio = False 
            if self.watchdog_task: self.watchdog_task.cancel()
            await self._notify_frontend("rag_end")
            # [新增] 打断也意味着 TTS 结束
            await self._notify_frontend("tts_end")
            
        elif event in [152, 153]:
            self.is_session_finished = True
            self.is_running = False

    async def _execute_rag_flow(self, query_text: str):
        try:
            filler_text = f"{self.filler_trigger_word}，请稍等。"
            await self.client.chat_tts_text(is_user_querying=False, start=True, end=False, content=filler_text)
            await self.client.chat_tts_text(is_user_querying=False, start=False, end=True, content="")
            
            rag_result = await search_knowledge_base(query_text)
            
            if rag_result:
                logger.info(f"RAG Hit! Sending data.")
                if self.mute_audio: self.mute_audio = False
                await self._notify_frontend("rag_end")
                await self.client.chat_rag_text(is_user_querying=False, external_rag=rag_result)
            else:
                logger.info("RAG Miss.")
                if self.mute_audio: self.mute_audio = False
                await self._notify_frontend("rag_end")
                await self.client.chat_rag_text(is_user_querying=False, external_rag="")

        except asyncio.CancelledError:
            logger.info("RAG task cancelled.")
            self.mute_audio = False # 确保取消时不做奇怪的状态残留
            await self._notify_frontend("rag_end")
        except Exception as e:
            logger.error(f"RAG execution failed: {e}")
            self.mute_audio = False
            self.is_responding = False
            if self.watchdog_task: self.watchdog_task.cancel()
            await self._notify_frontend("rag_end")