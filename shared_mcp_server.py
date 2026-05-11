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

async def _check_drug_interactions(
    drug1: str, drug2: str,
    patient_age: int | None = None,
    patient_sex: str | None = None,
    conditions: str | None = None
) -> str:
    """Queries OpenFDA for adverse events involving both drugs, optionally filtered by demographics."""
    # ── Duplicate call detection ──
    cache_key = f"fda:{drug1.lower().strip()}:{drug2.lower().strip()}:{patient_sex}:{patient_age}"
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
            f"You MUST provide two specific generic drug names. "
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
            url = "https://api.fda.gov/drug/event.json"
            
            # ── Build demographic-aware search query ──
            search_parts = [
                f'patient.drug.openfda.generic_name:"{drug1}"',
                f'patient.drug.openfda.generic_name:"{drug2}"',
            ]
            
            # OpenFDA patient sex codes: 1 = Male, 2 = Female
            sex_label = None
            if patient_sex:
                sex_normalized = patient_sex.strip().lower()
                if sex_normalized in ("male", "m"):
                    search_parts.append("patient.patientsex:1")
                    sex_label = "Male"
                elif sex_normalized in ("female", "f"):
                    search_parts.append("patient.patientsex:2")
                    sex_label = "Female"
            
            # If conditions are provided, add drug indication filter for the most
            # relevant condition (the first one listed).
            indication_used = None
            if conditions:
                primary_condition = conditions.split(",")[0].strip()
                if primary_condition and primary_condition.lower() not in invalid_terms:
                    search_parts.append(f'patient.drug.drugindication:"{primary_condition}"')
                    indication_used = primary_condition
            
            query_search = "+AND+".join(search_parts)
            query_string = f"?search={query_search}&count=patient.reaction.reactionmeddrapt.exact"
            
            # ── Describe the query being made ──
            demo_desc = f"{drug1} + {drug2}"
            if sex_label:
                demo_desc += f" | sex={sex_label}"
            if patient_age:
                demo_desc += f" | age~{patient_age}"
            if indication_used:
                demo_desc += f" | indication={indication_used}"
            print(f"INFO:  Querying OpenFDA (demographic-aware): {demo_desc}")
            
            resp = await client.get(url + query_string)
            
            # ── Fallback: if the narrow query returns nothing, retry without demographics ──
            narrowed = bool(sex_label or indication_used)
            if resp.status_code == 404 and narrowed:
                print("INFO:  Narrow query returned 404 — falling back to broad query.")
                broad_search = f'patient.drug.openfda.generic_name:"{drug1}"+AND+patient.drug.openfda.generic_name:"{drug2}"'
                broad_qs = f"?search={broad_search}&count=patient.reaction.reactionmeddrapt.exact"
                resp = await client.get(url + broad_qs)
                narrowed = False  # mark that we fell back
            
            if resp.status_code == 404:
                return f"No adverse interaction records found in FDA database for {drug1} and {drug2} together."
            
            if resp.status_code != 200:
                return f"OpenFDA Error: {resp.status_code} - {resp.text}"
            
            data = resp.json()
            results = data.get("results", [])
            
            if not results:
                return f"No specific reaction counts found for {drug1} and {drug2}."
            
            # Return the top 10 adverse events
            top_results = results[:10]
            total_reports = sum(r.get("count", 0) for r in results)
            
            # ── Build a context-rich header ──
            lines = [f"FDA ADVERSE EVENT DATA: {total_reports} total reports involving {drug1} and {drug2}."]
            if narrowed:
                filter_desc = []
                if sex_label:
                    filter_desc.append(f"sex={sex_label}")
                if indication_used:
                    filter_desc.append(f"indication={indication_used}")
                lines.append(f"DEMOGRAPHIC FILTER APPLIED: {', '.join(filter_desc)}. These results are narrowed to patients matching this profile.")
            else:
                lines.append("NOTE: These are population-level co-reported events from the FDA FAERS database.")
            
            if patient_age:
                lines.append(f"PATIENT AGE CONTEXT: {patient_age} years old. Weigh age-related pharmacokinetic risks (renal clearance, hepatic metabolism) when interpreting these counts.")
            if conditions and not indication_used:
                lines.append(f"COMORBIDITY CONTEXT: {conditions}. Consider how these pre-existing conditions may amplify adverse event risk.")
            lines.append("")
            
            lines.append("Top reported adverse events:")
            for i, r in enumerate(top_results, 1):
                lines.append(f"  {i}. {r.get('term')}: {r.get('count', 0)} reports")
            
            lines.append(f"\n(Showing top 10 of {len(results)} distinct event types)")
            lines.append("\n[INSTRUCTION: You now have the drug interaction data. Do NOT call this tool again. Once you have both OpenFDA and PubMed data, write your FINAL clinical risk assessment report to the Orchestrator. Do not just parrot the FDA stats — perform a real differential diagnosis that accounts for the patient's specific demographics and comorbidities.]")
                
            result = "\n".join(lines)
            _result_cache[cache_key] = result
            return result
            
        except httpx.TimeoutException:
            return "OpenFDA timeout: Proceed with clinical judgment."
        except Exception as e:
            return f"OpenFDA Error: {e}"


