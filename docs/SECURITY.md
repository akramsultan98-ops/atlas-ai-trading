# Security and containment

## Current posture

**No exchange credentials exist in this project.** The account is unfunded. No
real-money path is reachable. `ATLAS_EXCHANGE_ENV` defaults to `testnet` and live
operation requires an explicit configuration change plus the final audit gate.

## The containment boundary (spec §2)

ATLAS separates an **advisory plane** (where the AI operates) from a **control plane**
(deterministic code only). The boundary is enforced structurally.

| Control | Requirement | Mechanism |
|---|---|---|
| No AI access to funds | AI-01 | The research process reads credentials from a config scope that does not contain them. It is not "told not to" — the values are absent. |
| No dangerous tools | AI-02 | The LLM tool surface is an allowlist. Order, sizing, promotion and kill-switch functions are not registered. |
| No AI writes to execution state | AI-03 | Separate read-only database connection for the research plane. |
| One-way authority | AI-04 | Retire/disable is exposed; enable/resume/scale is not. |
| Untrusted output | AI-06 | LLM output is parsed as data against a strict schema. Never `exec`, never string-interpolated into SQL or an order. |
| Tamper-evident history | AI-07 | Hash-chained audit log. |

**Why this shape.** A prompt instruction not to trade is a request that a capable,
confused or compromised model can route around. A process that never receives an API key
cannot place an order regardless of intent. Enforcement lives in credential scope and
tool registration, never in prompt text.

## Strategy definitions are data, not code

LLM output is a **declarative specification** interpreted by a fixed engine (STRAT-01).
ATLAS never executes model-generated code. This is the single largest deviation from the
source, which emits Pine Script, and it exists because executing generated code would
place arbitrary logic inside the control plane.

## Credential handling (when live, later)

- Spot trading permission only. **Withdrawals disabled** (EXEC-07).
- IP-allowlisted where the deployment has a static address.
- Read from environment, never committed. `.env` is gitignored.
- Never logged. Never included in audit payloads, alerts or error messages.
- Held only by the execution process.

## Operational limits

Spot only. No leverage, margin, futures or derivatives (RISK-07). No withdrawal API
usage under any circumstance. Deterministic risk controls (§6) are evaluated in the
control plane and cannot be relaxed by the advisory plane.

## Reporting

This is a private single-operator repository. Security-relevant defects should be fixed
before any further phase work proceeds.
