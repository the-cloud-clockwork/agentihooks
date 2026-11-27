"""One MCP endpoint in front of one Serena backend per git worktree.

A Serena process has a single active project for every client it serves, so
sessions working in different worktrees cannot share one. The router keeps a
binding per MCP session and forwards each call to the backend of the root the
call carries, else of the session's bound root, because the parent and sub
agents of one Claude session share a single MCP session.
"""
