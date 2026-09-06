# Yandex live probes — synthetic data only

TL;DR: key ready; merged Qwen Chat adapters passed live synthetic extraction and QA. Combined suite: **1003 passed**. OCR prototype: English 16/16 exact cells; Russian 15/16 (Latin B → Cyrillic В in B12, numeric fields unchanged). Neither real medical sharing nor production Yandex activation has occurred.

## Provisioned

- Folder: `health-agent` (`b1gq8lr560bj7fec3suq`), separate from the existing unrelated project.
- Service account: `health-agent-ai`; roles `ai.languageModels.user`, `ai.assistants.editor`, and `ai.vision.user` for the requested synthetic OCR comparison.
- API-key scope: `yc.ai.foundationModels.execute`. Secret is in ignored `.tokens/yandex-api-key`, mode0600. The extra browser-download copy was removed. No key material is included in this report.
- No compute instances, Object Storage buckets or model training resources created.

## Observed results, not a general model benchmark

| Probe | Result |
|---|---|
| Qwen3.6-35b-a3b, Responses, simple/multiline/table lab inputs | Wrong field assignment or incomplete output; rejected by existing validation. No DB writes. |
| Qwen Responses, reasoning disabled | Still rejected on tested rows. |
| GPT-OSS-120B, Responses, reasoning low | Wrong field assignments; rejected. This does not establish that GPT-OSS is generally worse than Qwen. |
| Qwen native Chat, reasoning none, temperature0, raw user source, strict JSON | Simple, table, multiline and qualified `<15` examples accepted with exact values/units/references. |
| Qwen Chat with JSON-encoded page source | One table response had an escaped-newline evidence mismatch. Raw source in its own untrusted user message avoids this unnecessary encoding. |
| Labelled multiline source (`Результат`, `Референсный интервал`) | Existing strict source validator rejects intervening labels. This is a supported-layout limitation, not evidence by itself of a model error. Validator was NOT relaxed. |
| Qwen native Chat synthetic QA | Cited the supplied glucose fact and correctly said sleep causes cannot be determined without sleep/context data. Not a medical-quality acceptance suite. |
| Chat with `store=False` plus `safety_identifier` | HTTP400. Otherwise identical call with `store=False` but no `safety_identifier` succeeded. No actual profile IDs sent as identifiers. |
| Vision OCR `table`, direct one-page raster-only PDF | HTTP200, one table, 16/16 expected cells matched (header + 3 rows, 4 columns), including `<15`. In-memory synthetic PDF only; no patient document used. |
| Qwen vs GPT-OSS-120B, native Chat, same three-row source | Both extracted all 3 rows exactly. Single-run elapsed times: Qwen 0.76s; GPT-OSS 24.53s. Qwen used reasoning none; GPT-OSS low. This is not a repeatable performance benchmark. |
| Vision OCR `table`, Russian raster-only PDF | HTTP200, one table, 15/16 exact cells in two runs. Diagnostic repeat identified only `Витамин B12` → `Витамин В12` (Latin/Cyrillic letter); numbers, units, reference ranges, `<15` and collection date preserved. No automatic source-text repair or production OCR integration. |

Request safeguards: official Yandex endpoints, separate key, no retries, bounded output/timeouts, `x-data-logging-enabled:false`; native Chat preserves `store=False`. These flags do not establish guaranteed zero retention. Initial failed probes are retained in this record rather than counting only successful examples.

## Decision

Native Chat for Yandex questions/lab extraction is implemented; OpenAI is unchanged. Retain Qwen as a tested candidate, not a claimed medical specialist. GPT-OSS is not a drop-in configuration switch with the current reasoning setting. Vision OCR is a promising scanned-table fallback, currently a synthetic prototype only. Use ordinary PDF text first, consider OCR for scans; do not create a second cloud data store or replace already-stored page evidence without preserving provenance.

## Merged-code acceptance

- Feature commit `556c4b8`, root merge `0343617`; independent task review and final whole-feature review both approved. The subagent-driven workflow separated implementation, task review and final integration review; live checks were done by root.
- `uv run pytest -q`: **1003 passed in 14.70s**, five inherited SWIG deprecation warnings. Ruff clean, `mypy src` clean (110 files), lock and diff checks clean. This is automated code acceptance, not completion of all real medical scenarios.
- Actual merged `YandexLabExtractor`, default 2000-token cap, invented Glucose/Ferritin/B12 table: exact 3/3 names, values, units and references; `<15` remains a qualified value, not a made-up numeric 15. One call, 1.27s.
- Actual merged `YandexResponsesResponder`, invented glucose-only context: cited `[LAB1]`, compared 5.1 with supplied 3.9–5.5 reference and stated that the cause of poor sleep cannot be inferred from these data. One call, 0.67s. This narrow probe does not validate broad medical advice quality.
- Live probes used one-off settings and in-memory invented data, no DB access/writes. Production provider, profile allowlists and cloud extraction settings were not enabled. Ksyusha's data is not authorized by another user's consent.

Official references: [OCR models](https://aistudio.yandex.ru/en/docs/vision/concepts/ocr/models), [PDF request guide](https://aistudio.yandex.ru/en/docs/vision/operations/ocr/text-detection-pdf), [Qwen/model guide](https://aistudio.yandex.ru/en/docs/ai-studio/concepts/generation/models), [reasoning controls](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/generation/chain-of-thought).
