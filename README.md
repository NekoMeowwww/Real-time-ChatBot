AI 实时语音对话系统 (Gemini Live 风格)

这是一个基于 Web 的全双工（Full-Duplex）实时语音对话系统。它模仿了 Gemini Live 的沉浸式 UI 设计，后端对接字节跳动豆包（Doubao）实时语音大模型，实现了低延迟、可打断的自然语音交互体验。

(界面示意图：沉浸式深色背景与动态声波)

✨ 功能特点

实时流式对话：基于 WebSocket 实现双向音频流传输，毫秒级响应。

高音质支持：全链路支持 24000Hz 高采样率，语音清晰自然，无机械感。

沉浸式 UI：使用 Tailwind CSS 构建的极简深色界面，包含磨砂玻璃（Glassmorphism）效果和实时音频可视化动效。

全双工交互：支持随时打断 AI 说话（需后端 VAD 支持配合），类似真人通话体验。

轻量级架构：前端无构建步骤（Vue 3 CDN），后端基于高性能 FastAPI。

🛠️ 技术栈

前端：Vue 3, Tailwind CSS, Web Audio API (ScriptProcessor/AudioContext)

后端：Python 3.8+, FastAPI, Uvicorn, Websockets

AI 服务：火山引擎 (Volcengine) 豆包实时语音大模型 SDK

📂 目录结构

.
├── index.html                  # 前端入口文件 (Vue 3 + Tailwind)
├── server.py                   # FastAPI 后端主服务 (WebSocket 入口)
├── audio_manager.py            # 音频会话管理器 (处理音频流转发)
├── realtime_dialog_client.py   # 豆包 SDK 客户端 (修复了并发和采样率问题)
├── config.py                   # [官方提供] 豆包 API 配置信息
├── protocol.py                 # [官方提供] 豆包协议解析文件
└── README.md                   # 项目文档


🚀 快速开始

1. 环境准备

确保你的系统已安装 Python 3.8 或更高版本。

# 安装依赖
pip install fastapi uvicorn websockets numpy


2. 配置 API 信息

在项目根目录下创建 config.py（如果你使用的是豆包官方 SDK 包，请参考其提供的配置模版），确保包含以下核心配置：

# config.py 示例
appid = "你的APPID"
token = "你的ACCESS_TOKEN"
cluster = "volc_auth_cluster" # 或其他集群ID

# 必须包含 ws_connect_config 或 ws_config 字典
ws_connect_config = {
    "base_url": "wss://[openspeech.bytedance.com/api/v3/realtime/dialogue](https://openspeech.bytedance.com/api/v3/realtime/dialogue)",
    "headers": {
        "X-Api-App-Id": appid,
        "X-Api-Access-Key": token,
        "X-Api-Resource-Id": "volc.speech.dialog",
    }
}

# 全局请求参数模版
start_session_req = {
    # ... 复制官方示例中的 JSON 结构 ...
}


3. 启动后端服务

运行 FastAPI 服务：

python server.py


服务默认运行在 ws://0.0.0.0:8000/ws/live。

4. 启动前端页面

由于浏览器麦克风权限限制，建议通过本地服务器访问前端页面（不要直接双击打开 html 文件）。

# 使用 Python 快速启动一个 HTTP 服务器
python -m http.server 3000


然后在浏览器访问：http://localhost:3000

点击页面底部的 麦克风图标 即可开始对话。

⚠️ 常见问题排查

Q1: 豆包说话语速特别慢/特别快，或者声音变粗？

原因：采样率不匹配。
解决：

检查 realtime_dialog_client.py 中 StartSession 参数是否显式设置了 "sample_rate": 24000。

检查 index.html 中 new AudioContext({ sampleRate: 24000 }) 是否一致。

本项目默认统一为 24000Hz。

Q2: 听到全是刺耳的噪音？

原因：音频解码格式错误。
解决：确保后端 server.py 初始化 session 时，output_audio_format 设置为 "pcm_s16le"。前端只支持解析 16-bit PCM 数据。

Q3: 报错 "cannot call recv while another coroutine is already waiting"？

原因：WebSocket 竞争条件。
解决：请使用本项目提供的修复版 realtime_dialog_client.py，其中移除了 finish_connection 中的 recv() 等待。

🔜 后续计划

[ ] 接入 VAD (语音活动检测) 实现更精准的打断逻辑。

[ ] 适配微信小程序端 (使用 RecorderManager 和 InnerAudioContext)。

[ ] 增加文字聊天记录回显。