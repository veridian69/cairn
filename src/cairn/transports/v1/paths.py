"""Paths within `/v1` that more than one transport must agree about.

Only one entry so far, and it exists to break a real cycle rather than for
tidiness: the MCP mount renders its refusals with the REST error module,
and the REST boundary handlers must exclude the MCP path from their own
scope (I-84). Whichever of the two owned the constant, the other would
have to import it, and one direction is a cycle. The shared `/v1` package
owns it instead, which is also the honest answer to who owns the `/v1`
path map.
"""

# I-84: the MCP Streamable HTTP endpoint, inside `/v1` rather than at the
# root, so I-70's "outside /v1 only the health probes and /metrics exist"
# stays literally true.
MCP_MOUNT_PATH = "/v1/mcp"
