import json
import logging
import os
import uuid
from datetime import datetime, UTC
from typing import Any, Optional, AsyncGenerator, cast

import redis.asyncio as redis

from agent.clients.dial_client import DialClient
from agent.models.message import Message, Role
from agent.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

CONVERSATION_PREFIX = "conversation:"
CONVERSATION_LIST_KEY = "conversations:list"


class ConversationManager:
    """Manages conversation lifecycle including AI interactions and persistence"""

    def __init__(self, dial_client: DialClient, redis_client: redis.Redis):
        self.dial_client = dial_client
        self.redis = redis_client
        logger.info("ConversationManager initialized")

    async def create_conversation(self, title: Optional[str] = None) -> dict[str, Any]:
        """Create a new conversation"""

        conversation_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        conversation: dict[str, Any] = {
            "id": conversation_id,
            "title": title,
            "messages": [],
            "created_at": now,
            "updated_at": now
        }

        await self.redis.set(name=f"{CONVERSATION_PREFIX}{conversation_id}", value=json.dumps(conversation)) # type: ignore
        await self.redis.zadd(name=CONVERSATION_LIST_KEY, mapping={conversation_id: datetime.now(UTC).timestamp()}) # type: ignore

        logger.info(
            "Conversation created",
            extra={
                "conversation_id": conversation_id,
                "title": conversation["title"]
            }
        )
        return conversation

    async def list_conversations(self) -> list[dict[str, Any]]:
        """List all conversations sorted by last update time"""
        logger.debug("Listing all conversations")
        conversation_ids: list[str] = cast(list[str], await self.redis.zrevrange(CONVERSATION_LIST_KEY, 0, -1)) # type: ignore
        conversations: list[dict[str, Any]] = []

        for conversation_id in conversation_ids:
            conversation_data: str = cast(str, await self.redis.get(f"{CONVERSATION_PREFIX}{conversation_id}")) # type: ignore
            if conversation_data:
                conversation = json.loads(conversation_data)
                conversations.append({
                    "id": conversation["id"],
                    "title": conversation["title"],
                    "created_at": conversation["created_at"],
                    "updated_at": conversation["updated_at"],
                    "message_count": len(conversation["messages"])
                })
        
        logger.info(
            "Listed conversations",
            extra={
                "conversation_count": len(conversations)            }
        )
        return conversations    

    async def get_conversation(self, conversation_id: str) -> Optional[dict[str, Any]]:
        """Get a specific conversation"""
        conversation_data = cast(str, await self.redis.get(f"{CONVERSATION_PREFIX}{conversation_id}")) # type: ignore
        if not conversation_data:
            logger.warning(
                "Conversation not found",
                extra={"conversation_id": conversation_id}
            )
            return None

        conversation = json.loads(conversation_data)

        logger.info(
            "Retrieved conversation",
            extra={
                "conversation_id": conversation_id,
                "title": conversation["title"],
                "message_count": len(conversation["messages"])
            }
        )

        return conversation

    async def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation"""
        logger.info("Deleting conversation", extra={"conversation_id": conversation_id})
        deleted_count = await self.redis.delete(f"{CONVERSATION_PREFIX}{conversation_id}") # type: ignore

        if deleted_count == 0:
            logger.warning(
                "Attempted to delete non-existent conversation",
                extra={"conversation_id": conversation_id}
            )
            return False
        
        await self.redis.zrem(CONVERSATION_LIST_KEY, conversation_id) # type: ignore

        logger.info(
            "Conversation deleted",
            extra={"conversation_id": conversation_id}
        )
        
        return True

    async def chat(
            self,
            user_message: Message,
            conversation_id: str,
            stream: bool = False
    ) -> AsyncGenerator[str, None] | dict[str, Any]:
        """
        Process chat messages and return AI response.
        Automatically saves conversation state.
        """
        logger.debug(
            "Chat requested",
            extra={
                "conversation_id": conversation_id,
                "user_message": user_message.content,
                "stream": stream
            }
        )

        conversation = await self.get_conversation(conversation_id)
        if not conversation:
            error_message = f"Conversation with id {conversation_id} not found"
            logger.error(error_message, extra={"conversation_id": conversation_id})
            raise ValueError(error_message)
        
        messages_data: list[dict[str, Any]] = conversation.get("messages", [])
        messages: list[Message] = [Message(**msg_data) for msg_data in messages_data]
        logger.debug(
            "Loaded conversation history",
            extra={
                "conversation_id": conversation_id,
                "message_count": len(messages)
            }
        )

        if not messages:
            logger.debug("Starting new conversation, adding system prompt")
            messages.append(Message(role=Role.SYSTEM, content=os.getenv("SYSTEM_PROMPT", SYSTEM_PROMPT)))

        messages.append(user_message)

        if stream:
            return self._stream_chat(conversation_id, messages)
        else:
            return await self._non_stream_chat(conversation_id, messages)


    async def _stream_chat(
            self,
            conversation_id: str,
            messages: list[Message],
    ) -> AsyncGenerator[str, None]:
        """Handle streaming chat with automatic saving"""

        logger.debug(
            "Starting streaming chat",
            extra={
                "conversation_id": conversation_id,
                "message_count": len(messages)
            }
        )

        yield f"data: {json.dumps({'conversation_id': conversation_id})}\n\n"

        async for chunk in self.dial_client.stream_response(messages):
            yield chunk

        await self._save_conversation_messages(conversation_id, messages)

        logger.debug(
            "Finished streaming chat and saved conversation",
            extra={"conversation_id": conversation_id}
        )

    async def _non_stream_chat(
            self,
            conversation_id: str,
            messages: list[Message],
    ) -> dict[str, Any]:
        """Handle non-streaming chat"""

        logger.debug(
            "Starting non-streaming chat",
            extra={
                "conversation_id": conversation_id,
                "message_count": len(messages)
            }
        )

        ai_message: Message = await self.dial_client.response(messages)
        await self._save_conversation_messages(conversation_id, messages)

        logger.info(
            "Non-streaming chat completed",
            extra={"conversation_id": conversation_id}
        )

        return {
            "content": ai_message.content or '',
            "conversation_id": conversation_id
        }

    async def _save_conversation_messages(
            self,
            conversation_id: str,
            messages: list[Message]
    ):
        """Save or update conversation messages"""
        logger.debug(
            "Saving conversation messages",
            extra={
                "conversation_id": conversation_id,
                "message_count": len(messages)
            }
        )

        converssation = await self.get_conversation(conversation_id)
        if not converssation:
            logger.error(
                "Conversation not found when trying to save messages",
                extra={"conversation_id": conversation_id}
            )
            raise ValueError(f"Conversation with id {conversation_id} not found")
        
        converssation["messages"] = [msg.model_dump() for msg in messages]
        converssation["updated_at"] = datetime.now(UTC).isoformat()

        logger.debug("Updating existing conversation", extra={"conversation_id": conversation_id})

        await self._save_conversation(converssation)

    async def _save_conversation(self, conversation: dict[str, Any]):
        """Internal method to persist conversation to Redis"""

        conversation_id = conversation["id"]

        await self.redis.set(  # type: ignore
            name=f"{CONVERSATION_PREFIX}{conversation_id}", 
            value=json.dumps(conversation)
        )

        await self.redis.zadd( # type: ignore
            name=CONVERSATION_LIST_KEY, 
            mapping={conversation_id: datetime.now(UTC).timestamp()}
        ) 

        logger.debug(
            "Conversation persisted to Redis",
            extra={
                "conversation_id": conversation_id,
                "message_count": len(conversation.get("messages", []))
            }
        )
