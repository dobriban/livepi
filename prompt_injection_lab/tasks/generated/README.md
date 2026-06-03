# Generated Email Extension Cases

`email_gemini_extension_500.jsonl` contains 500 deterministic Gemini-inspired
email extension cases:

- 5 email-only extension technique families
- 100 cases per family
- 5 existing LivePI objective tags, preserving the normal verifier path

Regenerate from the repo root:

```bash
python3 prompt_injection_lab/scripts/generate_email_gemini_extension_cases.py
```

Run with the local email sweep:

```bash
python3 prompt_injection_lab/scripts/sweep_email_gmail_local.py \
  --email-attack-cases-jsonl prompt_injection_lab/tasks/generated/email_gemini_extension_500.jsonl
```

