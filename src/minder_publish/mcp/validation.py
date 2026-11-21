"""Keep rejected tool arguments out of model-visible errors and server logs."""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError


class PublishMCPServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as error:
            if isinstance(error.__cause__, ValidationError):
                # SDK validation text includes rejected values, which may contain
                # signed file URLs or passwords. The advertised schema remains the
                # source of field requirements; deliberate domain errors keep theirs.
                raise ToolError(
                    '{"code":"invalid_tool_arguments",'
                    '"detail":"Tool arguments do not match the advertised input schema.",'
                    '"action":"Check required fields and types in the tool schema; keep credentials out of arguments."}'
                ) from None
            raise
