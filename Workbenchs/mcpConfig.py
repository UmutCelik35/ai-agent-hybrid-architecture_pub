from autogen_ext.tools.mcp import StdioServerParams, McpWorkbench


class McpConfig:
    
    @staticmethod
    def get_playwright_workbench():
        # command "npx" yerine "node" oldu.
        # argüman olarak lokal klasöründeki derlenmiş js dosyasını gösteriyoruz.
        playwright_server_params = StdioServerParams(
            command="npx", 
            args=["@playwright/mcp@latest"
            ],
            read_timeout_seconds=40
        )
        return McpWorkbench(playwright_server_params)
