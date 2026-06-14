# CLI Coverage

### Overview
cli.py provides the Command Line Interface for Darwin using the typer library. It exposes verbs like run, price, and verify so humans can interact with and use Darwin from the terminal.

### Explanation
Similarly to mcp_server.py, cli.py functions as a router. When humans query a request through terminal commands, the file determines which handlers and imports to use for the task. Additionally, it parses human input (arguments and commands) and presents outputs using customized visuals.

### The Gap
The "run" verb is successfully covered, but the other verbs do not have coverage. Additionally, since the middleman is crucial in correctly processing user requests and visualizing outputs, it is important that test cases are added for whether the interface handles errors cleanly and whether the outputs are human-readable.

### Links
* **Source Module:** [cli.py](../../darwin/agenticcloud/cli.py)
* **Test File:** [tests/cli](../../tests/cli)