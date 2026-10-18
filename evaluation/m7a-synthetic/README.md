# M7a synthetic live-smoke corpus

Self-written, fictional documents for the M7a OpenRouter live smoke. No real paper,
person, instrument or dataset. `harbor-sensor.txt` is rendered to a one-page PDF by
`scripts/m7a_runtime_drill.py`; `lumen-bench.md` is uploaded as is (and once more by a
second synthetic user as a same-bytes control). The worker's own parsers preflight the
corpus to 8 embedded chunks in total.

`answers.json` keeps the model answers as samples for later human review
(`pending_human_review`). Valid JSON and exact quotes are not grounding judgements.
