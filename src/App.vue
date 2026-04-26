<template>
  <div class="app-shell">
    <div class="bg-overlay"></div>

    <button
      class="subtitle-toggle"
      :class="{ active: showSubtitles }"
      @click="toggleSubtitles"
    >
      字幕
    </button>

    <div
      class="nebula"
      :class="{ thinking: isThinking, hidden: showSubtitles }"
      :style="{ transform: `translate(-50%, -50%) scale(${1 + audioLevel * 1.5})` }"
    ></div>

    <div class="relative flex flex-col items-center justify-center z-10">
      <template v-if="!showSubtitles">
        <div
          class="voice-orb"
          :class="{ active: isCalling && !isThinking, thinking: isThinking }"
          :style="{ transform: `scale(${1 + (isThinking ? 0.1 : audioLevel * 0.5)})` }"
        >
          <div
            class="wave-bar"
            :style="!isThinking ? { height: `${isCalling ? 24 + audioLevel * 80 : 8}px` } : {}"
          ></div>
          <div
            class="wave-bar"
            :style="!isThinking ? { height: `${isCalling ? 36 + audioLevel * 120 : 12}px` } : {}"
          ></div>
          <div
            class="wave-bar"
            :style="!isThinking ? { height: `${isCalling ? 24 + audioLevel * 80 : 8}px` } : {}"
          ></div>
        </div>
      </template>
      <template v-else>
        <div ref="subtitleBox" class="subtitle-container">
          <div
            v-for="(msg, index) in messages"
            :key="`${msg.type}-${index}`"
            class="message"
            :class="[msg.type, { streaming: msg.streaming }]"
          >
            <span class="message-text">{{ msg.text }}</span>
            <span v-if="msg.streaming" class="streaming-cursor"></span>
          </div>
        </div>
      </template>

      <div class="status-text" :class="{ 'subtitle-mode': showSubtitles }">
        {{ statusText }}
      </div>
    </div>

    <div class="control-dock">
      <button
        class="btn-sub-action"
        :class="{ active: isRagEnabled }"
        title="知识库开关 (RAG)"
        @click="toggleRag"
      >
        <i class="fas fa-book" />
      </button>

      <button
        class="btn-main-action"
        :class="isCalling ? 'active' : 'inactive'"
        :title="isCalling ? '挂断' : '点击开始对话'"
        @click="toggleCall"
      >
        <i class="fas" :class="isCalling ? 'fa-plus' : 'fa-microphone'" />
      </button>

      <button
        class="btn-interrupt"
        :class="{ active: isAiSpeaking }"
        title="打断 AI 回复"
        @click="interruptAi"
      >
        <i class="fas fa-stop" />
      </button>
    </div>
  </div>
</template>

<script setup>
import { onUnmounted, ref, watch, nextTick } from 'vue';

const isCalling = ref(false);
const isThinking = ref(false);
const isRagEnabled = ref(false);
const isAiSpeaking = ref(false);
const statusText = ref('点击麦克风开始');
const audioLevel = ref(0);

// 字幕相关
const showSubtitles = ref(false); // 前端显示状态（不再需要后端字幕功能开关）
const subtitleBox = ref(null);
const currentStreamingText = ref('');
const messages = ref([
  {
    type: 'ai',
    text: '你好，我是湖州隆辕智控AI小助手，有什么可以帮助你的吗？',
    streaming: false
  }
]);
// 缓存 AI 文本，等待音频开始播放后再显示
const pendingAiText = ref(null);

let websocket = null;
let audioContext = null;
let mediaStream = null;
let scriptProcessor = null;
let analyser = null;
let analyserData = null;
let rafId = null;
let nextStartTime = 0;
let stateMonitorTimer = null;

const WS_PATH = '/ws/live';

const clearVisualLoop = () => {
  if (rafId) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
};

const updateVisuals = () => {
  if (!isCalling.value || !analyser || !analyserData) return;
  analyser.getByteFrequencyData(analyserData);
  const sum = analyserData.reduce((acc, value) => acc + value, 0);
  audioLevel.value = (sum / analyserData.length) / 64;
  rafId = requestAnimationFrame(updateVisuals);
};

