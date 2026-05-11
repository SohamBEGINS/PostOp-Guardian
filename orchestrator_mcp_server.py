"""
Orchestrator MCP Server — Patient Progress Logger
Provides two tools:
1. read_progress_notes  — called at the START of each session to recap previous days.
2. log_daily_progress   — called at the END of each session to persist today's record.

Persists data to a local JSON file: patient_progress_log.json
"""
import os
import json
import anyio
from datetime import datetime, timezone
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.transport_security import TransportSecuritySettings
import mcp.types as types
from mcp.server.session import ServerSession, InitializationState

# Monkey-patch ServerSession to bypass initialization checks for stateless mode
_original_session_init = ServerSession.__init__
def _stateless_session_init(self, *args, **kwargs):
    _original_session_init(self, *args, **kwargs)
    self._initialization_state = InitializationState.Initialized
ServerSession.__init__ = _stateless_session_init

SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# ── Log file path ──
LOG_FILE = os.path.join(os.path.dirname(__file__), "patient_progress_log.json")


# ──────────────────────────────────────────────
# 1. Tool logic (pure async functions)
# ──────────────────────────────────────────────

def _load_log() -> dict:
    """Loads the progress log from disk. Returns empty structure if not found."""
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"WARN: Could not read log file — {e}. Starting fresh.")
    return {"patients": {}}


def _save_log(data: dict) -> None:
    """Persists the progress log to disk."""
    with open(LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


async def _read_progress_notes(patient_id: str) -> str:
    """Reads all previous daily progress entries for a patient."""
    if not patient_id or patient_id.strip() == "":
        return "ERROR: patient_id is required to read progress notes."

    data = _load_log()
    patient_logs = data.get("patients", {}).get(patient_id, [])

    if not patient_logs:
        return (
            f"No previous progress notes found for patient '{patient_id}'. "
            f"This appears to be the patient's first check-in session."
        )

    lines = [f"═══ PREVIOUS PROGRESS NOTES FOR PATIENT: {patient_id} ═══\n"]
    for entry in patient_logs[-7:]:  # show last 7 days max to keep context short
        lines.append(f"── Day {entry.get('post_op_day', '?')} ({entry.get('date', '?')}) ──")
        lines.append(f"  Status:           {entry.get('patient_status', 'UNKNOWN')}")
        lines.append(f"  Symptoms:         {', '.join(entry.get('reported_symptoms', ['None reported']))}")
        lines.append(f"  Specialist Called: {entry.get('agent_consulted', 'None')}")
        lines.append(f"  Action Taken:     {entry.get('action_taken', 'No action recorded')}")
        lines.append("")

    lines.append("[INSTRUCTION: You have reviewed the patient's history. Now begin today's check-in.]")
    result = "\n".join(lines)
    print(f"INFO:  read_progress_notes — returned {len(patient_logs)} entries for patient {patient_id}")
    return result


async def _log_daily_progress(
    patient_id: str,
    post_op_day: int,
    patient_status: str,
    reported_symptoms: list,
    agent_consulted: str,
    action_taken: str
) -> str:
    """Logs a structured daily progress entry for a patient. Call this at the END of every session."""
    if not patient_id or patient_id.strip() == "":
        return "ERROR: patient_id is required to log progress."

    # Validate status
    valid_statuses = {"RED", "AMBER", "GREEN"}
    status = patient_status.upper().strip()
    if status not in valid_statuses:
        status = "GREEN"  # safe default

    entry = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "post_op_day": post_op_day,
        "patient_status": status,
        "reported_symptoms": reported_symptoms if isinstance(reported_symptoms, list) else [reported_symptoms],
        "agent_consulted": agent_consulted or "None",
        "action_taken": action_taken or "Standard monitoring."
    }

    data = _load_log()
    if patient_id not in data["patients"]:
        data["patients"][patient_id] = []

    data["patients"][patient_id].append(entry)
    _save_log(data)

    print(f"INFO:  log_daily_progress — saved Day {post_op_day} entry for patient {patient_id} | Status: {status}")
    return (
        f"✓ Daily progress logged successfully for patient '{patient_id}'.\n"
        f"  Post-Op Day: {post_op_day}\n"
        f"  Status: {status}\n"
        f"  Symptoms: {', '.join(entry['reported_symptoms'])}\n"
        f"  Action: {action_taken}\n\n"
        f"[INSTRUCTION: This is your LAST tool call. Now say goodbye to the patient warmly.]"
    )


