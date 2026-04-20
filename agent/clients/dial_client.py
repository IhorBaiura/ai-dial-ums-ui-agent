import json
import logging
from collections import defaultdict
from typing import Any, AsyncGenerator, Iterable, List, cast

from openai import AsyncAzureOpenAI, AsyncStream
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessageParam, ChatCompletionToolUnionParam
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall

from agent.clients.stdio_mcp_client import StdioMCPClient
from agent.models.message import Message, Role
from agent.clients.http_mcp_client import HttpMCPClient

logger = logging.getLogger(__name__)


class DialClient:
    """Handles AI model interactions and integrates with MCP client"""

    def __init__(
            self,
            api_key: str,
            endpoint: str,
            model: str,
            tools: list[dict[str, Any]],
            tool_name_client_map: dict[str, HttpMCPClient | StdioMCPClient]
    ):
        self.tools: Iterable[ChatCompletionToolUnionParam] = cast(Iterable[ChatCompletionToolUnionParam], tools)
        self.tool_name_client_map = tool_name_client_map
        self.model = model
        self.async_openai = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=""
        )

        logger.info(
            "DialClient initialized",
            extra={
                "model": model,
                "endpoint": endpoint,
                "tool_count": len(tools)
            }
        )

    async def response(self, messages: list[Message]) -> Message:
        """Non-streaming completion with tool calling support"""

        logger.debug(
            "Creating non-streaming completion",
            extra={"message_count": len(messages), "model": self.model}
        )

        response: ChatCompletion = await self.async_openai.chat.completions.create(
            model=self.model,
            messages=cast(Iterable[ChatCompletionMessageParam], [msg.to_dict() for msg in messages]),
            tools=self.tools,
            temperature=0.0,
            stream=False
        )

        ai_message = Message(
            role=Role.ASSISTANT,
            content=response.choices[0].message.content,
        )

        if hasattr(response.choices[0].message, "tool_calls"):
            ai_message.tool_calls = cast(list[dict[str, Any]], response.choices[0].message.tool_calls)
            logger.debug(
                "AI message contains tool calls",
                extra={
                    "model": self.model,
                    "tool_calls": response.choices[0].message.tool_calls
                }
            )

        if ai_message.tool_calls:
            messages.append(ai_message)
            await self._call_tools(ai_message, messages)
            return await self.response(messages)
        
        logger.debug("Non-streaming completion finished")
        return ai_message

    async def stream_response(self, messages: list[Message]) -> AsyncGenerator[str, None]:
        """
        Streaming completion with tool calling support.
        Yields SSE-formatted chunks.
        """

        stream: AsyncStream[ChatCompletionChunk] = await self.async_openai.chat.completions.create(
            model=self.model,
            messages=cast(Iterable[ChatCompletionMessageParam], [msg.to_dict() for msg in messages]),
            tools=self.tools,
            temperature=0.0,
            stream=True
        )

        content_buffer = ""
        tool_deltas: List[ChoiceDeltaToolCall] = []
    
        async for chunk in stream:
            delta = chunk.choices[0].delta

            if hasattr(delta, "content") and delta.content:
                chunk_data: dict[str, Any] = {
                    "choices": [
                        {
                            "delta": {"content": delta.content},
                            "index": 0,
                            "finish_reason": None
                        }
                    ]
                }
                yield f"data: {json.dumps(chunk_data)}\n\n"
                content_buffer += delta.content

            if hasattr(delta, "tool_calls") and delta.tool_calls:
                tool_deltas.extend(delta.tool_calls)

        if tool_deltas:
            tool_calls = self._collect_tool_calls(tool_deltas)
            logger.info(
                "Streaming response includes tool calls",
                extra={"tool_call_count": len(tool_calls)}
            )

            ai_message = Message(
                role=Role.ASSISTANT,
                content=content_buffer,
                tool_calls=tool_calls
            )

            messages.append(ai_message)
            await self._call_tools(ai_message, messages)

            async for chunk in self.stream_response(messages):
                yield chunk
            return

        messages.append(Message(role=Role.ASSISTANT, content=content_buffer))

        logger.debug("Streaming completion finished")
        final_chunk: dict[str, Any] = {
            "choices": [ {
                "delta": {},
                "index": 0,
                "finish_reason": "stop"
            }]
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    def _collect_tool_calls(self, tool_deltas: List[ChoiceDeltaToolCall]) -> list[dict[str, Any]]:
        """Convert streaming tool call deltas to complete tool calls"""

        tool_dict: defaultdict[int, dict[str, Any]] = defaultdict(lambda: {"id": None, "function": {"arguments": "", "name": None}, "type": None})

        for delta in tool_deltas:
            idx = delta.index
            if delta.id:
                tool_dict[idx]["id"] = delta.id
            if delta.function and delta.function.name:
                tool_dict[idx]["function"]["name"] = delta.function.name
            if delta.function and delta.function.arguments:
                tool_dict[idx]["function"]["arguments"] += delta.function.arguments
            if delta.type:
                tool_dict[idx]["type"] = delta.type

            logger.debug(
                f"chanked tool delta: index={idx}",
                extra={"tool_dict": tool_dict}
            )

        collected_tools = list(tool_dict.values())
        logger.debug(
            "Collected tool calls from deltas",
            extra={"tool_count": len(collected_tools)}
        )
        return collected_tools

    async def _call_tools(self, ai_message: Message, messages: list[Message], silent: bool = False):
        """Execute tool calls using MCP client"""

        for tool_call in ai_message.tool_calls or []:
            tool_name = tool_call["function"]["name"]
            tool_args = json.loads(tool_call["function"]["arguments"])
            mcp_client = self.tool_name_client_map.get(tool_name)

            if not mcp_client:
                error_message = f"Unable to call {tool_name}. MCP client not found."
                logger.error(error_message, extra={"tool_name": tool_name})
                messages.append(Message(role=Role.TOOL, content=error_message, tool_call_id=tool_call["id"]))
                continue

            if not silent:
                logger.info(
                    "Calling tool",
                    extra={"tool_name": tool_name, "tool_args": tool_args}
                )

            try:
                tool_result = await mcp_client.call_tool(tool_name, tool_args)
                messages.append(Message(role=Role.TOOL, content=str(tool_result), tool_call_id=tool_call["id"]))
                logger.info(
                    "Tool call successful",
                    extra={
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "tool_result": tool_result
                    }
                )
            except Exception as e:
                error_message = f"Error calling tool {tool_name}: {str(e)}"
                logger.error(error_message, extra={"tool_name": tool_name, "tool_args": tool_args})
                messages.append(Message(role=Role.TOOL, content=error_message, tool_call_id=tool_call["id"]))