const setupAudioNodes = () => {
  if (!audioContext || !mediaStream) return;
  const source = audioContext.createMediaStreamSource(mediaStream);

  analyser = audioContext.createAnalyser();
  analyser.fftSize = 256;
  analyserData = new Uint8Array(analyser.frequencyBinCount);
  source.connect(analyser);
  clearVisualLoop();
  updateVisuals();

  scriptProcessor = audioContext.createScriptProcessor(4096, 1, 1);
  source.connect(scriptProcessor);
  scriptProcessor.connect(audioContext.destination);

  scriptProcessor.onaudioprocess = (e) => {
    if (!websocket || websocket.readyState !== WebSocket.OPEN || isAiSpeaking.value) return;
    const inputData = e.inputBuffer.getChannelData(0);
    const pcmData = floatTo16BitPCM(inputData);
    websocket.send(pcmData);
  };
};

const floatTo16BitPCM = (input) => {
  const output = new Int16Array(input.length);
  for (let i = 0; i < input.length; i += 1) {
    const s = Math.max(-1, Math.min(1, input[i]));
    output[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return output.buffer;
};

const toggleRag = () => {
  isRagEnabled.value = !isRagEnabled.value;
  if (websocket && websocket.readyState === WebSocket.OPEN) {
    websocket.send(JSON.stringify({ type: 'rag_switch', enabled: isRagEnabled.value }));
  }
};

const toggleSubtitles = () => {
  showSubtitles.value = !showSubtitles.value;
  
  // 不再需要控制后端字幕功能，直接通过事件 550 获取文本
  if (showSubtitles.value) {
    nextTick(() => {
      scrollToBottom();
    });
  }
};

const scrollToBottom = () => {
  if (subtitleBox.value) {
    subtitleBox.value.scrollTop = subtitleBox.value.scrollHeight;
  }
};

const addMessage = (type, text, streaming = false) => {
  messages.value.push({
    type,
    text,
    streaming
  });
  nextTick(() => {
    scrollToBottom();
  });
};

// 监听 messages 变化，自动滚动到底部
watch(messages, () => {
  if (showSubtitles.value) {
    nextTick(() => {
      scrollToBottom();
    });
  }
}, { deep: true });

const interruptAi = () => {
  if (!isAiSpeaking.value) return;
  if (websocket && websocket.readyState === WebSocket.OPEN) {
    websocket.send(JSON.stringify({ type: 'interrupt' }));
  }
  stopAudioPlayback();
  isAiSpeaking.value = false;
  isThinking.value = false;
  statusText.value = '已打断';
  currentStreamingText.value = '';
  
  // 清除"..."消息（如果存在）
  if (showSubtitles.value) {
    const lastMsg = messages.value[messages.value.length - 1];
    if (lastMsg && lastMsg.type === 'ai' && lastMsg.text === '...') {
      messages.value.pop();
    }
  }
};

const stopAudioPlayback = async () => {
  if (!audioContext) return;
  try {
    await audioContext.suspend();
  } catch (err) {
    // 忽略短暂状态错误
  }
  await audioContext.close();
  audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
  nextStartTime = 0;
  if (mediaStream) {
    setupAudioNodes();
  }
};

const toggleCall = async () => {
  if (isCalling.value) {
    endCall();
  } else {
    await startCall();
  }
};

const startCall = async () => {
  try {
    statusText.value = '连接中...';
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    websocket = new WebSocket(`${protocol}//${host}${WS_PATH}`);
    // websocket = new WebSocket(`ws://127.0.0.1:8000${WS_PATH}`); //测试用
    websocket.onopen = () => {
      statusText.value = '正在聆听';
      isCalling.value = true;
      isThinking.value = false;
      isAiSpeaking.value = false;
      startAudioCapture();
      websocket.send(JSON.stringify({ type: 'rag_switch', enabled: isRagEnabled.value }));
      
      // 不再需要发送字幕开关，直接通过事件 550 获取 AI 回复文本
    };

    websocket.onmessage = async (event) => {
      if (event.data instanceof Blob || event.data instanceof ArrayBuffer) {
        const arrayBuffer = event.data instanceof Blob ? await event.data.arrayBuffer() : event.data;
        if (!isAiSpeaking.value) {
          isAiSpeaking.value = true;
          statusText.value = 'AI 回复中...';
          // AI 开始播放音频，处理缓存的文本
          if (pendingAiText.value && showSubtitles.value) {
            handlePendingAiText();
          }
        }
        playAudioChunk(arrayBuffer);
      } else if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data);
          handleControlSignal(msg);
          
          // 不再处理字幕生成结果，直接使用事件 550 的 ai_text
        } catch {
          // 控制信号解析失败忽略
        }
      }
    };

    websocket.onclose = () => {
      endCall();
    };

    websocket.onerror = () => {
      statusText.value = '连接失败';
    };
  } catch (error) {
    alert('无法连接到麦克风或服务器');
  }
};