async def _search_complications(
    symptoms: str, surgery_type: str,
    patient_age: int | None = None,
    patient_sex: str | None = None,
    conditions: str | None = None
) -> str:
    """Queries PubMed for literature on the specific symptoms post-surgery, enriched with demographics."""
    # ──────────────────────────────────────────────────────────────
    # BUILD A STRUCTURED BOOLEAN QUERY
    # Instead of keyword soup ("calf pain TKA elderly male diabetes"),
    # we build a proper Boolean expression with field tags so PubMed
    # can match full concepts rather than individual words anywhere.
    # ──────────────────────────────────────────────────────────────

    # Clause 1: symptoms anchored to title/abstract
    symptom_clause = f"({symptoms}[tiab])"

    # Clause 2: surgery type — try to match both abbreviation and full term in title/abstract
    # Also add a MeSH hint with [MeSH Terms] in case indexers used the controlled vocabulary
    surgery_clean = surgery_type.strip()
    # Common surgery abbreviation expansions for better MeSH coverage
    _surgery_mesh_hints = {
        "tka": "Arthroplasty, Replacement, Knee",
        "total knee arthroplasty": "Arthroplasty, Replacement, Knee",
        "total knee replacement": "Arthroplasty, Replacement, Knee",
        "cabg": "Coronary Artery Bypass",
        "coronary artery bypass": "Coronary Artery Bypass",
        "tha": "Arthroplasty, Replacement, Hip",
        "total hip arthroplasty": "Arthroplasty, Replacement, Hip",
    }
    mesh_hint = _surgery_mesh_hints.get(surgery_clean.lower())
    if mesh_hint:
        surgery_clause = f'("{surgery_clean}"[tiab] OR "{mesh_hint}"[MeSH Terms])'
    else:
        surgery_clause = f'("{surgery_clean}"[tiab])'

    # Clause 3: demographics (optional — only appended if provided)
    demographic_clauses = []

    if patient_age:
        # Map age to PubMed's official MeSH age-group terms
        if patient_age >= 80:
            demographic_clauses.append('("aged, 80 and over"[MeSH Terms] OR "very old"[tiab])')
        elif patient_age >= 65:
            demographic_clauses.append('("aged"[MeSH Terms] OR "elderly"[tiab] OR "older adult"[tiab])')
        elif patient_age >= 45:
            demographic_clauses.append('("middle aged"[MeSH Terms] OR "middle-aged"[tiab])')

    if patient_sex:
        sex_normalized = patient_sex.strip().lower()
        if sex_normalized in ("male", "m"):
            demographic_clauses.append('("male"[MeSH Terms] OR "men"[tiab])')
        elif sex_normalized in ("female", "f"):
            demographic_clauses.append('("female"[MeSH Terms] OR "women"[tiab])')

    if conditions:
        # Add top 2 comorbidities using both free-text and MeSH
        _condition_mesh_hints = {
            "hypertension": "Hypertension[MeSH Terms]",
            "type 2 diabetes": "Diabetes Mellitus, Type 2[MeSH Terms]",
            "diabetes": "Diabetes Mellitus[MeSH Terms]",
            "hyperlipidemia": "Hyperlipidemias[MeSH Terms]",
            "atrial fibrillation": "Atrial Fibrillation[MeSH Terms]",
            "obesity": "Obesity[MeSH Terms]",
            "chronic kidney disease": "Renal Insufficiency, Chronic[MeSH Terms]",
            "coronary artery disease": "Coronary Artery Disease[MeSH Terms]",
        }
        top_conditions = [c.strip() for c in conditions.split(",")[:2] if c.strip()]
        for cond in top_conditions:
            mesh = _condition_mesh_hints.get(cond.lower())
            if mesh:
                demographic_clauses.append(f'("{cond}"[tiab] OR {mesh})')
            else:
                demographic_clauses.append(f'("{cond}"[tiab])')

    # Clause 4: article type filter — prefer high-level evidence
    evidence_filter = (
        '(systematic review[ptyp] OR meta-analysis[ptyp] OR '
        'randomized controlled trial[ptyp] OR clinical trial[ptyp] OR '
        'review[ptyp])'
    )

    # ── Assemble full query (Tier 1: fully structured + demographics + evidence filter) ──
    query_parts = [symptom_clause, surgery_clause] + demographic_clauses + [evidence_filter]
    full_query = " AND ".join(query_parts)

    # ── Duplicate call detection (based on full structured query) ──
    cache_key = f"pubmed:{full_query.lower()}"
    if cache_key in _result_cache:
        return (
            f"[CACHED RESULT — DO NOT call this tool again with the same inputs.]\n\n"
            f"{_result_cache[cache_key]}\n\n"
            f"You already have this data. Use it to write your final risk assessment report NOW."
        )

    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

            # ── TIER 1: Full structured query with demographics + evidence filter ──
            print(f"INFO:  Querying PubMed [Tier 1 — structured+demographics]: {full_query}")
            t1_url = f"{base_url}?db=pubmed&term={full_query}&retmode=json&retmax=3&sort=relevance"
            s_resp = await client.get(t1_url)
            id_list = []
            tier_used = "Tier 1 (structured, demographic-filtered, high-evidence)"
            if s_resp.status_code == 200:
                id_list = s_resp.json().get("esearchresult", {}).get("idlist", [])

            # ── TIER 2: Drop demographics, keep symptoms + surgery + evidence filter ──
            if not id_list:
                t2_query = f"{symptom_clause} AND {surgery_clause} AND {evidence_filter}"
                print(f"INFO:  Querying PubMed [Tier 2 — structured, no demographics]: {t2_query}")
                t2_url = f"{base_url}?db=pubmed&term={t2_query}&retmode=json&retmax=3&sort=relevance"
                s_resp = await client.get(t2_url)
                tier_used = "Tier 2 (structured, no demographic filter, high-evidence)"
                if s_resp.status_code == 200:
                    id_list = s_resp.json().get("esearchresult", {}).get("idlist", [])

            # ── TIER 3: Drop evidence filter too — bare symptoms + surgery [tiab] ──
            if not id_list:
                t3_query = f"{symptom_clause} AND {surgery_clause}"
                print(f"INFO:  Querying PubMed [Tier 3 — bare tiab]: {t3_query}")
                t3_url = f"{base_url}?db=pubmed&term={t3_query}&retmode=json&retmax=3&sort=relevance"
                s_resp = await client.get(t3_url)
                tier_used = "Tier 3 (symptoms + surgery only, all article types)"
                if s_resp.status_code == 200:
                    id_list = s_resp.json().get("esearchresult", {}).get("idlist", [])

            if not id_list:
                return "No medical literature found matching these symptoms and surgery after three search tiers."

            # ── Step 2: efetch — retrieve full XML records ──
            ids = ",".join(id_list)
            fetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={ids}&retmode=xml"
            f_resp = await client.get(fetch_url)

            if f_resp.status_code != 200:
                return f"PubMed Fetch Error: {f_resp.status_code}"

            # ── Parse XML and extract article type + conclusion ──
            root = ET.fromstring(f_resp.text)
            articles = root.findall(".//PubmedArticle")

            results = []
            for article in articles:
                title_elem = article.find(".//ArticleTitle")
                title = title_elem.text if title_elem is not None else "Unknown Title"

                # Extract publication type (e.g., Systematic Review, Clinical Trial)
                pub_types = [
                    pt.text for pt in article.findall(".//PublicationType") if pt.text
                ]
                pub_type_str = ", ".join(pub_types[:2]) if pub_types else "Article"

                # Try to find a labeled CONCLUSION section first
                abstract_texts = article.findall(".//AbstractText")
                conclusion = None
                for at in abstract_texts:
                    if at.get("Label", "").upper() in ("CONCLUSION", "CONCLUSIONS"):
                        conclusion = at.text
                        break

                # Fallback: also check RESULTS section if CONCLUSION is absent
                if not conclusion:
                    for at in abstract_texts:
                        if at.get("Label", "").upper() == "RESULTS":
                            conclusion = at.text
                            break

                # Final fallback: first + last sentence of full abstract
                if not conclusion and abstract_texts:
                    full_abstract = " ".join(at.text for at in abstract_texts if at.text)
                    sentences = [s.strip() for s in full_abstract.split(". ") if s.strip()]
                    if len(sentences) >= 2:
                        conclusion = f"{sentences[0]}. ... {sentences[-1]}."
                    elif sentences:
                        conclusion = f"{sentences[0]}."

                if not conclusion:
                    conclusion = "No abstract available."

                results.append(
                    f"[{pub_type_str}]\n"
                    f"Title: {title}\n"
                    f"Finding: {conclusion}"
                )

            # ── Assemble output with search context header ──
            header_lines = [
                f"PubMed structured query ({tier_used}):",
                f"  Symptoms   : {symptoms}",
                f"  Surgery    : {surgery_type}",
            ]
            if patient_age:
                header_lines.append(f"  Age        : {patient_age} yrs")
            if patient_sex:
                header_lines.append(f"  Sex        : {patient_sex}")
            if conditions:
                header_lines.append(f"  Conditions : {conditions}")
            header_lines.append("-" * 60)
            header = "\n".join(header_lines) + "\n"

            result = header + "\n\n".join(results)
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
        # Shared demographic properties reused across both tools
        _demographic_props = {
            "patient_age": {"type": "integer", "description": "Patient's age in years (e.g., 65). Affects pharmacokinetic risk weighting."},
            "patient_sex": {"type": "string", "enum": ["male", "female"], "description": "Patient's biological sex. Used to filter FDA reports by sex."},
            "conditions": {"type": "string", "description": "Comma-separated pre-existing conditions (e.g., 'hypertension, type 2 diabetes'). Narrows results to relevant comorbidity profiles."}
        }
        return [
            types.Tool(
                name="check_drug_interactions",
                description=(
                    "Checks FDA FAERS database for adverse events when two drugs are co-administered. "
                    "Provide patient demographics (age, sex, conditions) for clinically relevant results "
                    "instead of broad population-level statistics."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "drug1": {"type": "string", "description": "First drug generic name (e.g., ibuprofen)"},
                        "drug2": {"type": "string", "description": "Second drug generic name (e.g., warfarin)"},
                        **_demographic_props
                    },
                    "required": ["drug1", "drug2"]
                }
            ),
            types.Tool(
                name="search_complications",
                description=(
                    "Searches PubMed medical literature for complications matching symptoms and surgery. "
                    "Provide patient demographics to get age/sex/comorbidity-specific literature "
                    "instead of generic results."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symptoms": {"type": "string", "description": "Patient symptoms (e.g., calf pain, swelling)"},
                        "surgery_type": {"type": "string", "description": "Surgery performed (e.g., total knee arthroplasty)"},
                        **_demographic_props
                    },
                    "required": ["symptoms", "surgery_type"]
                }
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        # Extract shared demographic args
        demo_kwargs = {
            "patient_age": arguments.get("patient_age"),
            "patient_sex": arguments.get("patient_sex"),
            "conditions": arguments.get("conditions"),
        }
        if name == "check_drug_interactions":
            result = await _check_drug_interactions(
                arguments.get("drug1", ""), arguments.get("drug2", ""),
                **demo_kwargs
            )
        elif name == "search_complications":
            result = await _search_complications(
                arguments.get("symptoms", ""), arguments.get("surgery_type", ""),
                **demo_kwargs
            )
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