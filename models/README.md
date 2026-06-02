# Local Abuse Classifier Models

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
```

Default paths are configured in `src/collector/settings.py`.
Override them with environment variables when models live elsewhere:

```env
ABUSE_P1_MODEL_DIR=models/clickbait-classifier
ABUSE_P2_MODEL_DIR=models/topic-mismatch-detector
```
