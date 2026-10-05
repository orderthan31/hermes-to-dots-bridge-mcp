"""One-job child protocol: stdin JSON; stdout NDJSON events/final.

Only the provider resolver and protocol adapter are reused, not the agent loop.
No model_tools, AIAgent, profile prompts, memory, skill discovery, plugins,
background review, terminal, MCP clients or session persistence are initialized.
"""
from __future__ import annotations
import contextlib
import json
import logging
import os
import sys
from hyemi_review import (MAX_JOB_BYTES, PROFILE_PATHS, ReviewError, failure,
                          review_with_client, validate_profile)


def install_runtime_fence():
    """Child-local import and plugin discovery fence, without editing Hermes."""
    import importlib.abc
    forbidden = ("run_agent", "model_tools", "tools.registry", "tools.mcp_tool")
    if any(name in sys.modules for name in forbidden):
        raise ReviewError("runtime_boundary_failed")
    class Fence(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
                raise ReviewError("runtime_boundary_failed")
            return None
    sys.meta_path.insert(0, Fence())
    # Provider metadata imports the plugin facade. Discovery itself is forbidden.
    from hermes_cli import plugins
    def denied(*args, **kwargs):
        raise ReviewError("plugin_discovery_denied")
    plugins.PluginManager.discover_and_load = denied
    plugins.discover_plugins = denied
    plugins.start_background_plugin_discovery = denied


def run_backend(job):
    home = validate_profile(job.get("profile"))
    if os.environ.get("HYEMI_REVIEW_CHILD") != "1" or os.environ.get("HERMES_HOME") != home:
        raise ReviewError("profile_path_denied")
    # Environment belongs exclusively to this child. No caller credentials are
    # present in the job. Existing profile credentials are resolved by Hermes.
    install_runtime_fence()
    from dotenv import load_dotenv
    load_dotenv(os.path.join(home, ".env"), override=True, interpolate=False)
    os.environ["HERMES_HOME"] = home
    from hermes_cli import config as config_module
    config_module.load_config = config_module.load_config_readonly
    cfg = config_module.load_config_readonly()
    selected = cfg.get("model", {})
    if not isinstance(selected, dict):
        raise ReviewError("model_configuration_missing")
    model, provider = selected.get("default"), selected.get("provider")
    if not model or not provider or provider == "auto":
        raise ReviewError("model_configuration_missing")
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested=provider, target_model=model)
    mode = runtime.get("api_mode")
    # External process agents own their tools and cannot satisfy this boundary.
    if mode not in ("codex_responses", "chat_completions"):
        raise ReviewError("unsupported_restricted_transport")
    from openai import OpenAI
    base_client = OpenAI(api_key=runtime["api_key"], base_url=runtime["base_url"],
                         max_retries=0, timeout=job["timeout"])
    client = base_client
    if mode == "codex_responses":
        from agent.auxiliary_client import CodexAuxiliaryClient
        client = CodexAuxiliaryClient(base_client, model)
    try:
        result = review_with_client(client, model, job, job["timeout"])
        result["backend"] = {"provider": runtime["provider"], "model": model,
                             "mode": "content_only", "transport": mode,
                             "profile": job["profile"], "exposed_tools": []}
        return result
    finally:
        base_client.close()


def main():
    protocol = sys.stdout
    logging.disable(logging.CRITICAL)
    try:
        raw = sys.stdin.buffer.read(MAX_JOB_BYTES + 1024)
        if len(raw) > MAX_JOB_BYTES + 512:
            raise ReviewError("input_limit")
        job = json.loads(raw)
        protocol.write(json.dumps({"type": "progress", "stage": "started", "mode": "content_only"}) + "\n")
        protocol.flush()
        # Third-party warnings and errors can contain credentials. The protocol
        # carries only fixed error codes, never exceptions or provider responses.
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = run_backend(job)
    except ReviewError as exc:
        result = failure(exc.code)
    except TimeoutError:
        result = failure("backend_timeout")
    except Exception:
        result = failure("backend_failed")
    protocol.write(json.dumps({"type": "final", "result": result}, ensure_ascii=False) + "\n")
    protocol.flush()


if __name__ == "__main__":
    main()
