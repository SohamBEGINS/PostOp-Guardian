"""
Shared MCP Server between specialist agents
Provides real-time grounding via OpenFDA and PubMed E-utilities.
"""
import os
import httpx
import anyio
import xml.etree.ElementTree as ET
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


# ──────────────────────────────────────────────
# 1. Tool logic (pure async functions)
# ──────────────────────────────────────────────

# ── Result cache: prevents infinite retry loops ──
_result_cache: dict[str, str] = {}

async def _check_drug_interactions(drug1: str, drug2: str) -> str:
    """Queries OpenFDA for adverse events involving both drugs."""
    # ── Duplicate call detection ──
    cache_key = f"fda:{drug1.lower().strip()}:{drug2.lower().strip()}"
    if cache_key in _result_cache:
        return (
            f"[CACHED RESULT — DO NOT call this tool again with the same inputs.]\n\n"
            f"{_result_cache[cache_key]}\n\n"
            f"You already have this data. Use it to write your final risk assessment report NOW."
        )

    # ── Input validation: prevent garbage queries that cause infinite retries ──
    invalid_terms = {"unknown", "n/a", "none", "not specified", ""}
    if drug1.strip().lower() in invalid_terms or drug2.strip().lower() in invalid_terms:
        return (
            f"ERROR: Cannot check drug interactions — one or both drug names are missing. "
            f"Received drug1='{drug1}', drug2='{drug2}'. "
            f"You MUST provide two specific generic drug names "
            f"Ask the Orchestrator for the patient's medication list first."
        )
    
    # Also reject if someone passes symptom text instead of a drug name
    if len(drug1.split()) > 3 or len(drug2.split()) > 3:
        return (
            f"ERROR: Drug names should be 1-3 words (e.g., 'ibuprofen', 'warfarin'). "
            f"Received drug1='{drug1}', drug2='{drug2}'. These look like symptom descriptions, not drug names."
        )

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            # Query OpenFDA for reports where BOTH drugs were co-administered, and count the reactions
            url = "https://api.fda.gov/drug/event.json"
            params = {
                "search": f'patient.drug.openfda.generic_name:"{drug1}"+AND+patient.drug.openfda.generic_name:"{drug2}"',
                "count": "patient.reaction.reactionmeddrapt.exact"
            }
            
            # Use raw string replacement to avoid URL encoding of the +AND+ part
            query_string = f"?search=patient.drug.openfda.generic_name:\"{drug1}\"+AND+patient.drug.openfda.generic_name:\"{drug2}\"&count=patient.reaction.reactionmeddrapt.exact"
            
            print(f"INFO:  Querying OpenFDA: {drug1} + {drug2}")
            resp = await client.get(url + query_string)
            
            if resp.status_code == 404:
                return f"No adverse interaction records found in FDA database for {drug1} and {drug2} together."
            
            if resp.status_code != 200:
                return f"OpenFDA Error: {resp.status_code} - {resp.text}"
            
            data = resp.json()
            results = data.get("results", [])
            
            if not results:
                return f"No specific reaction counts found for {drug1} and {drug2}."
            
            # Return the top 10 adverse events — raw, uncategorized
            top_results = results[:10]
            total_reports = sum(r.get("count", 0) for r in results)
            
            lines = [f"FDA ADVERSE EVENT DATA: {total_reports} total reports involving {drug1} and {drug2}."]
            lines.append("NOTE: These are population-level co-reported events from the FDA FAERS database. They reflect what was observed globally when both drugs were taken together — they are NOT a patient-specific diagnosis.\n")
            lines.append("Top reported adverse events:")
            for i, r in enumerate(top_results, 1):
                lines.append(f"  {i}. {r.get('term')}: {r.get('count', 0)} reports")
            
            lines.append(f"\n(Showing top 10 of {len(results)} distinct event types)")
            lines.append("\n[INSTRUCTION: You now have the drug interaction data. Do NOT call this tool again. Once you have both OpenFDA and PubMed data, write your FINAL clinical risk assessment report to the Orchestrator. Do not just parrot the FDA stats — perform a real differential diagnosis.]")
                
            result = "\n".join(lines)
            _result_cache[cache_key] = result
            return result
            
        except httpx.TimeoutException:
            return "OpenFDA timeout: Proceed with clinical judgment."
        except Exception as e:
            return f"OpenFDA Error: {e}"


