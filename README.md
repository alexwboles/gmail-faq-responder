# Safe Gmail FAQ Responder Demo

A small, dependency-free reference implementation for the fitness-business
Gmail automation opportunity. It demonstrates the safety behavior promised in
the proposal before account credentials are connected.

## What it proves

- Replies only when an approved FAQ matches above a configurable threshold.
- Leaves unknown or ambiguous questions for manual review.
- Prevents duplicate processing within a batch by message ID.
- Uses a durable SQLite idempotency store to prevent duplicate drafts across
  restarts and repeated polling.
- Creates drafts only; sending remains a separate human-approved action.
- Writes a privacy-conscious JSONL audit trail containing decisions and IDs,
  without copying message bodies.
- Keeps classification separate from delivery, so a Gmail integration cannot
  send a message unless the decision is explicitly `reply`.
- Includes automated tests and editable JSON examples.

## Run the demo

From this folder:

```powershell
python faq_responder.py
python -m unittest -v
```

The sample output should reply to the schedule question, leave the medical
question untouched, and skip the duplicate message ID. Automated integration
tests also exercise draft creation, manual-review routing, audit logging, and
durable duplicate protection across repeated runs.

## Production integration plan

1. Replace the sample message loader with Gmail API polling using the minimum
   required OAuth scopes.
2. Connect the included SQLite idempotency store to the deployed worker.
3. Create drafts first during acceptance testing; enable sending only after the
   client approves the FAQ text and test results.
4. Record decisions and failures in an audit log without storing unnecessary
   message content.
5. Add deployment-specific monitoring and a documented manual kill switch.

No credentials, personal email, or external services are used by this demo.
