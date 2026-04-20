import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from agent.clients.dial_client import DialClient
from agent.clients.http_mcp_client import HttpMCPClient
from agent.clients.stdio_mcp_client import StdioMCPClient
from agent.conversation_manager import ConversationManager
from agent.models.message import Message

DIAL_ENDPOINT = "https://ai-proxy.lab.epam.com"
API_KEY = os.getenv('DIAL_API_KEY', '')

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

conversation_manager: Optional[ConversationManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize MCP clients, Redis, and ConversationManager on startup"""
    global conversation_manager

    logger.info("Application startup initiated")

    tools: list[dict[str, Any]] = []
    tool_name_client_map: dict[str, HttpMCPClient | StdioMCPClient] = {}

    logger.info("Initializing UMS MCP client")
    ums_mcp_url = os.getenv("UMS_MCP_URL", "http://localhost:8005/mcp")
    logger.info("UMS MCP URL: %s", ums_mcp_url)
    ums_mcp_client = await HttpMCPClient.create(ums_mcp_url)
    ums_tools = await ums_mcp_client.get_tools()
    for tool in ums_tools:
        tools.append(tool)
        tool_name_client_map[tool["function"]["name"]] = ums_mcp_client


    logger.info("Initializing Fetch MCP client")
    fetch_mcp_url = os.getenv("FETCH_MCP_URL", "https://remote.mcpservers.org/fetch/mcp")
    logger.info("Fetch MCP URL: %s", fetch_mcp_url)
    fetch_mcp_client = await HttpMCPClient.create(fetch_mcp_url)
    fetch_tools = await fetch_mcp_client.get_tools()
    for tool in fetch_tools:
        tools.append(tool)
        tool_name_client_map[tool["function"]["name"]] = fetch_mcp_client


    logger.info("Initializing DuckDuckGo MCP client")
    duck_mcp_client = await StdioMCPClient.create("mcp/duckduckgo:latest")
    duck_tools = await duck_mcp_client.get_tools()
    for tool in duck_tools:
        tools.append(tool)
        tool_name_client_map[tool["function"]["name"]] = duck_mcp_client


    dial_api_key = os.getenv("DIAL_API_KEY", API_KEY)
    if not dial_api_key:
        logger.error("DIAL_API_KEY environment variable not set")
        raise ValueError("DIAL_API_KEY environment variable is required")
    
    model = os.getenv("ORCHESTRATION_MODEL", "gpt-4o")
    endpoint=os.getenv("DIAL_URL", DIAL_ENDPOINT)
    logger.info("Initializing DIAL client", extra={"url": endpoint, "model": model, "api_key": "***" if dial_api_key else None})

    dial_client = DialClient(
        api_key=dial_api_key,
        endpoint=endpoint,
        model=model,
        tools=tools,
        tool_name_client_map=tool_name_client_map
    )


    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", 6379))

    logger.info(
        "Connecting to Redis",
        extra={"host": redis_host, "port": redis_port}
    )

    redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    try:
        await redis_client.ping() # type: ignore
        logger.info("Successfully connected to Redis")
    except Exception as e:
        logger.error("Failed to connect to Redis", exc_info=e)
        raise RuntimeError("Could not connect to Redis") from e
    
    conversation_manager = ConversationManager(dial_client=dial_client, redis_client=redis_client)
    logger.info("ConversationManager initialized and ready")

    yield

    logger.info("Application shutdown initiated")
    await redis_client.close()
    logger.info("Application shutdown completed")


app = FastAPI(
    lifespan=lifespan
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request/Response Models
class ChatRequest(BaseModel):
    message: Message
    stream: bool = True


class ChatResponse(BaseModel):
    content: str
    conversation_id: str


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class CreateConversationRequest(BaseModel):
    title: Optional[str] = None


# Endpoints
@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check endpoint"""
    logger.debug("Health check requested")
    return {
        "status": "healthy",
        "conversation_manager_initialized": conversation_manager is not None
    }

@app.post("/conversations")
async def create_conversation(request: CreateConversationRequest) -> dict[str, Any]:
    """Create a new conversation with optional title"""
    if not conversation_manager:
        logger.error("ConversationManager not initialized")
        raise HTTPException(status_code=500, detail="Conversation manager is not initialized")

    conversation = await conversation_manager.create_conversation(title=request.title)
    logger.info(
        "Created new conversation",
        extra={
            "conversation_id": conversation["id"],
            "title": conversation["title"]
        }
    )
    return {"conversation_id": conversation["id"], "title": conversation["title"]}

@app.get("/conversations")
async def list_conversations() -> list[ConversationSummary]:
    """List all conversations with summaries"""
    if not conversation_manager:
        logger.error("ConversationManager not initialized")
        raise HTTPException(status_code=500, detail="Conversation manager is not initialized")

    conversations = await conversation_manager.list_conversations()
    return [ConversationSummary(**conv) for conv in conversations]

@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    """Get full conversation by ID"""
    if not conversation_manager:
        logger.error("ConversationManager not initialized")
        raise HTTPException(status_code=500, detail="Conversation manager is not initialized")

    conversation = await conversation_manager.get_conversation(conversation_id)
    if conversation:
        return conversation
    else:
        raise HTTPException(status_code=404, detail=f"Conversation {conversation_id} not found")

@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, Any]:
    """Delete a conversation by ID"""
    if not conversation_manager:
        logger.error("ConversationManager not initialized")
        raise HTTPException(status_code=500, detail="Conversation manager is not initialized")

    success = await conversation_manager.delete_conversation(conversation_id)
    if success:
        return {"message": f"Conversation {conversation_id} deleted successfully"}
    else:
        raise HTTPException(status_code=404, detail=f"Conversation {conversation_id} not found")


@app.post("/conversations/{conversation_id}/chat", response_model=None)
async def chat(
    conversation_id: str,
    request: ChatRequest
) -> StreamingResponse | ChatResponse:
    """Chat endpoint that processes messages and returns assistant response.
    Supports both streaming and non-streaming modes."""

    if not conversation_manager:
        logger.error("ConversationManager not initialized")
        raise HTTPException(status_code=500, detail="Conversation manager is not initialized")

    if conversation_id in (None, "", "null"):
        raise HTTPException(status_code=400, detail="A valid conversation_id is required")

    logger.info(
        "Chat request received",
        extra={
            "conversation_id": conversation_id,
            "user_message": request.message.to_dict(),
            "stream": request.stream
        }
    )

    result = await conversation_manager.chat(
        user_message=request.message,
        conversation_id=conversation_id,
        stream=request.stream
    )

    if request.stream:
        logger.debug("Returning streaming response...")
        return StreamingResponse(
            result,
            media_type="text/event-stream"
        )
    else:
        logger.debug("Returning non-streaming response...")
        return ChatResponse(**result) # type: ignore
    

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting UMS Agent server")
    uvicorn.run(
        "agent.app:app",
         host="0.0.0.0",
         port=8011,
         log_level="debug"
    )