"""factorybot 启动入口。

默认以 mock 模式运行（无需真实 Kafka/MySQL/Redis/LLM）：
    python main.py

或：
    uvicorn app.main:app --reload --port 8000

真实模式：复制 .env.example 为 .env 并填写连接信息。
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD", "0") == "1"),
    )
