from autogen_agentchat.agents import AssistantAgent
from Workbenchs.mcpConfig import McpConfig

class AgentFactory:
    def __init__(self, model_client):
        self.model_client = model_client

    def create_playwright_agent(self, system_message):
        playwright_automation = AssistantAgent(
            name="PlaywrightAutomationAgent",
            model_client=self.model_client,
            workbench=McpConfig.get_playwright_workbench(),
            system_message=system_message
        )
        return playwright_automation

    
