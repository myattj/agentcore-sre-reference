"""Shared AgentCore Runtime app instance.

Lives in its own module so that main.py, tools.py, and ping.py can all
import the same `app` without circular imports.
"""
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