# ──────────────────────────────────────────────
# 2. Server factory — new instance per request
# ──────────────────────────────────────────────
def _create_server(headers: dict) -> Server:
    server = Server("OrchestratorProgressTools", version="1.0.0")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="read_progress_notes",
                description=(
                    "Call this at the START of every patient session to review their recovery history. "
                    "Returns all previous daily entries so you can track trends (e.g., worsening pain over 3 days)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "The patient's FHIR ID or unique identifier (e.g., 'tyrone_ram')."
                        }
                    },
                    "required": ["patient_id"]
                }
            ),
            types.Tool(
                name="log_daily_progress",
                description=(
                    "Call this as the VERY LAST action of every session, before saying goodbye. "
                    "Persists a structured daily progress note for the patient."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "The patient's FHIR ID or unique identifier."
                        },
                        "post_op_day": {
                            "type": "integer",
                            "description": "The number of days since surgery (e.g., 10)."
                        },
                        "patient_status": {
                            "type": "string",
                            "enum": ["RED", "AMBER", "GREEN"],
                            "description": "Overall status from today's session. RED = urgent, AMBER = monitor, GREEN = normal."
                        },
                        "reported_symptoms": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of symptoms the patient reported today (e.g., ['calf pain', 'swelling'])."
                        },
                        "agent_consulted": {
                            "type": "string",
                            "description": "Which specialist agent was consulted today, if any (e.g., 'TKA_Specialist', 'CABG_Specialist', 'None')."
                        },
                        "action_taken": {
                            "type": "string",
                            "description": "Brief summary of what action was recommended or taken (e.g., 'Ordered duplex ultrasound. Instructed to stop Ibuprofen.')."
                        }
                    },
                    "required": ["patient_id", "post_op_day", "patient_status", "reported_symptoms", "agent_consulted", "action_taken"]
                }
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        if name == "read_progress_notes":
            result = await _read_progress_notes(arguments.get("patient_id", ""))
        elif name == "log_daily_progress":
            result = await _log_daily_progress(
                patient_id=arguments.get("patient_id", ""),
                post_op_day=arguments.get("post_op_day", 0),
                patient_status=arguments.get("patient_status", "GREEN"),
                reported_symptoms=arguments.get("reported_symptoms", []),
                agent_consulted=arguments.get("agent_consulted", "None"),
                action_taken=arguments.get("action_taken", "")
            )
        else:
            result = f"Unknown tool: {name}"
        return [types.TextContent(type="text", text=result)]

    return server


def _build_init_options(server: Server):
    opts = server.create_initialization_options()
    extra = dict(opts.capabilities)
    extra["extensions"] = {
        "ai.promptopinion/fhir-context": {
            "scopes": []  # Orchestrator MCP doesn't need FHIR scopes — it's a local logger
        }
    }
    from mcp.types import ServerCapabilities
    opts.capabilities = ServerCapabilities(**extra)
    return opts


# ──────────────────────────────────────────────
# 3. Per-request handler
# ──────────────────────────────────────────────
async def _handle_mcp_request(headers: dict, scope, receive, send):
    server = _create_server(headers)
    transport = StreamableHTTPServerTransport(
        mcp_session_id=None,
        is_json_response_enabled=True,
        security_settings=SECURITY,
    )
    init_opts = _build_init_options(server)

    async with anyio.create_task_group() as tg:
        async with transport.connect() as (read_stream, write_stream):
            async def run_server():
                await server.run(read_stream, write_stream, init_opts)

            tg.start_soon(run_server)
            await transport.handle_request(scope, receive, send)
            tg.cancel_scope.cancel()


# ──────────────────────────────────────────────
# 4. ASGI app
# ──────────────────────────────────────────────
async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    if scope["type"] != "http":
        return

    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    path = scope.get("path", "")
    method = scope.get("method", "")

    if path == "/mcp":
        if method == "POST":
            try:
                await _handle_mcp_request(headers, scope, receive, send)
            except Exception as e:
                print(f"ERROR: {e}")
        else:
            await send({
                "type": "http.response.start", "status": 405,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"Method Not Allowed"})

    elif path == "/" and method == "GET":
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({"type": "http.response.body", "body": b'{"status":"ok","service":"OrchestratorProgressLogger"}'})
    else:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})


if __name__ == "__main__":
    import uvicorn
    # Port 8006 — Orchestrator Progress Logger
    # Recovery Coach = 8004, Clinical Assessor = 8005
    uvicorn.run(app, host="0.0.0.0", port=8006)
