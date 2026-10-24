"""Integration glue checks: n8n workflow exports, Fly.io configs, the demo e-mail sender.

Run with ``uv run pytest integrations/tests``. Nothing here starts n8n or calls Fly; the n8n
Code nodes' JavaScript is executed under Node with a stub of the n8n runtime
(``n8n_code_harness.cjs``) and its output is checked against the API's real HMAC verifier and
``EmailIngest`` schema.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from invoicedesk_api.auth import verify_ingest_signature
from invoicedesk_api.errors import ApiError
from invoicedesk_api.schemas import EmailIngest
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / "integrations" / "n8n" / "workflows"
FLY = ROOT / "infra" / "fly"
HARNESS = Path(__file__).with_name("n8n_code_harness.cjs")
DEMO_EMAIL = ROOT / "data" / "demo" / "bank-change-email.json"
DEMO_PDF = ROOT / "data" / "demo" / "email" / "kettle-creek-A-3601.pdf"
SECRET = "test-ingest-secret-0123456789abcdef"  # test value only

NODE = shutil.which("node") or (
    "/opt/node22/bin/node" if Path("/opt/node22/bin/node").exists() else None
)
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


# ----------------------------------------------------------------------------- helpers
def load_workflow(name: str) -> dict[str, Any]:
    return json.loads((WORKFLOWS / name).read_text(encoding="utf-8"))


def node_by_name(wf: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [n for n in wf["nodes"] if n["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


def run_code_node(code: str, items: list[dict[str, Any]], env: dict[str, str]) -> dict[str, Any]:
    assert NODE is not None
    proc = subprocess.run(
        [NODE, str(HARNESS)],
        input=json.dumps({"code": code, "items": items, "env": env}),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(proc.stdout)


def api_verify(body: bytes, header: str | None, secret: str = SECRET) -> None:
    """The API's own signature check, with a minimal runtime stand-in."""
    rt = SimpleNamespace(settings=SimpleNamespace(ingest_webhook_secret=SecretStr(secret)))
    verify_ingest_signature(rt, body, header)  # type: ignore[arg-type]


