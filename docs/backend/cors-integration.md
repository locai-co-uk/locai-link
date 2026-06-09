# Backend integration — CORS origins for Link

**Audience:** control-plane developers. Link-side is shipped; this is what's left.

## Where origins go

`cors_allowed_origins` ships in the `DEPLOY_MODEL` command's
`runtime_config.process.parameters`. Link copies that dict into the
pipeline source's args verbatim, so no agent-side code change is needed.

```json
{
  "command_type": "DEPLOY_MODEL",
  "payload": {
    "model_id": "llama-3-8b",
    "model_name": "llama-3-8b.Q4_K_M.gguf",
    "runtime_config": {
      "process": {
        "impl": {"runner": "gguf_language_model"},
        "parameters": {
          "n_ctx": 4096,
          "n_gpu_layers": 35,
          "cors_allowed_origins": [
            "https://app.safechat.com",
            "https://dev.safechat.com"
          ]
        }
      },
      "outputs": [{"semantic_type": "text_generation"}]
    }
  }
}
```

Omit the field for tenants that don't need CORS (standalone, Meetily). Link
treats absent/empty as "no proxy" — llama-swap binds the public port
directly, zero overhead.

## Why DEPLOY_MODEL, not START_SERVING

CORS is deployment-time policy, not per-start state. Origins describe who
can call this model under this tenant; they survive every `stop/start
serving` cycle. `START_SERVING` carries transient knobs like `port` and
`alias` only, and would need Link-side code changes to read CORS.

## What you need to wire up

1. **Tenant config** carrying the allowlist (one per integrator). Shape is
   up to you; the contract is that DEPLOY_MODEL rendering can read it.
2. **Tenant resolution at registration.** The device sends its bundle
   `manifest.json` (includes `profile: "safechat"` etc.) when calling
   `/devices/register-with-key`. Use that — possibly combined with the
   Registration Key — to pick the right tenant config.
3. **DEPLOY_MODEL rendering** for that tenant injects
   `cors_allowed_origins` into `process.parameters`.

## Don'ts

- Don't push a top-level `cors:` field on AgentConfig. Schema is 2.1; Link
  doesn't read that field. We'd bump the schema with a coordination plan.
- Don't bake origins into bundles. Profiles intentionally don't carry CORS
  so the allowlist stays editable per-environment without rebuilds.
- Don't set `cors_allowed_origins` on non-HTTP plugins. Only `language_model`
  accepts the kwarg today; others will raise.

## Verifying it works

Once a SafeChat-tenant device has a model deployed:

```bash
curl -X OPTIONS -H 'Origin: https://app.safechat.com' \
     -H 'Access-Control-Request-Method: POST' \
     -i http://<device>:8100/v1/chat/completions
# → 204, Access-Control-Allow-Origin: https://app.safechat.com  ✓

curl -X OPTIONS -H 'Origin: https://evil.example.com' \
     -i http://<device>:8100/v1/chat/completions
# → 204, no ACAO header  ✓
```

Agent log line to look for: `CORS proxy listening on http://… -> http://127.0.0.1:… (allowlist: N origins)`.
Absence + a `Starting llama-swap on http://0.0.0.0:8100` line means CORS is off.

## Open questions

- **Tenant identification rules** — is it derived from the Registration Key,
  from `manifest.profile`, or both?
- **Live updates** — if a tenant adds an origin, do running devices need it
  pushed? Today re-deploy is required. Could add an `UPDATE_PIPELINE_ARGS`
  command if needed.
- **Per-environment splits** — dev/staging want `http://localhost:3000`;
  prod doesn't. Same tenant_id across envs means you need an env axis.

## Pointers

- Proxy: `src/link/infra/cors_proxy.py`
- Plugin kwarg: `plugins/language_model/adapter.py` (`LanguageModel.__init__`)
- Deploy → args mapping: `src/link/app/runtime.py::_map_runtime_to_pipeline_config`
- Bundle profiles: `bundling/profiles/{standalone,meetily,safechat}.yaml`
