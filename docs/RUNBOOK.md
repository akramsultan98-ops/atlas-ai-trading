# Runbook

## Kill switch

### Check state
```bash
python -m atlas.cli killswitch status
```

### Arm manually
```bash
python -m atlas.cli killswitch arm --reason "why"
```
Effect: no new entries system-wide. **Existing protective stops remain in place**
(KILL-03) — cancelling them would leave positions naked.

### Disarm
```bash
python -m atlas.cli killswitch disarm --reason "why" --confirm
```
Requires an explicit human confirmation flag and a reason string (KILL-05). There is no
AI-reachable path. Before disarming, establish *why* it armed — the audit log records
the trigger.

### It armed on its own — triage order

1. `killswitch status` — read the trigger and timestamp.
2. Audit log — the events immediately preceding.
3. Match the trigger:

| Trigger | Spec | Meaning | Action |
|---|---|---|---|
| `DAILY_LOSS_LIMIT` | RISK-05 | Lost > 5% in one UTC day | Do not disarm the same day. Review which strategies contributed. |
| `MAX_ACCOUNT_DD` | RISK-06 | Down > 20% from peak | Full review before any disarm. Likely several strategies need retiring. |
| `RECONCILIATION_FAILURE` | EXEC-05 | Local and exchange state disagree irreconcilably | **Do not disarm.** Reconcile manually against the exchange first. |
| `DATA_STALENESS` | DATA-05 | Feed older than 2× bar interval | Confirm feed health, then disarm. |
| `API_ERROR_RATE` | KILL-02 | Sustained exchange errors | Check Binance status. Disarm when clear. |
| `STATE_DISAGREEMENT` | KILL-04 | File and DB disagree | Investigate for tampering or partial write, repair, then disarm. |
| `MANUAL` | — | A human armed it | Whoever armed it decides. |

## Strategy retirement

Retirement is automatic, single-trigger and one-way (MON-06, MON-07). A retired
strategy places no new entries; open positions run to their existing brackets.

**There is no programmatic reactivation.** Reinstating a retired strategy is a
deliberate human action taken outside the trading loop. This is the central lesson of
the source material: the documented failure was a present rule overridden by its author
because the strategy briefly recovered.

Before reinstating, ask what changed — in the market or in the strategy. "It looks
better now" is the failure mode, not a reason.

## Restart recovery

1. Load persisted state.
2. Read kill-switch state — unreadable means armed (KILL-06).
3. Reconcile every open position against the exchange (EXEC-04).
4. Irreconcilable divergence arms the kill switch (EXEC-05); do not trade.
5. Resume monitoring before resuming entries.

## Audit log

```bash
python -m atlas.cli audit verify     # walk the hash chain
python -m atlas.cli audit tail -n 50
```

A verification failure means the log was modified or truncated. Treat as a security
incident: arm the kill switch and investigate before trading.

## Pre-live checklist

Not to be exercised until the full system audit and explicit approval.

- [ ] All phases complete, full suite green
- [ ] Testnet: signal → sized → bracketed → filled → reconciled
- [ ] Restart recovery verified mid-position
- [ ] Kill switch verified to halt mid-flight without orphaning a position
- [ ] Audit chain verifies
- [ ] API key: spot only, **withdrawals disabled**, IP-allowlisted
- [ ] Live `minNotional` and fee tier confirmed against `exchangeInfo` (RISK-09, BT-04)
- [ ] At least one strategy through full incubation with human promotion approval
- [ ] `ATLAS_EXCHANGE_ENV=live` set deliberately, as the final step
