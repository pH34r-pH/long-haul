import json
import os

import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.generic_trigger(
    arg_name="context",
    type="mcpToolTrigger",
    toolName="identity_whoami",
    description="Return the transport-authenticated Long Haul MCP principal.",
    toolProperties="[]",
)
def identity_whoami(context) -> str:
    """Authentication spike only; EasyAuth is the enforcement boundary.

    Azure App Service Authentication injects authenticated identity metadata.
    The Function never accepts a caller-selected crew identity as a tool arg.
    """
    principal = {
        "authenticated": True,
        "provider": os.environ.get("LONG_HAUL_AUTH_PROVIDER", "entra"),
        "server_client_id": os.environ.get("LONG_HAUL_MCP_SERVER_CLIENT_ID"),
        "phase": "auth-spike",
    }
    return json.dumps(principal)


@app.generic_trigger(
    arg_name="context",
    type="mcpToolTrigger",
    toolName="capabilities_inspect",
    description="Inspect the harmless capabilities exposed during the auth spike.",
    toolProperties="[]",
)
def capabilities_inspect(context) -> str:
    return json.dumps(
        {
            "capabilities": ["identity.whoami", "capabilities.inspect"],
            "reference_vessel_authority": False,
        }
    )