const startAudioCapture = async () => {
  audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    setupAudioNodes();
  } catch (err) {
    console.error('Error accessing microphone:', err);
    statusText.value = '无法访问麦克风';
  }
};

const playAudioChunk = (arrayBuffer) => {
  if (!audioContext) return;
  if (audioContext.state === 'suspended') {
    audioContext.resume();
  }
  const int16Array = new Int16Array(arrayBuffer);
  const float32Array = new Float32Array(int16Array.length);
  for (let i = 0; i < int16Array.length; i += 1) {
    float32Array[i] = int16Array[i] / 32768;
  }
  const audioBuffer = audioContext.createBuffer(1, float32Array.length, 24000);
  audioBuffer.getChannelData(0).set(float32Array);
  const source = audioContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(audioContext.destination);

  const currentTime = audioContext.currentTime;
  if (nextStartTime < currentTime) {
    nextStartTime = currentTime;
  }
  source.start(nextStartTime);
  nextStartTime += audioBuffer.duration;
};

// 处理缓存的 AI 文本
const handlePendingAiText = () => {
  if (pendingAiText.value) {
    const cachedMsg = pendingAiText.value;
    pendingAiText.value = null; // 先清空缓存，避免重复处理
    processAiText(cachedMsg);
  }
};

// 处理 AI 文本消息
const processAiText = (msg) => {
  const aiText = msg.text.trim();
  if (!aiText) return;
  
  const lastMsg = messages.value[messages.value.length - 1];
  
  // 如果最后一条是"..."，先移除它（AI 开始说话了）
  if (lastMsg && lastMsg.type === 'ai' && lastMsg.text === '...') {
    messages.value.pop();
  }
  
  // 重新获取最后一条消息（可能已经移除了"..."）
  const currentLastMsg = messages.value[messages.value.length - 1];
  
  // 检查最后一条消息是否是正在流式更新的 AI 消息
  const isStreamingMsg = currentLastMsg && currentLastMsg.type === 'ai' && currentLastMsg.streaming === true;
  
  if (msg.streaming === true) {
    // 流式文本：更新现有流式消息或创建新的流式消息
    if (isStreamingMsg) {
      // 直接更新文本内容，Vue 会自动检测变化（不需要替换数组）
      currentLastMsg.text = aiText;
    } else {
      // 创建新的流式消息（第一次收到流式文本）
      addMessage('ai', aiText, true);
    }
    // 使用 requestAnimationFrame 优化滚动性能
    requestAnimationFrame(() => {
      scrollToBottom();
    });
  } else {
    // 最终文本：只更新流式消息，不创建新消息（事件 559 会标记为最终）
    // 注意：理论上不应该收到 streaming: false 的消息，因为服务端会发送事件 559
    if (isStreamingMsg) {
      // 更新文本内容
      if (currentLastMsg.text !== aiText) {
        currentLastMsg.text = aiText;
      }
      // 不在这里标记为最终，等待事件 559
    } else {
      // 如果没有流式消息，可能是异常情况，记录警告但不创建消息
      console.warn('收到最终文本但没有流式消息，可能是时序问题');
    }
    currentStreamingText.value = ''; // 清除流式文本
  }
};

