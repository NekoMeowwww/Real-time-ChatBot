import aiohttp
import json
import logging
from typing import Optional, List, Dict

logger = logging.getLogger("RAGService")

# --- 配置区域 (请填入你的真实信息) ---

# 1. 域名 (公网域名)
KNOWLEDGE_BASE_DOMAIN = "api-knowledgebase.mlp.cn-beijing.volces.com" 

# 2. API Key (Bearer Token)
# 在这里直接填入火山控制台获取的 API Key
API_KEY = "f58edcaf-d01c-4439-9ee4-21ac44ad3044"

# 3. [核心] Service Resource ID (服务资源 ID)
# 请在火山引擎控制台 -> 知识库 -> 服务管理 中查看 (通常以 service- 开头)
SERVICE_RESOURCE_ID = "kb-service-8b2a98ca73f1cea3" 
# -----------------------------------

# 接口地址: Service Chat (对话问答接口)
KNOWLEDGE_CHAT_URL = f"https://{KNOWLEDGE_BASE_DOMAIN}/api/knowledge/service/chat"

async def search_knowledge_base(query: str) -> str:
    """
    调用火山引擎知识库 Service Chat 接口
    仅使用检索结果（result_list），不使用 generated_answer，避免与主模型回答混叠。
    """
    if not query:
        return ""
        
    logger.info(f"[RAG] Calling Knowledge Service for: {query}")
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}" # API Key 鉴权，简单直接
    }
    
    # 构造请求体 (符合 Service Chat 接口定义)
    payload = {
        "service_resource_id": SERVICE_RESOURCE_ID,
        "messages": [
            {
                "role": "user",
                "content": query
            }
        ],
        # 仅检索，不要让知识库生成回答，避免与主对话模型重复
        "stream": False
    }

    try:
        async with aiohttp.ClientSession() as session:
            # 注意：超时时间设长一点，因为 Service Chat 需要 LLM 生成，比纯检索慢
            async with session.post(KNOWLEDGE_CHAT_URL, headers=headers, ssl = False, json=payload, timeout=15) as resp:
                
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"[RAG API ERROR] Status: {resp.status}, Response: {error_text}")
                    return ""
                
                data = await resp.json()
                
                # --- 解析响应 ---
                # Service Chat 非流式返回结构通常包含 'data'
                response_data = data.get("data", {})
                
                result_list = response_data.get("result_list", [])
                rag_payload = []
                
                if result_list:
                    logger.info(f"[RAG] Success! Got {len(result_list)} source chunks.")
                    for idx, item in enumerate(result_list):
                        content = item.get("content") or item.get("original_content", "")
                        title = item.get("title") or f"参考资料_{idx+1}"
                        if content:
                            rag_payload.append({"title": title, "content": content})
                else:
                    logger.warning("[RAG] API returned 200 but no result_list content.")
                    return ""

                return json.dumps(rag_payload, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[RAG Request Exception]: {e}")
        return ""