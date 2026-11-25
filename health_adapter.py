"""Harness-owned model probe. No Agent turn, Task, tool or session is created."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import yaml

RUNTIME = 'codex'
WIRE = 'responses'


def configuration():
    home = Path(os.getenv("AGENTTEAMS_WORKER_HOME") or os.environ["HOME"])
    record = Path(os.getenv("TEAMHARNESS_RUNTIME_CONFIG") or home / "runtime/runtime.yaml")
    if RUNTIME == "codex" and os.getenv("CODEX_AUTH_MODE") == "chatgpt":
        raise NotImplementedError("account authentication requires a native account probe")
    if RUNTIME != "openclaw" and record.is_file():
        doc = yaml.safe_load(record.read_text())
        binding = doc.get("desired", {}).get("model")
        if binding is None:
            return native_configuration(home)
        key_env = doc.get("credentials", {}).get("gatewayKeyEnv", "AGENTTEAMS_WORKER_GATEWAY_KEY")
        key = os.getenv(key_env, "").strip() or binding.get("gatewayKey", "")
        return binding["gatewayUrl"], key, binding["model"], WIRE
    return native_configuration(home)


def native_configuration(home):
    native = Path(os.getenv("OPENCLAW_CONFIG_PATH") or home / "openclaw.json")
    doc = json.loads(native.read_text())
    provider, model = doc["agents"]["defaults"]["model"]["primary"].split("/", 1)
    selected = doc["models"]["providers"][provider]
    wire = WIRE
    if RUNTIME == "openclaw":
        wire = {"openai-completions": "chat", "openai-responses": "responses", "anthropic-messages": "messages"}[selected.get("api", "openai-completions")]
    return selected["baseUrl"], selected["apiKey"], model, wire


def valid(data, wire):
    if not isinstance(data, dict) or data.get("error") or (data.get("base_resp") or {}).get("status_code", 0):
        return False
    if wire == "messages":
        return data.get("type") == "message" and data.get("role") == "assistant" and any(
            block.get("type") == "text" and str(block.get("text", "")).strip() for block in data.get("content", []))
    if wire == "responses":
        return data.get("status") in {"completed", "incomplete"} and any(
            block.get("type") == "output_text" and str(block.get("text", "")).strip()
            for item in data.get("output", []) for block in item.get("content", []))
    return any(str((item.get("message") or {}).get("content") or (item.get("message") or {}).get("reasoning_content") or "").strip()
               for item in data.get("choices", []))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def check(config):
    base, key, model, wire = config
    address = urllib.parse.urlsplit(base)
    if address.scheme not in {"http", "https"} or not address.netloc or address.username or address.query or address.fragment or not key or not model:
        return {"status": "unknown", "reason": "configuration"}
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": "Reply with one word: ok"}], "max_tokens": 256, "stream": False}
    suffix = "/chat/completions"
    if wire == "messages":
        suffix = "/messages"
        headers["anthropic-version"] = "2023-06-01"
    elif wire == "responses":
        suffix = "/responses"
        payload = {"model": model, "input": "Reply with one word: ok", "max_output_tokens": 256, "stream": False, "store": False}
    request = urllib.request.Request(base + suffix, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            body = response.read(65537)
        if len(body) > 65536:
            raise ValueError("oversized model response")
        data=json.loads(body)
        if isinstance(data,dict) and (data.get("error") or (data.get("base_resp") or {}).get("status_code",0)):
            text=json.dumps(data).lower()
            reason="billing_or_quota" if any(term in text for term in ("quota","balance","1008")) else "authentication" if any(term in text for term in ("unauthorized","api_key","authentication")) else "invalid_response"
            return {"status":"failed","reason":reason}
        return {"status": "ok", "reason": "ok"} if valid(data, wire) else {"status": "failed", "reason": "invalid_response"}
    except urllib.error.HTTPError as error:
        reason = {401: "authentication", 403: "authentication", 402: "billing_or_quota", 404: "model_or_endpoint", 429: "rate_limit_or_quota"}.get(error.code)
        return {"status": "failed", "reason": reason or ("upstream_unavailable" if error.code >= 500 else "configuration")}
    except (TimeoutError, socket.timeout):
        return {"status": "failed", "reason": "timeout"}
    except urllib.error.URLError as error:
        return {"status": "failed", "reason": "timeout" if isinstance(error.reason, TimeoutError) else "network"}
    except (ValueError, TypeError, AttributeError):
        return {"status": "failed", "reason": "invalid_response"}


def readiness():
    status=json.loads((Path(os.getenv("AGENTTEAMS_LOG_DIR","/var/log/agentteams/manager"))/"status.json").read_text())
    ready=status.get("boot_id")==os.getenv("AGENTTEAMS_BOOT_ID") and status.get("state")=="ready"
    return {"version":1,"status":"ready" if ready else "starting","evidence":status.get("evidence","native_status")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--configuration", action="store_true")
    parser.add_argument("--readiness", action="store_true")
    args = parser.parse_args()
    if args.readiness:
        try:
            result=readiness()
        except (OSError,ValueError,TypeError,AttributeError,subprocess.SubprocessError):
            result={"version":1,"status":"starting","evidence":"native_probe_pending"}
        print(json.dumps(result))
        return
    try:
        config = configuration()
        digest = hashlib.sha256(json.dumps(config).encode()).hexdigest()
        if args.configuration:
            print(digest)
            return
        result = dict(check(config), configuration=digest)
    except NotImplementedError:
        result = {"status": "unknown", "reason": "unsupported"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError):
        result = {"status": "unknown", "reason": "configuration"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