const handleControlSignal = (msg) => {
  if (msg.type === 'rag_start') {
    isThinking.value = true;
    statusText.value = '正在思考...';
    // 字幕模式下，显示"..."表示思考中
    if (showSubtitles.value) {
      // 检查是否已有"..."消息，避免重复
      const lastMsg = messages.value[messages.value.length - 1];
      if (!lastMsg || lastMsg.type !== 'ai' || lastMsg.text !== '...') {
        addMessage('ai', '...', false);
      }
    }
  } else if (msg.type === 'rag_end') {
    isThinking.value = false;
    // 注意：不要在这里设置 isAiSpeaking = true，因为音频可能还没开始播放
    // isAiSpeaking 应该由实际收到音频数据时设置
    statusText.value = 'AI 准备回复';
    // 清除"..."，等待AI开始说话时再显示字幕
    if (showSubtitles.value) {
      const lastMsg = messages.value[messages.value.length - 1];
      // 如果最后一条是"..."，移除它
      if (lastMsg && lastMsg.type === 'ai' && lastMsg.text === '...') {
        messages.value.pop();
      }
    }
  } else if (msg.type === 'tts_end') {
    // TTS 结束，清除缓存的文本
    currentStreamingText.value = '';
    pendingAiText.value = null;
  } else if (msg.type === 'ai_text_end') {
    // 事件 559 (ChatEnded)：模型回复文本结束，标记流式消息为最终
    const lastMsg = messages.value[messages.value.length - 1];
    if (lastMsg && lastMsg.type === 'ai' && lastMsg.streaming) {
      // 如果有文本，更新文本；否则只标记为最终
      if (msg.text && msg.text.trim()) {
        const finalText = msg.text.trim();
        if (lastMsg.text !== finalText) {
          lastMsg.text = finalText;
        }
      }
      lastMsg.streaming = false;
      // 直接修改属性，Vue 会自动检测变化
    }
    currentStreamingText.value = '';
    pendingAiText.value = null;
  } else if (msg.type === 'asr_text' && msg.text) {
    // 处理用户语音识别的文本
    const userText = msg.text.trim();
    if (userText && showSubtitles.value) {
      // 只在最终结果（VAD End）时添加消息，避免中间结果造成重复
      if (msg.final) {
        // 检查是否已有相同的用户消息（避免重复）
        const lastMsg = messages.value[messages.value.length - 1];
        if (!lastMsg || lastMsg.type !== 'user' || lastMsg.text !== userText) {
          addMessage('user', userText, false);
        }
      }
      // 中间结果暂时不显示，避免消息过多
    }
  } else if (msg.type === 'ai_text' && msg.text) {
    // 处理 AI 回复的文本（来自事件 550 ChatResponse，流式返回）
    const aiText = msg.text.trim();
    if (aiText && showSubtitles.value) {
      // 只有在 AI 开始播放音频时才显示字幕
      // 如果还没开始播放，更新缓存（保留最新的文本）
      if (!isAiSpeaking.value) {
        pendingAiText.value = msg;
        return;
      }
      
      // AI 已经开始播放，处理文本
      processAiText(msg);
    }
  }
};

const startStateMonitor = () => {
  if (stateMonitorTimer) return;
  stateMonitorTimer = window.setInterval(() => {
    if (!audioContext || !isCalling.value) return;
    if (nextStartTime > audioContext.currentTime + 0.1) {
      if (!isAiSpeaking.value) {
        isAiSpeaking.value = true;
        statusText.value = 'AI 回复中...';
        // AI 开始播放音频，处理缓存的文本
        if (pendingAiText.value && showSubtitles.value) {
          handlePendingAiText();
        }
      }
    } else if (isAiSpeaking.value) {
      isAiSpeaking.value = false;
      statusText.value = '正在聆听';
      currentStreamingText.value = '';
    }
  }, 100);
};

startStateMonitor();

const endCall = () => {
  isCalling.value = false;
  isThinking.value = false;
  isAiSpeaking.value = false;
  statusText.value = '点击麦克风开始';
  audioLevel.value = 0;
  currentStreamingText.value = '';
  pendingAiText.value = null; // 清除缓存的文本
  
  // 不再需要关闭字幕功能

  if (websocket) {
    websocket.onopen = null;
    websocket.onmessage = null;
    websocket.onclose = null;
    websocket.onerror = null;
    try {
      websocket.close();
    } catch {
      // ignore
    }
    websocket = null;
  }

  if (mediaStream) {
    mediaStream.getTracks().forEach((track) => track.stop());
    mediaStream = null;
  }

  if (scriptProcessor) {
    scriptProcessor.disconnect();
    scriptProcessor.onaudioprocess = null;
    scriptProcessor = null;
  }

  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }

  analyser = null;
  analyserData = null;
  clearVisualLoop();
  nextStartTime = 0;
};

onUnmounted(() => {
  endCall();
  if (stateMonitorTimer) {
    clearInterval(stateMonitorTimer);
    stateMonitorTimer = null;
  }
});
</script>

<style scoped>
.app-shell {
  position: relative;
  width: 100vw;
  height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
</style>