async def _search_complications(symptoms: str, surgery_type: str) -> str:
    """Queries PubMed for literature on the specific symptoms post-surgery."""
    # ── Duplicate call detection ──
    cache_key = f"pubmed:{symptoms.lower().strip()}:{surgery_type.lower().strip()}"
    if cache_key in _result_cache:
        return (
            f"[CACHED RESULT — DO NOT call this tool again with the same inputs.]\n\n"
            f"{_result_cache[cache_key]}\n\n"
            f"You already have this data. Use it to write your final risk assessment report NOW."
        )

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            # Step 1: esearch
            term = f"{symptoms} {surgery_type}".replace(" ", "+")
            search_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={term}&retmode=json&retmax=3"
            print(f"INFO:  Querying PubMed: {term}")
            
            s_resp = await client.get(search_url)
            if s_resp.status_code != 200:
                return f"PubMed Search Error: {s_resp.status_code}"
                
            id_list = s_resp.json().get("esearchresult", {}).get("idlist", [])
            if not id_list:
                return "No medical literature found matching these symptoms and surgery."
                
            # Step 2: efetch
            ids = ",".join(id_list)
            fetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={ids}&retmode=xml"
            f_resp = await client.get(fetch_url)
            
            if f_resp.status_code != 200:
                return f"PubMed Fetch Error: {f_resp.status_code}"
                
            # Parse XML
            root = ET.fromstring(f_resp.text)
            articles = root.findall(".//PubmedArticle")
            
            results = []
            for article in articles:
                title_elem = article.find(".//ArticleTitle")
                title = title_elem.text if title_elem is not None else "Unknown Title"
                
                # Try to find a conclusion
                abstract_texts = article.findall(".//AbstractText")
                conclusion = None
                
                for at in abstract_texts:
                    if at.get("Label", "").upper() == "CONCLUSION":
                        conclusion = at.text
                        break
                        
                # Fallback: Lead and Final sentence
                if not conclusion and abstract_texts:
                    full_abstract = " ".join(at.text for at in abstract_texts if at.text)
                    sentences = [s.strip() for s in full_abstract.split(". ") if s.strip()]
                    if len(sentences) >= 2:
                        conclusion = f"{sentences[0]}. ... {sentences[-1]}."
                    elif sentences:
                        conclusion = f"{sentences[0]}."
                        
                if not conclusion:
                    conclusion = "No conclusion available."
                    
                results.append(f"Title: {title}\nFinding: {conclusion}")
                
            result = "\n\n".join(results)
            result += "\n\n[INSTRUCTION: You now have the PubMed literature data. Do NOT call this tool again. Proceed to write your risk assessment report.]"
            _result_cache[cache_key] = result
            return result
            
        except httpx.TimeoutException:
            return "PubMed literature search timed out. Proceed with available safety data."
        except Exception as e:
            return f"PubMed Error: {e}"


# ──────────────────────────────────────────────
# 2. Server factory — new instance per request
# ──────────────────────────────────────────────
def _create_server(headers: dict) -> Server:
    server = Server("ClinicalAssessorTools", version="1.0.1")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="check_drug_interactions",
                description="Checks FDA adverse event databases for severe interactions between two medications.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "drug1": {"type": "string", "description": "First drug generic name (e.g., ibuprofen)"},
                        "drug2": {"type": "string", "description": "Second drug generic name (e.g., warfarin)"}
                    },
                    "required": ["drug1", "drug2"]
                }
            ),
            types.Tool(
                name="search_complications",
                description="Searches PubMed medical literature for known complications matching symptoms and surgery.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symptoms": {"type": "string", "description": "Patient symptoms (e.g., calf pain)"},
                        "surgery_type": {"type": "string", "description": "Surgery performed (e.g., total knee arthroplasty)"}
                    },
                    "required": ["symptoms", "surgery_type"]
                }
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        if name == "check_drug_interactions":
            result = await _check_drug_interactions(arguments.get("drug1", ""), arguments.get("drug2", ""))
        elif name == "search_complications":
            result = await _search_complications(arguments.get("symptoms", ""), arguments.get("surgery_type", ""))
        else:
            result = f"Unknown tool: {name}"
        return [types.TextContent(type="text", text=result)]

    return server


def _build_init_options(server: Server):
    opts = server.create_initialization_options()
    # We do not strictly need FHIR capabilities for the assessor, but keep the template structure intact.
    extra = dict(opts.capabilities)
    extra["extensions"] = {
        "ai.promptopinion/fhir-context": {
            "scopes": []
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
        await send({"type": "http.response.body", "body": b'{"status":"ok"}'})
    else:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})


if __name__ == "__main__":
    import uvicorn
    # Important: Run on port 8005 so it doesn't clash with Recovery Coach on 8004
    uvicorn.run(app, host="0.0.0.0", port=8005)