def load_sender() -> ModuleType:
    path = ROOT / "integrations" / "n8n" / "scripts" / "post_demo_email.py"
    spec = importlib.util.spec_from_file_location("post_demo_email", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["post_demo_email"] = mod
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------- n8n exports
@pytest.mark.parametrize("name", ["email-ingest.json", "vendor-followup.json"])
def test_workflow_export_is_valid_n8n_json(name: str) -> None:
    wf = load_workflow(name)
    assert {"name", "nodes", "connections", "settings"} <= wf.keys()
    assert wf["active"] is False  # imported inactive; activated after credentials are set
    assert wf["settings"]["executionOrder"] == "v1"
    assert wf["settings"]["saveDataSuccessExecution"] == "none"

    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    ids = [n["id"] for n in wf["nodes"]]
    assert len(ids) == len(set(ids))
    for n in wf["nodes"]:
        assert n["type"].startswith("n8n-nodes-base."), n["type"]
        assert isinstance(n["typeVersion"], int | float)
        assert len(n["position"]) == 2
        for cred in n.get("credentials", {}).values():
            assert cred["id"] == "REPLACE_ON_IMPORT"  # never a real credential reference

    for src, outputs in wf["connections"].items():
        assert src in names
        for branch in outputs["main"]:
            for edge in branch:
                assert edge["node"] in names
                assert edge["type"] == "main"

    # every node except the trigger is reachable from a connection
    targets = {e["node"] for o in wf["connections"].values() for b in o["main"] for e in b}
    triggers = [n for n in wf["nodes"] if n["name"] not in targets]
    assert len(triggers) == 1


@pytest.mark.parametrize("name", ["email-ingest.json", "vendor-followup.json"])
def test_workflow_export_carries_no_secrets(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    for needle in ("change-me", "dev-only-change-me", "password", "sk-ant", "Bearer "):
        assert needle not in text
    # secrets are read from the n8n environment at run time only
    if name == "email-ingest.json":
        assert "$env.INGEST_WEBHOOK_SECRET" in text


def test_email_ingest_wiring() -> None:
    wf = load_workflow("email-ingest.json")
    imap = node_by_name(wf, "Vendor inbox (IMAP)")
    assert imap["type"] == "n8n-nodes-base.emailReadImap"
    assert imap["parameters"]["format"] == "resolved"  # attachments come through as binary
    http = node_by_name(wf, "POST /api/v1/ingest/email")
    p = http["parameters"]
    assert p["method"] == "POST"
    assert p["url"].endswith("/api/v1/ingest/email")
    # the signed string is sent verbatim, so the HMAC covers exactly the bytes on the wire
    assert p["contentType"] == "raw"
    assert p["rawContentType"] == "application/json"
    assert p["body"] == "={{ $json.body }}"
    headers = {h["name"]: h["value"] for h in p["headerParameters"]["parameters"]}
    assert headers == {"X-InvoiceDesk-Signature": "={{ $json.signature }}"}
    assert http["retryOnFail"] is True


def test_followup_wiring() -> None:
    wf = load_workflow("vendor-followup.json")
    hook = node_by_name(wf, "Approved follow-up (webhook)")
    assert hook["parameters"]["httpMethod"] == "POST"
    assert hook["parameters"]["path"] == "invoicedesk-followup"
    assert hook["parameters"]["responseMode"] == "responseNode"
    branches = wf["connections"]["Valid?"]["main"]
    assert [e["node"] for e in branches[0]] == ["Send via SMTP"]
    assert [e["node"] for e in branches[1]] == ["Respond: rejected"]
    send = node_by_name(wf, "Send via SMTP")
    assert send["parameters"]["toEmail"] == "={{ $json.to }}"
    assert "smtp" in send["credentials"]
    rejected = node_by_name(wf, "Respond: rejected")
    assert rejected["parameters"]["options"]["responseCode"] == 422


# ----------------------------------------------------------------------------- Code nodes under Node
def _ingest_code() -> str:
    return node_by_name(load_workflow("email-ingest.json"), "Build signed payload")["parameters"][
        "jsCode"
    ]


def _followup_code() -> str:
    return node_by_name(load_workflow("vendor-followup.json"), "Validate")["parameters"]["jsCode"]


def _imap_item(**json_overrides: Any) -> dict[str, Any]:
    pdf = DEMO_PDF.read_bytes()
    mail: dict[str, Any] = {
        "messageId": "<remit-update-A-3601@mail.example>",
        "from": {
            "value": [{"address": "billing@kettlecreek.example", "name": "Kettle Creek"}],
            "text": "Kettle Creek <billing@kettlecreek.example>",
        },
        "subject": "Invoice A-3601 – updated remittance details",  # non-ASCII on purpose
        "text": "Please update our bank details before you pay it: account 000555067890.",
        "date": "2026-10-20T14:03:00.000Z",
    }
    mail.update(json_overrides)
    return {
        "json": mail,
        "binary": {
            "attachment_0": {
                "mimeType": "application/pdf",
                "fileName": "kettle-creek-A-3601.pdf",
                "data_b64": base64.b64encode(pdf).decode(),
            },
            "attachment_1": {
                "mimeType": "text/plain",
                "fileName": "notes.txt",
                "data_b64": base64.b64encode(b"not an invoice").decode(),
            },
        },
    }


@needs_node
def test_ingest_code_node_output_passes_the_api_signature_check_and_schema() -> None:
    res = run_code_node(_ingest_code(), [_imap_item()], {"INGEST_WEBHOOK_SECRET": SECRET})
    assert res["ok"], res
    (item,) = res["items"]
    out = item["json"]
    body = out["body"].encode("utf-8")

    api_verify(body, out["signature"])  # raises ApiError on mismatch
    with pytest.raises(ApiError):
        api_verify(body, out["signature"], secret="another-secret")
    with pytest.raises(ApiError):
        api_verify(body + b" ", out["signature"])

    msg = EmailIngest.model_validate(json.loads(body))
    assert msg.message_id == "<remit-update-A-3601@mail.example>"
    assert msg.from_ == "billing@kettlecreek.example"
    assert msg.subject.startswith("Invoice A-3601 –")
    assert "bank details" in msg.body_text  # the body is forwarded so the API can screen it
    assert msg.received_at == "2026-10-20T14:03:00.000Z"
    assert [a.filename for a in msg.attachments] == ["kettle-creek-A-3601.pdf"]
    assert base64.b64decode(msg.attachments[0].content_b64) == DEMO_PDF.read_bytes()
    assert out["skipped"] == [{"filename": "notes.txt", "reason": "type text/plain"}]
    assert out["attachments"] == 1


@needs_node
def test_ingest_code_node_handles_string_from_html_body_and_missing_message_id() -> None:
    item = _imap_item(
        messageId="",
        **{
            "from": "Vendor Billing <ap@vendor.example>",
            "text": "",
            "html": "<p>Hi&nbsp;there</p>",
        },
    )
    env = {"INGEST_WEBHOOK_SECRET": SECRET}
    first = run_code_node(_ingest_code(), [item], env)
    second = run_code_node(_ingest_code(), [item], env)
    assert first["ok"] and second["ok"]
    a = json.loads(first["items"][0]["json"]["body"])
    b = json.loads(second["items"][0]["json"]["body"])
    assert a["from"] == "ap@vendor.example"
    assert a["body_text"] == "Hi there"
    assert a["message_id"].startswith("<n8n-") and a["message_id"] == b["message_id"]
    EmailIngest.model_validate(a)


@needs_node
def test_ingest_code_node_caps_attachments_and_refuses_without_secret() -> None:
    item = _imap_item()
    pdf = item["binary"]["attachment_0"]
    item["binary"] = {f"attachment_{i:02d}": dict(pdf, fileName=f"f{i}.pdf") for i in range(12)}
    res = run_code_node(_ingest_code(), [item], {"INGEST_WEBHOOK_SECRET": SECRET})
    assert res["ok"]
    out = res["items"][0]["json"]
    assert out["attachments"] == 10
    assert len(out["skipped"]) == 2
    EmailIngest.model_validate(json.loads(out["body"]))  # schema allows at most 10

    res = run_code_node(_ingest_code(), [_imap_item()], {})
    assert not res["ok"]
    assert "INGEST_WEBHOOK_SECRET" in res["error"]


def _followup_item(**overrides: Any) -> dict[str, Any]:
    body = {
        "followup_id": "1b6f4a52-3c0e-4f43-9a51-0f2d1f0b8e11",
        "document_id": "0d8d1c1e-8f5b-4b8e-9d0e-2a7f9e3c4b55",
        "to": "billing@kettlecreek.example",
        "subject": "Missing work-order number on invoice A-3600",
        "body": "Hello, please resend with the work-order number. Account 000555067890.",
    }
    body.update(overrides)
    return {"json": {"headers": {}, "params": {}, "query": {}, "body": body}}


@needs_node
def test_followup_validator_accepts_a_good_payload_and_masks_digits() -> None:
    res = run_code_node(_followup_code(), [_followup_item()], {})
    assert res["ok"], res
    out = res["items"][0]["json"]
    assert out["ok"] is True and out["errors"] == []
    assert out["to"] == "billing@kettlecreek.example"
    assert "000555067890" not in out["text"]
    assert out["text"].endswith("Account ••••7890.")


@needs_node
@pytest.mark.parametrize(
    ("override", "env"),
    [
        ({"to": "a@x.example, attacker@evil.example"}, {}),
        ({"to": "not-an-address"}, {}),
        ({"subject": "Hi\r\nBcc: attacker@evil.example"}, {}),
        ({"subject": ""}, {}),
        ({"body": "x" * 5001}, {}),
        ({"followup_id": ""}, {}),
        (
            {"to": "billing@elsewhere.example"},
            {"FOLLOWUP_ALLOWED_RECIPIENT_DOMAINS": "kettlecreek.example"},
        ),
    ],
)
def test_followup_validator_rejects_bad_payloads(
    override: dict[str, str], env: dict[str, str]
) -> None:
    res = run_code_node(_followup_code(), [_followup_item(**override)], env)
    assert res["ok"], res
    out = res["items"][0]["json"]
    assert out["ok"] is False
    assert out["errors"]


@needs_node
def test_followup_validator_allow_list_accepts_listed_domain() -> None:
    env = {"FOLLOWUP_ALLOWED_RECIPIENT_DOMAINS": " Kettlecreek.example , other.example"}
    res = run_code_node(_followup_code(), [_followup_item()], env)
    assert res["items"][0]["json"]["ok"] is True


# ----------------------------------------------------------------------------- demo e-mail sender
def test_demo_email_sender_builds_a_payload_the_api_accepts() -> None:
    mod = load_sender()
    payload = mod.build_payload(DEMO_EMAIL)
    body = mod.encode_body(payload)
    sig = mod.sign(body, SECRET)
    api_verify(body, sig)
    msg = EmailIngest.model_validate(json.loads(body))
    assert msg.attachments and msg.attachments[0].content_type == "application/pdf"
    assert "bank details" in msg.body_text
    assert sig == "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_demo_email_sender_dry_run_and_missing_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = load_sender()
    monkeypatch.delenv("INGEST_WEBHOOK_SECRET", raising=False)
    assert mod.main(["--dry-run"]) == 2
    monkeypatch.setenv("INGEST_WEBHOOK_SECRET", SECRET)
    assert mod.main(["--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["dry_run"] is True
    assert out["attachments"] == ["kettle-creek-A-3601.pdf"]
    assert SECRET not in json.dumps(out)


def test_demo_email_sender_refuses_attachment_path_escape(tmp_path: Path) -> None:
    mod = load_sender()
    fixture = tmp_path / "mail.json"
    fixture.write_text(
        json.dumps(
            {"message_id": "<x@y>", "from_addr": "a@b.example", "attachments": ["../secret.pdf"]}
        )
    )
    with pytest.raises(ValueError, match="escapes"):
        mod.build_payload(fixture)


# ----------------------------------------------------------------------------- Fly configs
FLY_APPS = ["api", "worker", "mcp", "n8n"]
SECRETISH = ("SECRET", "_KEY", "TOKEN", "PASSWORD", "DATABASE_URL", "SUPABASE_URL")


def load_fly(name: str) -> dict[str, Any]:
    with (FLY / f"{name}.toml").open("rb") as fh:
        return tomllib.load(fh)


def console_scripts() -> set[str]:
    names: set[str] = set()
    for pp in ROOT.glob("*/**/pyproject.toml"):
        if "node_modules" in pp.parts or ".venv" in pp.parts:
            continue
        with pp.open("rb") as fh:
            names |= set(tomllib.load(fh).get("project", {}).get("scripts", {}))
    return names


@pytest.mark.parametrize("name", FLY_APPS)
def test_fly_config_parses_and_has_no_secrets(name: str) -> None:
    cfg = load_fly(name)
    assert cfg["app"] == f"invoicedesk-{name}"
    assert cfg["primary_region"]
    for key in cfg.get("env", {}):
        assert not any(s in key.upper() for s in SECRETISH), f"{name}: {key} belongs in fly secrets"


@pytest.mark.parametrize("name", ["api", "worker", "mcp"])
def test_python_apps_use_the_shared_dockerfile_and_real_entry_points(name: str) -> None:
    cfg = load_fly(name)
    dockerfile = (FLY / cfg["build"]["dockerfile"]).resolve()
    assert dockerfile == (ROOT / "infra" / "docker" / "python.Dockerfile").resolve()
    assert dockerfile.exists()
    scripts = console_scripts() | {"uvicorn"}
    for cmd in cfg["processes"].values():
        assert shlex.split(cmd)[0] in scripts, cmd
    assert cfg["env"]["INVOICEDESK_MODE"] in {"offline", "live"}
    if "QBO_ENVIRONMENT" in cfg["env"]:
        assert cfg["env"]["QBO_ENVIRONMENT"] == "sandbox"


def test_api_runs_migrations_and_health_checks() -> None:
    cfg = load_fly("api")
    assert shlex.split(cfg["deploy"]["release_command"]) == ["invoicedesk", "migrate"]
    assert cfg["http_service"]["internal_port"] == 8000
    assert cfg["http_service"]["checks"][0]["path"] == "/healthz"
    assert "invoicedesk_api.main:app" in cfg["processes"]["app"]
    assert cfg["env"]["TRUST_FORWARDED_FOR"] == "false"


def test_mcp_resource_url_matches_its_public_host() -> None:
    cfg = load_fly("mcp")
    env = cfg["env"]
    assert env["MCP_RESOURCE_URL"] == f"https://{env['MCP_ALLOWED_HOSTS']}/mcp"
    assert cfg["http_service"]["internal_port"] == int(env["MCP_PORT"])


def test_n8n_is_private_and_pinned() -> None:
    cfg = load_fly("n8n")
    assert "http_service" not in cfg and "services" not in cfg
    assert cfg["build"]["image"] == "n8nio/n8n:1.80.0"
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "n8nio/n8n:1.80.0" in compose  # same version locally and on Fly
    assert cfg["env"]["NODE_FUNCTION_ALLOW_BUILTIN"] == "crypto"
    api_env = load_fly("api")["env"]
    assert api_env["N8N_FOLLOWUP_WEBHOOK_URL"].startswith("http://invoicedesk-n8n.internal:5678/")
    assert api_env["N8N_FOLLOWUP_WEBHOOK_URL"].endswith(
        "/webhook/"
        + node_by_name(load_workflow("vendor-followup.json"), "Approved follow-up (webhook)")[
            "parameters"
        ]["path"]
    )
