"""TDD: repo artifacts Glama needs for a quality score / deployable release."""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import shutil

import pytest

from dbu_mcp.server import build

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_glama_json_exists_and_lists_maintainer():
    path = ROOT / "glama.json"
    assert path.is_file(), "glama.json missing at repo root (required to claim on Glama)"
    data = json.loads(path.read_text())
    assert data.get("$schema") == "https://glama.ai/mcp/schemas/server.json"
    maintainers = data.get("maintainers")
    assert isinstance(maintainers, list) and "markatdataandbeyond" in maintainers


def test_dockerfile_builds_stdio_mcp_entrypoint():
    path = ROOT / "Dockerfile"
    assert path.is_file(), "Dockerfile missing at repo root"
    text = path.read_text()
    assert "FROM python:" in text
    assert "dbu-waste-finder-mcp" in text
    # Must install the mcp package and default to stdio entrypoint
    assert re.search(r"(ENTRYPOINT|CMD).*(dbu-waste-finder-mcp|python)", text)


def test_license_and_readme_present():
    assert (ROOT / "LICENSE").is_file()
    readme = (ROOT / "README.md").read_text()
    assert "DBU Waste Finder" in readme
    mcp_readme = (ROOT / "mcp" / "README.md").read_text()
    assert "cost_summary" in mcp_readme and "find_waste" in mcp_readme


def test_tool_descriptions_are_agent_ready():
    """Glama Tool Definition Quality: purpose, params, behaviour, when-to-use."""
    mcp = build(runner=lambda q: [])
    # MCPServer v2 exposes tools via list; fall back to inspecting registered callables
    tools = getattr(mcp, "_tool_manager", None)
    names_desc = {}
    # Prefer public list_tools if available (async); else inspect attributes
    list_fn = getattr(mcp, "list_tools", None)
    if list_fn is not None:
        import asyncio
        result = asyncio.run(list_fn())
        items = result.tools if hasattr(result, "tools") else result
        for t in items:
            names_desc[t.name] = (t.description or "")
    else:
        # Fallback: read docstrings from the build()-registered tools via module source
        src = (ROOT / "mcp" / "src" / "dbu_mcp" / "server.py").read_text()
        for name in ("cost_summary", "top_cost_drivers", "find_waste",
                     "cost_review_report", "explain_cost_spike", "propose_fix"):
            # crude: find def <name> and the preceding/following docstring
            m = re.search(rf'def {name}\([^)]*\).*?:\n\s+"""(.*?)"""', src, re.S)
            assert m, f"missing docstring for {name}"
            names_desc[name] = m.group(1)

    required = {"cost_summary", "top_cost_drivers", "find_waste",
                "cost_review_report", "explain_cost_spike", "propose_fix"}
    assert required <= set(names_desc), f"missing tools: {required - set(names_desc)}"
    for name, desc in names_desc.items():
        if name not in required:
            continue
        assert len(desc) >= 80, f"{name} description too short for Glama TDQS ({len(desc)})"
        # behavioural transparency: read-only / never applies
        low = desc.lower()
        assert any(w in low for w in ("read-only", "list price", "proposal", "never", "not applied", "diff")), \
            f"{name} should state read-only / proposal behaviour"
        # parameter semantics: at least one param named in the description for tools that take days
        if name in ("cost_summary", "top_cost_drivers", "find_waste", "cost_review_report"):
            assert "days" in low, f"{name} should document the days parameter"



def test_mcp_stdio_initialize_without_databricks_env():
    """Glama introspection needs initialize/tools/list with no credentials."""
    init = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        '{"protocolVersion":"2024-11-05","capabilities":{},'
        '"clientInfo":{"name":"glama-test","version":"0"}}}\n'
    )
    env = {k: v for k, v in __import__("os").environ.items()
           if not k.startswith("DATABRICKS_")}
    cmd = [str(ROOT / ".venv" / "bin" / "dbu-waste-finder-mcp")]
    if not (ROOT / ".venv" / "bin" / "dbu-waste-finder-mcp").exists():
        cmd = [str(ROOT / ".venv" / "bin" / "python"), "-c",
               "from dbu_mcp.server import main; main()"]
    run = subprocess.run(
        cmd, input=init, capture_output=True, text=True, timeout=30, env=env, cwd=ROOT,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert "dbu-waste-finder" in run.stdout, run.stdout[:500]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_docker_image_builds_and_answers_initialize():
    """Build + run image for MCP initialize. Skips if docker.sock is not usable."""
    tag = "dbu-waste-finder-mcp:glama-test"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "."],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    combined = (build.stderr + build.stdout).lower()
    if build.returncode != 0 and "permission denied" in combined:
        pytest.skip(
            "docker daemon socket not usable (permission denied); Dockerfile present; "
            "stdio initialize covered by test_mcp_stdio_initialize_without_databricks_env"
        )
    assert build.returncode == 0, build.stdout[-2000:] + "\n" + build.stderr[-2000:]
    init = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        '{"protocolVersion":"2024-11-05","capabilities":{},'
        '"clientInfo":{"name":"glama-test","version":"0"}}}\n'
    )
    run = subprocess.run(
        ["docker", "run", "--rm", "-i", tag],
        input=init, capture_output=True, text=True, timeout=60,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert '"serverInfo"' in run.stdout or "dbu-waste-finder" in run.stdout, run.stdout[:500]
