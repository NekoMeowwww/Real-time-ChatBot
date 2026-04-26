import asyncio
import uuid
import logging
import json
import time
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
                 mod: str = "audio",
                 streaming_caption_service = None):
        
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
        
        # 累积事件 550 的文本片段（流式返回）
        self.current_ai_text_buffer = ""
        # 最后活动时间（任意事件/音频到达时更新，用于超时保护）
        self.last_activity_ts = time.time()
        
        self.filler_trigger_word = "收到" 
        
        # 看门狗任务引用
        self.watchdog_task = None
        # RAG 任务引用 (用于打断)
        self.rag_task = None
        
        # 不再使用流式字幕服务，直接使用事件 550 (ChatResponse) 获取 AI 回复文本
        self.streaming_caption_service = None

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
        self.current_ai_text_buffer = ""  # 清空累积的 AI 文本
        
        # 5. 通知前端结束状态 (虽然前端点击时可能已经重置了，但双重保险)
        await self._notify_frontend("tts_end") 
        await self._notify_frontend("rag_end") # 确保思考状态也结束

    def set_caption_enabled(self, enabled: bool):
        """设置字幕功能开启/关闭（已废弃，不再使用字幕生成服务）"""
        # 不再使用字幕生成服务，直接通过事件 550 获取文本
        logger.debug(f"set_caption_enabled 调用已废弃，字幕功能通过事件 550 自动获取")

    async def start(self) -> None:
        try:
            logger.info(f"Starting session {self.session_id}...")
            await self.client.connect()
            await self.client.say_hello()
            
            # 不再使用字幕生成服务，直接通过事件 550 (ChatResponse) 获取 AI 回复文本
            
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
        # 全双工测试：不再阻塞用户音频，直接转发
        try:
            self.last_activity_ts = time.time()
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
        # 更新最后活动时间
        self.last_activity_ts = time.time()
        msg_type = response.get('message_type')

        if msg_type == 'SERVER_ACK' and isinstance(response.get('payload_msg'), bytes):
            audio_data = response['payload_msg']
            
            # 不再收集音频用于字幕生成，直接使用事件 550 (ChatResponse) 获取文本
            
            if self.mute_audio: return 
            
            if self.output_callback:
                await self.output_callback(audio_data)

        elif msg_type == 'SERVER_FULL_RESPONSE':
            await self._handle_full_response(response)

        elif msg_type == 'SERVER_ERROR':
            logger.error(f"Server Error: {response.get('payload_msg')}")

    async def _notify_frontend(self, msg):
        """
        通知前端
        
        Args:
            msg: 可以是字符串（消息类型）或字典（完整消息对象）
        """
        if self.output_callback:
            if isinstance(msg, str):
                await self.output_callback({"type": msg})
            else:
                await self.output_callback(msg)
            
    def _is_simple_chat(self, text: str) -> bool:
        if not text: return True
        clean_text = text.strip().rstrip(".,?!。，？！")
        if len(clean_text) < 2: return True
        if clean_text in self.simple_chat_keywords: return True
        return False

    async def _response_watchdog(self, timeout=15):
        try:
            await asyncio.sleep(timeout)
            # 如果超时内无新事件且仍在响应中，强制解锁
            now = time.time()
            if self.is_responding and now - self.last_activity_ts >= timeout:
                logger.warning(f"Response timeout ({timeout}s) since last activity. Forcing unlock.")
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
        
        # A. ASR Update - 用户语音识别文本（中间结果）
        text = None
        if 'extra' in payload:
            text = payload['extra'].get('origin_text')
        if not text and 'result' in payload:
            text = payload['result']
        if text:
            self.current_asr_text = text
            # 注意：中间结果不发送给前端，避免消息过多
            # 只在 VAD End 时发送最终结果

        # B. VAD End - 用户说话结束
        if event == 459:
            logger.info(f"[VAD END] Query: '{self.current_asr_text}'")
            
            # 【关键修复】强制重置上一轮的所有状态，确保新的一轮对话可以正常开始
            # 防止上一轮状态残留导致新问题无法触发响应
            if self.is_responding:
                logger.warning(f"[VAD END] 检测到上一轮仍在响应中，强制重置状态")
                self.is_responding = False
                self.mute_audio = False
                self.pending_tts_count = 0
                if self.watchdog_task:
                    self.watchdog_task.cancel()
                    self.watchdog_task = None
                if self.rag_task and not self.rag_task.done():
                    self.rag_task.cancel()
                    logger.info("取消上一轮未完成的RAG任务")
            
            # 清空上一轮 AI 回复的累积文本
            self.current_ai_text_buffer = ""
            
            if self.current_asr_text:
                # 发送最终的用户输入文本给前端（用于字幕显示）
                await self._notify_frontend({
                    "type": "asr_text",
                    "text": self.current_asr_text,
                    "final": True  # 标记为最终结果
                })
                
                self.is_responding = True 
                self._start_watchdog(10)
                
                if not self.rag_enabled:
                    logger.info("RAG Disabled. Using standard AI response.")
                    self.pending_tts_count = 1
                    self.current_asr_text = ""
                    return

                # RAG 流程
                self.mute_audio = True
                # 【关键修复】RAG流程中：
                # 填充音频"收到，请稍等"通过chat_tts_text发送，可能不会单独产生事件359
                # 只有主回复的TTS会产生事件359
                # 所以只需要等待1个TTS完成事件
                self.pending_tts_count = 1
                logger.info(f"[RAG] 设置pending_tts_count=1（只等待主回复的TTS完成）")
                await self._notify_frontend("rag_start")
                # 保存 task 引用以便打断
                self.rag_task = asyncio.create_task(self._execute_rag_flow(self.current_asr_text))
                self.current_asr_text = ""
            else:
                logger.warning("VAD ended but no text.")

        # C. ChatResponse - 模型回复的文本内容（事件 550）
        elif event == 550:
            # 提取模型回复的文本内容
            # payload 可能是字典或字符串
            content = None
            if isinstance(payload, dict):
                # 直接获取 content 字段
                content = payload.get('content')
                # 如果没有 content，尝试其他可能的字段
                if not content:
                    content = payload.get('text') or payload.get('result')
            elif isinstance(payload, str):
                # 尝试解析 JSON 字符串
                try:
                    payload_dict = json.loads(payload)
                    content = payload_dict.get('content') or payload_dict.get('text') or payload_dict.get('result')
                except (json.JSONDecodeError, TypeError):
                    # 如果不是 JSON，直接使用字符串作为内容
                    content = payload if payload.strip() else None
            
            if content and content.strip():
                content = content.strip()
                
                # 【关键修复】如果处于静音状态，直接丢弃文本，不暂存
                # 原因：静音期间的文本可能是大模型在思考时产生的，不应该显示
                # 填充音频"收到，请稍等"是通过chat_tts_text发送的，不会产生事件550的文本
                # 但如果大模型在收到RAG数据之前就开始生成回复，这些文本应该被丢弃
                if self.mute_audio:
                    logger.debug(f"[ChatResponse Event 550] 静音期间收到文本片段: '{content}'，直接丢弃（不显示字幕）")
                    # 不累积到任何缓冲区，直接丢弃
                    return
                
                # 非静音状态：正常累积和发送文本
                # 累积文本片段
                self.current_ai_text_buffer += content
                logger.debug(f"[ChatResponse Event 550] 收到文本片段: '{content}', 累积文本: '{self.current_ai_text_buffer}'")
                
                # 直接流式发送完整累积文本（保持与音频一致）
                await self._notify_frontend({
                    "type": "ai_text",
                    "text": self.current_ai_text_buffer,
                    "streaming": True,  # 标记为流式，前端会更新而不是创建新消息
                    "source": "chat_response"
                })
            else:
                logger.debug(f"[ChatResponse Event 550] 未找到有效内容，payload 类型: {type(payload)}, payload: {payload}")

        # D. ChatEnded - 模型回复文本结束事件（事件 559）
        elif event == 559:
            logger.info("[ChatEnded Event 559] AI 回复文本结束")
            # 发送结束信号，标记流式消息为最终
            if self.current_ai_text_buffer:
                await self._notify_frontend({
                    "type": "ai_text_end",
                    "text": self.current_ai_text_buffer,
                    "source": "chat_response"
                })
            else:
                # 即使没有文本，也发送结束信号
                await self._notify_frontend({
                    "type": "ai_text_end",
                    "text": "",
                    "source": "chat_response"
                })
            
            # 【关键修复】事件559表示AI回复文本生成完成，对话已经结束
            # 即使TTS音频可能还在传输/播放中，也应该允许下一轮对话开始
            # 因为文本已经生成完成，用户可以开始下一轮提问
            # 事件359（TTS Finished）只是音频播放完成的信号，不应该阻塞下一轮对话
            
            # 更新活动时间
            self.last_activity_ts = time.time()
            
            # 事件559到达时，强制解锁状态，允许下一轮对话开始
            was_responding = self.is_responding
            self.is_responding = False
            self.mute_audio = False
            # 不清零pending_tts_count，让事件359可以正常处理（如果后续到达）
            # 但不再依赖pending_tts_count来决定是否解锁
            self.current_asr_text = ""
            if self.watchdog_task:
                self.watchdog_task.cancel()
                self.watchdog_task = None
            
            if was_responding:
                logger.info("[ChatEnded Event 559] 对话结束，解锁状态，允许下一轮对话")
                await self._notify_frontend("tts_end")
            
            # 清空缓冲区，准备下一轮对话
            self.current_ai_text_buffer = ""

        # E. TTS Finished
        elif event == 359:
            logger.info(f"[TTS Finished Event 359] pending_tts_count: {self.pending_tts_count}")
            if self.pending_tts_count > 0:
                self.pending_tts_count -= 1
            
            # 【关键修复】TTS结束事件是真正结束一轮对话的信号
            # 当pending_tts_count归零时，说明所有TTS音频都已播放完成
            # 此时应该解锁状态，允许下一轮对话开始
            if self.pending_tts_count <= 0:
                if self.is_responding:
                    logger.info("[TTS Finished Event 359] 所有TTS完成，解锁状态，允许下一轮对话")
                    self.is_responding = False
                    self.mute_audio = False
                    self.current_asr_text = ""
                    if self.watchdog_task: 
                        self.watchdog_task.cancel()
                        self.watchdog_task = None
                    
                    # 通知前端 TTS 结束，可以开始下一轮录音了
                    await self._notify_frontend("tts_end")
                    
                    # 清空缓冲区，准备下一轮对话
                    self.current_ai_text_buffer = ""
                    # 重置活动时间
                    self.last_activity_ts = time.time()
                else:
                    logger.debug("[TTS Finished Event 359] TTS完成，但状态已解锁（可能事件559已处理）")
            else:
                logger.debug(f"[TTS Finished Event 359] TTS片段完成，但仍有{self.pending_tts_count}个TTS待完成")

        elif event == 450:
            logger.info("User interrupt (Unexpected).")
            self.is_responding = False
            self.mute_audio = False 
            if self.watchdog_task: self.watchdog_task.cancel()
            await self._notify_frontend("rag_end")
            # [新增] 打断也意味着 TTS 结束
            await self._notify_frontend("tts_end")
            # 清空 AI 文本缓冲区
            self.current_ai_text_buffer = ""
            
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