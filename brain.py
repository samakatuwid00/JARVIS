# Jarvis Brain - Claude API integration with tool use
import json
from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_HISTORY
from tools import TOOLS, execute_tool


JARVIS_SYSTEM = """You are JARVIS (Just A Rather Very Intelligent System), an AI assistant inspired by Iron Man's JARVIS.

You are running on the user's computer as a voice-activated assistant. You can:
- Execute shell commands and control the system
- Read and write files
- Search the web
- Open applications
- Provide information and answer questions

Personality:
- Helpful, efficient, slightly formal but friendly
- Reference your capabilities when relevant
- Keep responses concise for voice output (avoid long lists)
- Address the user respectfully

When using tools:
- Execute commands carefully
- Report results clearly
- If a command might be destructive, warn first

Current system info will be provided in context."""


class JarvisBrain:
    """Claude-powered AI brain with tool use."""

    def __init__(self):
        if not ANTHROPIC_API_KEY:
            raise ValueError(
                "ANTHROPIC_API_KEY not set!\n"
                "Create .env file with:\n"
                "ANTHROPIC_API_KEY=your-key-here"
            )

        self.client = Anthropic(api_key=ANTHROPIC_API_KEY)
        self.model = CLAUDE_MODEL
        self.conversation = []
        self.system_prompt = JARVIS_SYSTEM

    def think(self, user_input: str) -> str:
        """Process user input and return response, using tools as needed."""
        # Add user message
        self.conversation.append({
            "role": "user",
            "content": user_input
        })

        # Trim history
        if len(self.conversation) > MAX_HISTORY * 2:
            self.conversation = self.conversation[-(MAX_HISTORY * 2):]

        # Keep thinking with tools until we get a final text response
        max_iterations = 10
        for _ in range(max_iterations):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=self.system_prompt,
                tools=TOOLS,
                messages=self.conversation
            )

            # Check if we need to execute tools
            if response.stop_reason == "tool_use":
                # Collect tool calls and their results
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result
                        })

                # Add assistant message and tool results to conversation
                self.conversation.append({
                    "role": "assistant",
                    "content": response.content
                })
                self.conversation.append({
                    "role": "user",
                    "content": tool_results
                })
                continue

            else:
                # Got a final text response
                text = response.content[0].text if response.content else ""
                self.conversation.append({
                    "role": "assistant",
                    "content": text
                })
                return text

        return "I apologize, but I encountered an issue processing that request."

    def reset(self):
        """Clear conversation history."""
        self.conversation = []
        return "Memory cleared. Ready for new commands."
