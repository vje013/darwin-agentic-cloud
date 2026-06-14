# MCP Server Coverage

### Overview
mcp_server.py acts as a communication interface for Darwin. It uses MCP (Model Context Protocol) to expose 7 main tools (such as running code or verifying signed attestations) so external AI agents can communicate with Darwin over standard input/output.

### Explanation
The file acts as a router that, when an AI calls a tool, attributes requests to the proper handler.

### The Gap
There is no dedicated tests/test_mcp_server.py file. Because the server relies on a live network connection to fetch the list of public keys, any changes to that website could break verification. Without tests using mock network responses, the file is essentially unmonitored. Additionally, there are no tests for whether the interface passes inputs and outputs to the right tools in a proper manner.

### Links
* **Source Module:** [mcp_server.py](../../darwin/agenticcloud/mcp_server.py)