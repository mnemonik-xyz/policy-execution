# Real io.net workload instance

`instance-ionet.json` is the workload of the mainnet canary deal.
It replaces the bundled synthetic `instance.json`.
Every number in it comes from a real io.net price estimate and a real LibriVox recording.
This record lists both sources and the rule behind each number.
The instance hash below goes stale on any change to the JSON.
`python3 scripts/test_instance_ionet.py` checks this record against the JSON.

## Estimate

The estimate is a real `caas_get_price_estimate` result.
The user took it on the laptop with their own io.net key:

```sh
.venv/bin/python scripts/ionet-showcase.py read caas_get_price_estimate \
  --args artifacts/price-args-a6000.json --out artifacts/io-estimate.json
```

The raw result stays in `artifacts/io-estimate.json`, which is not committed.

| Field | Value |
|---|---|
| Tool | caas_get_price_estimate |
| Arguments | hardware_id gpu_1x_a6000, location_ids US, duration_hours 1, gpus_per_container 1, replica_count 1 |
| observed_at | 1792048743 (2026-10-15T07:19:03Z) |
| Total cost | 0.49 USDC at JSON pointer /result/data/data/total_cost_usdc |

The spec text expected an estimate from 2026-10-14.
No saved estimate from that date was available, so the user took this fresh one on 2026-10-15.
The returned total, 0.49, equals the spec text.

The excerpt keeps only the tool, the arguments, `observed_at` and the price fields.
Hardware and location are in the arguments.
It drops `available_replica_count` and `min_non_promotional_credits_required`.
The raw result holds no key, account or deployment field.

```json
{
  "tool": "caas_get_price_estimate",
  "arguments": {
    "hardware_id": "gpu_1x_a6000",
    "location_ids": "US",
    "duration_hours": 1,
    "gpus_per_container": 1,
    "replica_count": 1
  },
  "observed_at": 1792048743,
  "result": {
    "service": "caas",
    "operation": "get_price_estimate",
    "data": {
      "data": {
        "gpus_per_container": 1,
        "replica_count": 1,
        "discount": 0.0,
        "ionet_fee": 0.0,
        "ionet_fee_percent": 0.0,
        "currency_conversion_fee": 0.0,
        "currency_conversion_fee_percent": 0.0,
        "total_cost_usdc": 0.49
      }
    }
  }
}
```

## Audio

The workload is the transcription of one LibriVox recording.
The audio file is not committed.

| Field | Value |
|---|---|
| Title | If |
| Author | Rudyard Kipling |
| Reader | Lydia Uribe (LibriVox reader 450, Version 9) |
| Catalogue page | https://librivox.org/if-by-rudyard-kipling/ |
| File URL | https://archive.org/download/if_kipling_librivox/if_kipling_lmu_64kb.mp3 |
| Format | MP3, 64 kbit/s, 24000 Hz, 2 channels |
| Duration (s) | 120.472 |
| Size (bytes) | 963968 |
| SHA-256 | fc859f575beb94c133f27900ad54bed9f0a549801fa81d02cae5318c1e1fc594 |
| Retrieved | 2026-10-15T07:32:54Z |
| Licence | The recording is in the public domain (LibriVox). The text is in the public domain (published 1910). |

The duration comes from `afinfo` on macOS. The size matches the archive.org metadata.
Check the file with `shasum -a 256 if_kipling_lmu_64kb.mp3`.

## Mapping

| Quantity | Rule |
|---|---|
| Tick | One tick is one second. |
| Machine | One machine: the estimated A6000 container. `capacity` is `gpus_per_container`, 1. |
| Price | `price_per_unit_tick` is the estimate total in micro-USD per lease hour: 0.49 × 1000000 = 490000. It is exact; no rounding. |
| Jobs | The recording is cut into consecutive 30 s segments. The remainder joins the last segment. One job transcribes one segment. |
| Durations | A job's duration is its segment length in seconds, a real-time-factor-1 transcription budget. 120.472 s gives 30, 30, 30 and 30.472. The last is rounded up to 31. |
| Units | Each job uses `units` 1: one GPU. |
| Release, deadline | Every job has `release` 0 and `deadline` 3600 = 3600 × `duration_hours`: the one-hour lease. |
| Dependencies | None. Segments are transcribed independently. |
| max_cost | The full lease: 3600 × 490000 = 1764000000. A valid schedule never models more than the estimated hour. |

The cost unit is "micro-USD-seconds per hour".
`check_schedule` computes `duration * units * price_per_unit_tick` over all jobs.
Divide a modelled cost by 3600 to get micro-USD.
`max_cost` and the costs in `quote.json` are in this unit.
They are not USD and not token base units.
The bounty amount is a separate token amount.

Rounding the last segment up adds 0.528 s of budget.
That raises the modelled cost by 0.528 × 490000 = 258720 units, about 72 micro-USD.
The budget is never smaller than the audio.

The reference seller places the four jobs back to back from tick 0.
Its schedule ends at tick 121 and costs 59290000, about 0.0165 USD.

## Values

| Key | Value |
|---|---|
| estimate_total_usd | 0.49 |
| estimate_observed_at | 1792048743 |
| cost_unit | micro-USD-seconds per hour |
| price_per_unit_tick | 490000 |
| deadline | 3600 |
| max_cost | 1764000000 |
| instance_hash | 0x48ed79b023d0ca12bcfea54afb9146e4262cb43c18df89fc77579f740118a6e0 |

`max_cost` is the value for `solver-canary.py init --max-cost`.
The instance hash comes from `target/debug/warrant-solver instance-hash solver-bounty/instance-ionet.json`.

## Limits

The checker proves a modelled cost, not the bill.
The durations are a stated budget, not a measured run.
Settlement starts no cloud run.
No GPU run is part of the proven deal.
The io.net price can change after `observed_at`.
