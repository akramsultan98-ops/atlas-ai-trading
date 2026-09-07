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

## Deployment

### Docker

```bash
cp .env.example .env          # fill in; never committed
docker compose -f deploy/docker-compose.yml --env-file .env up -d
docker compose -f deploy/docker-compose.yml logs -f
```

The image does not default to `run` — starting a trader is an act by whoever launches
the container. State lives in the `atlas-data` volume; losing it loses the ledger, the
audit chain and the kill-switch state.

### systemd

```bash
sudo cp deploy/atlas.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now atlas
sudo journalctl -u atlas -f
```

`Restart=always` with `StartLimitBurst=5` throttles a crash loop. `KillSignal=SIGINT`
lets the scheduler shut down gracefully. Restart is safe because startup recovery
reconciles against the exchange before entries resume.

### Health

```bash
atlas health              # exit 0 healthy, 1 stale
atlas health --quiet      # exit code only; used as the container HEALTHCHECK
```

An armed kill switch is **not** unhealthy: the process is alive and deliberately not
trading, and restarting it would achieve nothing.

## Binance Testnet procedure

Not yet executed. `testnet.binance.vision` is unreachable from the development
environment (egress policy, `403` at the CONNECT tunnel), so every step below is
untested and is written from the API contract, not from observed behaviour.

1. Create a Testnet key at https://testnet.binance.vision (spot only).
2. Set `ATLAS_EXCHANGE_ENV=testnet`, `ATLAS_BINANCE_API_KEY`, `ATLAS_BINANCE_API_SECRET`.
3. `atlas preflight`. Read every line, not just the last one:
   - `exchange_reachable: yes` and `authenticated: yes`
   - `clock_drift_ms` below `recv_window_ms`. Above it, every signed request is
     rejected as `-1021`, which reads like a bad key and is not one.
   - `filters_<SYMBOL>` present for each configured symbol, and **compare the real
     `minNotional` against the $5 ATLAS assumed** (RISK-09, `docs/EVIDENCE.md`).
   - `feasible_stop_band_<SYMBOL>` non-empty. `EMPTY` means the balance is too small
     to size any trade at the intended risk — fund the account or nothing will trade.
4. `atlas killswitch init` — the deliberate release to trade.
5. `atlas run --once --explain` — one cycle, printing the decision for every
   configured symbol. `atlas decisions` replays the same from the audit trail, and
   `--json` gives the machine-readable form.

   **`NO_STRATEGY` is the expected result on a fresh install.** It means no strategy
   has status LIVE, so the entry pipeline never ran. It is not a fault, and no amount
   of market data will change it: a strategy has to be generated, backtested,
   independently verified, gated, incubated and promoted before anything can trade.
6. Confirm in order, in the audit trail and the database:
   - klines fetched and validated
   - entry submitted (`ORDER_INTENT` before the network call) and acknowledged
   - a row in `orders` for the entry, **and for both exit legs**, each carrying a
     `position_id`
   - a row in `positions`, open, at the price the entry *filled* at
   - the OCO accepted — if it is rejected, `OCO_ENDPOINT` in
     `atlas/execution/broker.py` may need the newer `/api/v3/orderList/oco`
     (see `docs/EVIDENCE.md`)
   - fill retrieved from `myTrades` and recorded in `fills`
   - reconciliation clean, kill switch still disarmed
7. Let a position close (or close it by hand on the exchange). Confirm the next tick
   ingests the exit fill, `positions.closed_at` is set, `realised_pnl` is recorded, and
   the symbol is free to trade again. **A position that never closes locally is the
   failure to watch for**: it blocks the symbol under RISK-08 and inflates deployed
   capital indefinitely.
8. Kill the process mid-position and restart. Recovery must reconcile before entering,
   and must *not* arm the kill switch over ATLAS's own open orders.
9. Only then `atlas run` continuously.

If any step fails, stop. Do not proceed to the next.

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
