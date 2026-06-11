# Local AI Models

Model weight files are not committed to this repository.

Expected local layout:

```text
models/
  clickbait-classifier/
    config.json
    model.safetensors
    run_config.json
    threshold.json
    tokenizer.json
    tokenizer_config.json

  topic-mismatch-detector/
    config.json
    model.safetensors
    run_config.json
    threshold.json
    tokenizer.json
    tokenizer_config.json

  summary/
    bertsum_ext_model.pt
    config.json
    special_tokens_map.json
    tokenizer.json
    tokenizer_config.json
    vocab.txt
```

Default paths are configured in `src/collector/settings.py`.
Override them with environment variables when models live elsewhere:

```env
ABUSE_P1_MODEL_DIR=models/clickbait-classifier
ABUSE_P2_MODEL_DIR=models/topic-mismatch-detector
SUMMARY_MODEL_PATH=models/summary/bertsum_ext_model.pt
SUMMARY_TOKENIZER_DIR=models/summary
SUMMARY_SENTENCES=3
SUMMARY_MAX_CANDIDATES=80
SUMMARY_HEAD_CANDIDATES=50
SUMMARY_MIDDLE_CANDIDATES=15
SUMMARY_TAIL_CANDIDATES=15
```
