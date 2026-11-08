# PoopWatch: working rules

## Writing style (code, comments, docs, commit messages)
- Never use the em dash character or a double hyphen as punctuation. Use a
  comma, colon, period, or parentheses instead.
- Command-line flags like `--clear` are fine. Only prose punctuation is banned.
- Keep sentences plain. Avoid filler phrases like "It's worth noting".

## Git
- Commit and push only when the user asks.
- Never commit `evidence/`, `.env`, `*.db`, images, videos, or `*.pt` weights.
- Commit messages have no AI attribution lines.

## Camera data is private
- Do not print camera passwords or RTSP URLs with credentials in output. Use
  `scrub()` from `poopwatch/capture.py` before showing any ffmpeg error text.
