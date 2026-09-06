# Configuration

All configuration is environment-driven and loaded through `atlas.config.Settings`.
Copy `.env.example` to `.env`.

**No financial parameter has a default (ADR-006).** A missing risk value is a startup
error, not a fallback. A default risk parameter is a silent policy.

## Operational

| Variable | Default | Meaning |
|---|---|---|
| `ATLAS_ENV` | `development` | `development` \| `testing` \| `production` |
| `ATLAS_DATA_DIR` | `var` | Root for database, kill-switch state, caches |
| `ATLAS_LOG_LEVEL` | `INFO` | Standard logging level |

## Risk policy — spec §6, all ATLAS decisions

| Variable | Required | Spec | Meaning |
|---|---|---|---|
| `ATLAS_RISK_PCT` | yes | RISK-01 | Fraction of equity risked per trade. `0.01` = 1%. |
| `ATLAS_MAX_CONCURRENT` | yes | RISK-02 | Maximum simultaneous open positions. |
| `ATLAS_MAX_POSITION_PCT` | yes | RISK-03 | Cap on a single position as a fraction of equity. |
| `ATLAS_MAX_DEPLOYED_PCT` | yes | RISK-04 | Cap on total deployed capital. |
| `ATLAS_DAILY_LOSS_LIMIT` | yes | RISK-05 | Daily loss fraction that arms the kill switch. |
| `ATLAS_MAX_ACCOUNT_DD` | yes | RISK-06 | Drawdown from peak equity that arms the kill switch. |

Validated at load: each must be in `(0, 1)` except `ATLAS_MAX_CONCURRENT` (positive
integer), and `MAX_POSITION_PCT` must not exceed `MAX_DEPLOYED_PCT` — otherwise a single
position could never be opened at its own cap.

Note that `MAX_POSITION_PCT × MAX_CONCURRENT` *may* exceed `MAX_DEPLOYED_PCT`, and does
at the shipped values (`0.3333 × 3 = 0.9999` against `0.75`). This is not a
contradiction: the two caps bind at different levels. A single position is capped at
33.3% of equity, while total deployment is capped at 75%, so three positions cannot all
be opened at full size — the third is truncated by remaining budget or rejected. The
aggregate cap is deliberately the tighter constraint.

### Feasible stop band

`RISK_PCT` and `MAX_POSITION_PCT` jointly fix the minimum tradeable stop distance:

```
lower bound = RISK_PCT / MAX_POSITION_PCT
```

At the shipped values that is `0.01 / 0.3333 = 3.0%`, and it does not change as the
account grows. Tighter stops are structurally under-risked. Changing that requires
changing the policy, not waiting for equity.

## Execution

| Variable | Default | Meaning |
|---|---|---|
| `ATLAS_EXCHANGE_ENV` | `testnet` | `testnet` \| `live`. `live` additionally requires the final audit gate. |
| `ATLAS_QUOTE_ASSET` | `USDT` | Single quote asset (RISK-10). |

## Credentials

| Variable | Plane | Notes |
|---|---|---|
| `ATLAS_BINANCE_API_KEY` | execution only | Spot trading only, withdrawals disabled. |
| `ATLAS_BINANCE_API_SECRET` | execution only | Never logged, never in audit payloads. |
| `ANTHROPIC_API_KEY` | research only | The research plane has no exchange credential. |

`Settings.for_research_plane()` returns a settings object with exchange credentials
stripped, enforcing AI-01 in code rather than by convention.
