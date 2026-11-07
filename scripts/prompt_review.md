# Prompt review: reduce false positives

You are reviewing the vision prompts for PoopWatch, a camera app that detects
dogs squatting to defecate. The reviewer marked some events as false positives
and wrote a note on each one saying why.

Inputs:
- `evidence/review/false_positives.md`: the false positives, with the model's
  reasoning, its gate output, and the reviewer's note.
- `poopwatch/analyze.py`: the gate prompt and the verdict prompt.

Do this:
1. Read the false-positive file. Group the notes by pattern, such as "a bush
   was called a dog" or "a person walking past was counted as a handler".
   Ignore one-off cases that don't repeat.
2. For each repeated pattern, propose one small, specific change to the gate
   or verdict prompt in `poopwatch/analyze.py`. Prefer a clear rule ("a dog
   must be visible as an animal with a body, legs, and tail") over a long
   list of examples.
3. Do not change the output schema, the field names, or the violation logic in
   `Verdict.is_violation`. Those are code, not prompt text.
4. Write your proposal as a unified diff to `evidence/review/prompt_proposal.diff`.
   Do not edit `analyze.py` directly. A person will review and test it first.
5. Above the diff, list each proposed change with the pattern it targets, the
   notes that support it, and the risk: which real dog events it might now
   reject.

Keep changes small. If the notes don't show a clear pattern, say so and propose
nothing.
